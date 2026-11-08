from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import AuditService
from ..bank import BANK_NAMES, BankAdapter, BankResult, BankTimeout, BankUnavailable
from ..config import Settings
from ..intent import Intent
from ..models import Device, Lock, Session as SessionModel, Transfer, User
from ..money import amount_to_words, format_naira
from ..parser import names_compatible
from ..prompts import PromptLibrary
from ..security import ApprovalError, WebAuthnService
from ..state_machine import TransferState, display_state, ensure_transition
from ..timeutil import iso, utcnow


class TransferLocked(RuntimeError):
    code = "transfers_locked"


class TransferNotFound(RuntimeError):
    code = "transfer_not_found"


class RiskHold(RuntimeError):
    def __init__(self, seconds: int):
        super().__init__(f"risk hold for {seconds} seconds")
        self.seconds = seconds
        self.code = "risk_hold"


@dataclass(frozen=True)
class ConfirmationOutcome:
    outcome: str  # confirmed | cancelled | repeat | more_confirmation
    transfer: Transfer


class TransferService:
    def __init__(
        self,
        session_factory,
        audit: AuditService,
        bank: BankAdapter,
        security: WebAuthnService,
        prompts: PromptLibrary,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.audit = audit
        self.bank = bank
        self.security = security
        self.prompts = prompts
        self.settings = settings

    # ------------------------------------------------------------------
    # Locks
    # ------------------------------------------------------------------
    @staticmethod
    def is_locked(db: Session, user_id: str) -> bool:
        row = db.scalar(
            select(Lock)
            .where(Lock.user_id == user_id, Lock.unlocked_at.is_(None))
            .order_by(Lock.locked_at.desc())
            .limit(1)
        )
        return row is not None

    def engage_lock(self, user_id: str, reason: str = "customer_request") -> Lock:
        with self.audit.unit_of_work() as db:
            existing = db.scalar(
                select(Lock).where(Lock.user_id == user_id, Lock.unlocked_at.is_(None)).limit(1)
            )
            if existing is not None:
                return existing
            row = Lock(id=f"lock_{uuid.uuid4().hex}", user_id=user_id, reason=reason)
            db.add(row)
            self.audit.record(db, "lock.engaged", details={"reason": reason})
            return row

    def mark_unlocked(self, user_id: str) -> None:
        with self.audit.unit_of_work() as db:
            rows = db.scalars(
                select(Lock).where(Lock.user_id == user_id, Lock.unlocked_at.is_(None))
            ).all()
            now = utcnow()
            for row in rows:
                row.unlocked_at = now
            self.audit.record(db, "lock.unlocked", details={"count": len(rows)})

    # ------------------------------------------------------------------
    # Draft and read-back
    # ------------------------------------------------------------------
    def _readback_text(
        self,
        *,
        amount_kobo: int,
        recipient: str,
        bank_code: str,
        account_number: str,
        risk_flags: list[str],
        hint: str | None,
        language: str,
    ) -> str:
        base = self.prompts.render(
            "readback",
            language=language,
            amount_words=amount_to_words(amount_kobo),
            amount_display=format_naira(amount_kobo),
            recipient=recipient,
            last4=account_number[-4:],
            bank=BANK_NAMES.get(bank_code, bank_code),
        ).text
        prefixes: list[str] = []
        if "duplicate" in risk_flags:
            prefixes.append(self.prompts.render("duplicate_warning", language=language).text)
        if "new_recipient_large" in risk_flags:
            prefixes.append(self.prompts.render("new_recipient_warning", language=language).text)
        if "name_mismatch" in risk_flags:
            prefixes.append(
                self.prompts.render(
                    "name_mismatch_warning", language=language, recipient=recipient, hint=hint or ""
                ).text
            )
        return " ".join(prefixes + [base])

    def create_draft(
        self,
        *,
        user_id: str,
        session_id: str,
        amount_kobo: int,
        bank_code: str,
        account_number: str,
        verified_name: str,
        hint: str | None,
        language: str = "en",
    ) -> Transfer:
        now = utcnow()
        with self.audit.unit_of_work() as db:
            if self.is_locked(db, user_id):
                raise TransferLocked("transfers are locked")

            duplicate = db.scalar(
                select(Transfer)
                .where(
                    Transfer.user_id == user_id,
                    Transfer.bank_code == bank_code,
                    Transfer.account_number == account_number,
                    Transfer.amount_kobo == amount_kobo,
                    Transfer.state == TransferState.SETTLED.value,
                    Transfer.created_at >= now - timedelta(hours=self.settings.duplicate_window_hours),
                )
                .order_by(Transfer.created_at.desc())
                .limit(1)
            )
            prior_recipient = db.scalar(
                select(Transfer)
                .where(
                    Transfer.user_id == user_id,
                    Transfer.bank_code == bank_code,
                    Transfer.account_number == account_number,
                    Transfer.state == TransferState.SETTLED.value,
                )
                .limit(1)
            )

            flags: list[str] = []
            duplicate_of = None
            confirmations_required = 1
            risk_hold_until = None
            if duplicate is not None:
                flags.append("duplicate")
                duplicate_of = duplicate.id
                confirmations_required = 2
            if prior_recipient is None and amount_kobo >= self.settings.large_amount_threshold_kobo:
                flags.append("new_recipient_large")
                if self.settings.new_recipient_hold_s > 0:
                    risk_hold_until = now + timedelta(seconds=self.settings.new_recipient_hold_s)
            if not names_compatible(hint, verified_name):
                flags.append("name_mismatch")

            transfer = Transfer(
                id=f"tr_{uuid.uuid4().hex}",
                user_id=user_id,
                session_id=session_id,
                idempotency_key=f"avz_{uuid.uuid4().hex}",
                state=TransferState.DRAFTED.value,
                amount_kobo=amount_kobo,
                bank_code=bank_code,
                account_number=account_number,
                verified_recipient_name=verified_name,
                recipient_hint=hint or "",
                nonce=f"nonce_{secrets.token_hex(16)}",
                confirmations_required=confirmations_required,
                confirmations_done=0,
                risk_flags=flags,
                duplicate_of=duplicate_of,
                risk_hold_until=risk_hold_until,
                created_at=now,
                expires_at=now + timedelta(seconds=self.settings.draft_ttl_s),
            )
            transfer.readback_text = self._readback_text(
                amount_kobo=amount_kobo,
                recipient=verified_name,
                bank_code=bank_code,
                account_number=account_number,
                risk_flags=flags,
                hint=hint,
                language=language,
            )
            db.add(transfer)
            db.flush()
            self.audit.record(
                db,
                "transfer.drafted",
                transfer_id=transfer.id,
                session_id=session_id,
                details={
                    "amount_kobo": amount_kobo,
                    "bank_code": bank_code,
                    "account_last4": account_number[-4:],
                    "verified_name": verified_name,
                    "risk_flags": flags,
                    "duplicate_of": duplicate_of,
                },
            )
            return transfer

    def mark_readback(self, transfer_id: str) -> Transfer:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state == TransferState.DRAFTED.value:
                transfer.state = ensure_transition(transfer.state, TransferState.READBACK_PLAYED).value
                transfer.expires_at = utcnow() + timedelta(seconds=self.settings.readback_ttl_s)
                self.audit.record(
                    db,
                    "readback.played",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"amount_kobo": transfer.amount_kobo, "recipient": transfer.verified_recipient_name},
                )
            return transfer

    def record_confirmation(self, transfer_id: str, intent: Intent) -> ConfirmationOutcome:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state != TransferState.READBACK_PLAYED.value:
                raise ApprovalError(f"transfer is not awaiting a voice reply ({transfer.state})")

            if intent == Intent.YES:
                transfer.confirmations_done += 1
                if transfer.confirmations_done < transfer.confirmations_required:
                    transfer.expires_at = utcnow() + timedelta(seconds=self.settings.readback_ttl_s)
                    self.audit.record(
                        db,
                        "voice.confirm_partial",
                        transfer_id=transfer.id,
                        session_id=transfer.session_id,
                        details={
                            "done": transfer.confirmations_done,
                            "required": transfer.confirmations_required,
                        },
                    )
                    return ConfirmationOutcome("more_confirmation", transfer)
                transfer.state = ensure_transition(
                    transfer.state, TransferState.CONFIRMED_BY_VOICE
                ).value
                self.audit.record(
                    db,
                    "voice.confirmed",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"confirmations": transfer.confirmations_done},
                )
                return ConfirmationOutcome("confirmed", transfer)

            if intent == Intent.NO:
                transfer.state = ensure_transition(transfer.state, TransferState.CANCELLED).value
                self.audit.record(
                    db,
                    "voice.cancelled",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={},
                )
                return ConfirmationOutcome("cancelled", transfer)

            if intent == Intent.REPEAT:
                transfer.expires_at = utcnow() + timedelta(seconds=self.settings.readback_ttl_s)
                self.audit.record(
                    db,
                    "voice.repeat",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={},
                )
                return ConfirmationOutcome("repeat", transfer)

            raise ApprovalError("not a confirmation intent")

    # ------------------------------------------------------------------
    # Approval and submission
    # ------------------------------------------------------------------
    def issue_approval(self, transfer_id: str, device_id: str) -> tuple[dict | None, int, Transfer]:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state != TransferState.CONFIRMED_BY_VOICE.value:
                raise ApprovalError(f"transfer is not ready for approval ({transfer.state})")
            if self.is_locked(db, transfer.user_id):
                raise TransferLocked("transfers are locked")
            if transfer.risk_hold_until is not None:
                remaining = int((transfer.risk_hold_until - utcnow()).total_seconds())
                if remaining > 0:
                    return None, remaining, transfer
            device = db.get(Device, device_id)
            if device is None or device.user_id != transfer.user_id:
                raise ApprovalError("device does not belong to this customer")
            options = self.security.approval_options(db, transfer, device)
            transfer.state = ensure_transition(
                transfer.state, TransferState.AWAITING_BIOMETRIC
            ).value
            self.audit.record(
                db,
                "approval.requested",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={
                    "challenge_hash": transfer.challenge_hash,
                    "expires_at": transfer.approval_expires_at.isoformat()
                    if transfer.approval_expires_at
                    else None,
                },
            )
            return options, 0, transfer

    def reissue_approval(self, transfer_id: str, device_id: str) -> dict:
        """Issue a fresh challenge after a failed fingerprint read."""
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state != TransferState.AWAITING_BIOMETRIC.value:
                raise ApprovalError(f"transfer is not awaiting biometric approval ({transfer.state})")
            if transfer.approval_used_at is not None:
                raise ApprovalError("approval already used")
            if self.is_locked(db, transfer.user_id):
                raise TransferLocked("transfers are locked")
            device = db.get(Device, device_id)
            if device is None or device.user_id != transfer.user_id:
                raise ApprovalError("device does not belong to this customer")
            options = self.security.approval_options(db, transfer, device)
            self.audit.record(
                db,
                "approval.reissued",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={"challenge_hash": transfer.challenge_hash},
            )
            return options

    async def approve_and_submit(
        self, transfer_id: str, device_id: str, credential: dict | None, *, dev_approval: bool = False
    ) -> Transfer:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state != TransferState.AWAITING_BIOMETRIC.value:
                raise ApprovalError(f"transfer is not awaiting biometric approval ({transfer.state})")
            if self.is_locked(db, transfer.user_id):
                raise TransferLocked("transfers are locked")
            device = db.get(Device, device_id)
            if device is None or device.user_id != transfer.user_id:
                raise ApprovalError("device does not belong to this customer")
            try:
                if dev_approval:
                    if not self.security.dev_approval_allowed():
                        raise ApprovalError("developer approval is disabled")
                    transfer.approval_used_at = utcnow()
                    transfer.signed_approval = '{"dev_approval":true}'
                else:
                    if credential is None:
                        raise ApprovalError("missing WebAuthn assertion")
                    self.security.verify_approval(db, transfer, device, credential)
            except ApprovalError:
                self.audit.record(
                    db,
                    "approval.rejected",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"dev_approval": dev_approval},
                )
                raise
            transfer.state = ensure_transition(transfer.state, TransferState.APPROVED).value
            self.audit.record(
                db,
                "approval.verified",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={
                    "dev_approval": dev_approval,
                    "sign_count": device.sign_count,
                    "user_verified": True,
                },
            )
        return await self.submit(transfer_id)

    async def submit(self, transfer_id: str) -> Transfer:
        # Phase 1: mark SUBMITTED before calling the bank, so a crash or a lost
        # reply can be reconciled by idempotency key rather than blindly retried.
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state in {
                TransferState.SETTLED.value,
                TransferState.FAILED.value,
                TransferState.PENDING_UNKNOWN.value,
                TransferState.SUBMITTED.value,
            }:
                return transfer
            if transfer.state != TransferState.APPROVED.value:
                raise ApprovalError(f"transfer is not approved ({transfer.state})")
            transfer.state = ensure_transition(transfer.state, TransferState.SUBMITTED).value
            transfer.submitted_at = utcnow()
            self.audit.record(
                db,
                "transfer.submitted",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={
                    "amount_kobo": transfer.amount_kobo,
                    "bank_code": transfer.bank_code,
                    "account_last4": transfer.account_number[-4:],
                    "idempotency_key": transfer.idempotency_key,
                },
            )

        result = await self._send_to_bank(transfer)
        return self._finish_submission(transfer_id, result)

    async def _send_to_bank(self, transfer: Transfer) -> BankResult | None:
        try:
            return await self.bank.create_transfer(
                idempotency_key=transfer.idempotency_key,
                customer_ref=transfer.user_id,
                amount_kobo=transfer.amount_kobo,
                bank_code=transfer.bank_code,
                account_number=transfer.account_number,
                recipient_name=transfer.verified_recipient_name,
                nonce=transfer.nonce,
            )
        except (BankTimeout, BankUnavailable, Exception) as exc:
            self.audit.record_standalone(
                "bank.outcome_unknown",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={"error": type(exc).__name__},
            )
            return await self._status_then_maybe_retry(transfer)

    async def _status_then_maybe_retry(self, transfer: Transfer) -> BankResult | None:
        """Status check first. Only a definitive NOT_FOUND permits one safe retry."""
        try:
            status = await self.bank.get_transfer_status(
                reference=transfer.bank_reference, idempotency_key=transfer.idempotency_key
            )
        except (BankTimeout, BankUnavailable, Exception):
            return None

        self.audit.record_standalone(
            "status.checked",
            transfer_id=transfer.id,
            session_id=transfer.session_id,
            details={"status": status.status, "reference": status.reference},
        )
        if status.status != "NOT_FOUND":
            return status

        # Same idempotency key: a retry cannot create a second payment.
        try:
            return await self.bank.create_transfer(
                idempotency_key=transfer.idempotency_key,
                customer_ref=transfer.user_id,
                amount_kobo=transfer.amount_kobo,
                bank_code=transfer.bank_code,
                account_number=transfer.account_number,
                recipient_name=transfer.verified_recipient_name,
                nonce=transfer.nonce,
            )
        except (BankTimeout, BankUnavailable, Exception):
            return None

    def _finish_submission(self, transfer_id: str, result: BankResult | None) -> Transfer:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            now = utcnow()
            if result is None:
                transfer.state = ensure_transition(
                    transfer.state, TransferState.PENDING_UNKNOWN
                ).value
                self.audit.record(
                    db,
                    "transfer.pending_unknown",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"reason": "lost_reply_or_bank_unavailable"},
                )
                return transfer

            transfer.bank_reference = result.reference
            transfer.bank_status = result.raw or {"status": result.status}
            if result.status == "SETTLED":
                transfer.state = ensure_transition(transfer.state, TransferState.SETTLED).value
                transfer.settled_at = now
                self.audit.record(
                    db,
                    "transfer.settled",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={
                        "reference": result.reference,
                        "amount_kobo": result.amount_kobo,
                        "recipient": result.recipient_name,
                    },
                )
            elif result.status == "FAILED":
                transfer.state = ensure_transition(transfer.state, TransferState.FAILED).value
                transfer.failure_reason = result.failure_reason or "bank_declined"
                self.audit.record(
                    db,
                    "transfer.failed",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"reference": result.reference, "reason": transfer.failure_reason},
                )
            else:
                transfer.state = ensure_transition(
                    transfer.state, TransferState.PENDING_UNKNOWN
                ).value
                self.audit.record(
                    db,
                    "transfer.pending_unknown",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"reference": result.reference, "bank_status": result.status},
                )
            return transfer

    async def check_status(self, transfer_id: str) -> Transfer:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state not in {
                TransferState.SUBMITTED.value,
                TransferState.PENDING_UNKNOWN.value,
            }:
                return transfer
            reference = transfer.bank_reference
            key = transfer.idempotency_key
        try:
            status = await self.bank.get_transfer_status(reference=reference, idempotency_key=key)
        except (BankTimeout, BankUnavailable, Exception) as exc:
            self.audit.record_standalone(
                "status.check_failed",
                transfer_id=transfer_id,
                details={"error": type(exc).__name__},
            )
            return transfer
        self.audit.record_standalone(
            "status.checked",
            transfer_id=transfer_id,
            details={"status": status.status, "reference": status.reference},
        )
        if status.status == "NOT_FOUND":
            return transfer
        return self._finish_submission(transfer_id, status)

    # ------------------------------------------------------------------
    # Expiry and cancellation
    # ------------------------------------------------------------------
    def cancel(self, transfer_id: str, reason: str = "customer_request") -> Transfer:
        with self.audit.unit_of_work() as db:
            transfer = self._get(db, transfer_id)
            if transfer.state in {
                TransferState.SETTLED.value,
                TransferState.FAILED.value,
                TransferState.CANCELLED.value,
                TransferState.EXPIRED.value,
                TransferState.SUBMITTED.value,
                TransferState.PENDING_UNKNOWN.value,
            }:
                return transfer
            transfer.state = ensure_transition(transfer.state, TransferState.CANCELLED).value
            self.audit.record(
                db,
                "transfer.cancelled",
                transfer_id=transfer.id,
                session_id=transfer.session_id,
                details={"reason": reason},
            )
            return transfer

    def expire_due(self, session_id: str | None = None) -> list[Transfer]:
        now = utcnow()
        with self.audit.unit_of_work() as db:
            query = select(Transfer).where(
                Transfer.state.in_(
                    [
                        TransferState.DRAFTED.value,
                        TransferState.READBACK_PLAYED.value,
                        TransferState.CONFIRMED_BY_VOICE.value,
                        TransferState.AWAITING_BIOMETRIC.value,
                    ]
                ),
                Transfer.expires_at < now,
            )
            if session_id:
                query = query.where(Transfer.session_id == session_id)
            rows = db.scalars(query).all()
            for transfer in rows:
                previous = transfer.state
                transfer.state = ensure_transition(transfer.state, TransferState.EXPIRED).value
                self.audit.record(
                    db,
                    "transfer.expired",
                    transfer_id=transfer.id,
                    session_id=transfer.session_id,
                    details={"from": previous},
                )
            return rows

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, transfer_id: str) -> Transfer:
        with self.session_factory() as db:
            return self._get(db, transfer_id)

    def active_for_session(self, session_id: str) -> Transfer | None:
        with self.session_factory() as db:
            return db.scalar(
                select(Transfer)
                .where(
                    Transfer.session_id == session_id,
                    Transfer.state.notin_(
                        [
                            TransferState.SETTLED.value,
                            TransferState.FAILED.value,
                            TransferState.CANCELLED.value,
                            TransferState.EXPIRED.value,
                        ]
                    ),
                )
                .order_by(Transfer.created_at.desc())
                .limit(1)
            )

    def history(self, user_id: str, start: datetime, end: datetime, limit: int = 50) -> list[Transfer]:
        with self.session_factory() as db:
            return list(
                db.scalars(
                    select(Transfer)
                    .where(
                        Transfer.user_id == user_id,
                        Transfer.created_at >= start,
                        Transfer.created_at <= end,
                    )
                    .order_by(Transfer.created_at.desc())
                    .limit(max(1, min(100, limit)))
                ).all()
            )

    def latest_for_user(self, user_id: str) -> Transfer | None:
        with self.session_factory() as db:
            return db.scalar(
                select(Transfer)
                .where(Transfer.user_id == user_id)
                .order_by(Transfer.created_at.desc())
                .limit(1)
            )

    def pending_for_reconciliation(self, older_than_seconds: int = 5) -> list[Transfer]:
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        with self.session_factory() as db:
            return list(
                db.scalars(
                    select(Transfer)
                    .where(
                        Transfer.state.in_(
                            [TransferState.SUBMITTED.value, TransferState.PENDING_UNKNOWN.value]
                        ),
                        Transfer.submitted_at.is_not(None),
                        Transfer.submitted_at < cutoff,
                    )
                    .limit(20)
                ).all()
            )

    @staticmethod
    def _get(db: Session, transfer_id: str) -> Transfer:
        transfer = db.get(Transfer, transfer_id)
        if transfer is None:
            raise TransferNotFound(transfer_id)
        return transfer

    # ------------------------------------------------------------------
    # Presentation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def summary(transfer: Transfer) -> dict:
        return {
            "id": transfer.id,
            "state": transfer.state,
            "display": display_state(transfer.state),
            "amount_kobo": transfer.amount_kobo,
            "amount_display": format_naira(transfer.amount_kobo),
            "bank_code": transfer.bank_code,
            "bank_name": BANK_NAMES.get(transfer.bank_code, transfer.bank_code),
            "account_last4": transfer.account_number[-4:],
            "recipient": transfer.verified_recipient_name,
            "reference": transfer.bank_reference,
            "risk_flags": transfer.risk_flags or [],
            "created_at": iso(transfer.created_at),
            "settled_at": iso(transfer.settled_at),
            "failure_reason": transfer.failure_reason,
        }
