from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .bank import BANK_NAMES, BankAdapter, BankNotFound, BankUnavailable
from .config import Settings
from .intent import Intent, IntentClassifier
from .money import format_naira
from .parser import ParseError, extract_transfer_fields, normalize, parse_history_query
from .prompts import PROMPTS, PromptLibrary
from .services import SessionService, TransferLocked, TransferService
from .state_machine import TransferState, display_state
from .timeutil import utcnow

logger = logging.getLogger("avenza.orchestrator")

SendEvents = Callable[[list[dict]], Awaitable[None]]


@dataclass
class SessionContext:
    session_id: str
    user_id: str
    device_id: str
    language: str
    display_name: str = ""
    active_transfer_id: str | None = None
    accumulator: dict = field(default_factory=dict)
    collecting: bool = False
    unclear_count: int = 0
    approval_failures: int = 0
    last_status_check: float = 0.0
    send: SendEvents | None = None
    hold_task: asyncio.Task | None = None
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TurnOrchestrator:
    def __init__(
        self,
        settings: Settings,
        prompts: PromptLibrary,
        stt,
        tts,
        intent: IntentClassifier,
        bank: BankAdapter,
        transfers: TransferService,
        sessions: SessionService,
    ):
        self.settings = settings
        self.prompts = prompts
        self.stt = stt
        self.tts = tts
        self.intent = intent
        self.bank = bank
        self.transfers = transfers
        self.sessions = sessions

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------
    def _prompt(self, ctx: SessionContext, prompt_id: str, **kwargs) -> dict:
        prompt = self.prompts.render(prompt_id, language=ctx.language, **kwargs)
        return {
            "type": "prompt.play",
            "id": prompt.id,
            "text": prompt.text,
            "language": prompt.language,
            "audio_url": prompt.audio_url,
        }

    @staticmethod
    def _state(transfer) -> dict:
        return {
            "type": "state.changed",
            "transfer_id": transfer.id,
            "state": transfer.state,
            "display": display_state(transfer.state),
            "amount_display": format_naira(transfer.amount_kobo),
            "recipient": transfer.verified_recipient_name,
            "reference": transfer.bank_reference,
        }

    @staticmethod
    def _listening() -> dict:
        return {"type": "listening", "tone": True}

    @staticmethod
    def _error(code: str, prompt_id: str, message: str) -> dict:
        return {"type": "error", "code": code, "prompt_id": prompt_id, "message": message}

    def _result_prompt(self, ctx: SessionContext, transfer) -> dict:
        if transfer.state == TransferState.SETTLED.value:
            return self._prompt(
                ctx,
                "settled",
                amount_display=format_naira(transfer.amount_kobo),
                recipient=transfer.verified_recipient_name,
                reference=transfer.bank_reference or "",
            )
        if transfer.state == TransferState.FAILED.value:
            reason = {
                "insufficient_funds": "The bank declined it for insufficient funds.",
                "bank_declined": "The bank declined it.",
            }.get(transfer.failure_reason or "", "The bank reported a failure.")
            return self._prompt(ctx, "failed", reason=reason)
        if transfer.state == TransferState.PENDING_UNKNOWN.value:
            return self._prompt(ctx, "pending")
        if transfer.state == TransferState.CANCELLED.value:
            return self._prompt(ctx, "cancelled")
        if transfer.state == TransferState.EXPIRED.value:
            return self._prompt(ctx, "expired")
        return self._prompt(ctx, "wait_status")

    def _clear_active(self, ctx: SessionContext, transfer_id: str | None = None) -> None:
        if transfer_id is None or ctx.active_transfer_id == transfer_id:
            ctx.active_transfer_id = None
        ctx.accumulator = {}
        ctx.collecting = False
        ctx.unclear_count = 0
        ctx.approval_failures = 0

    def _expire_events(self, ctx: SessionContext) -> list[dict]:
        events: list[dict] = []
        for transfer in self.transfers.expire_due(session_id=ctx.session_id):
            events.append(self._state(transfer))
            events.append(self._prompt(ctx, "expired"))
            if ctx.active_transfer_id == transfer.id:
                self._clear_active(ctx, transfer.id)
        return events

    # ------------------------------------------------------------------
    # Session start
    # ------------------------------------------------------------------
    async def start_events(self, ctx: SessionContext) -> list[dict]:
        active = self.transfers.active_for_session(ctx.session_id)
        if active is None:
            return [self._prompt(ctx, "session_ready"), self._listening()]
        ctx.active_transfer_id = active.id
        if active.state in {
            TransferState.DRAFTED.value,
            TransferState.READBACK_PLAYED.value,
        }:
            return [
                self._state(active),
                self._prompt(ctx, "readback", **_readback_fields(active)),
                self._listening(),
            ]
        if active.state == TransferState.CONFIRMED_BY_VOICE.value:
            return await self._begin_approval(ctx, active.id)
        if active.state == TransferState.AWAITING_BIOMETRIC.value:
            return await self._resume_approval(ctx, active.id)
        if active.state in {
            TransferState.SUBMITTED.value,
            TransferState.PENDING_UNKNOWN.value,
        }:
            return [self._state(active), self._prompt(ctx, "pending"), self._listening()]
        return [self._prompt(ctx, "session_ready"), self._listening()]

    async def _resume_approval(self, ctx: SessionContext, transfer_id: str) -> list[dict]:
        try:
            options = self.transfers.reissue_approval(transfer_id, ctx.device_id)
        except Exception:
            return [self._prompt(ctx, "approval_required"), self._listening()]
        transfer = self.transfers.get(transfer_id)
        return [
            self._state(transfer),
            self._prompt(ctx, "confirm_voice"),
            {
                "type": "approval.request",
                "transfer_id": transfer.id,
                "options": options,
                "transfer": TransferService.summary(transfer),
                "expires_in_s": self.settings.approval_ttl_s,
            },
        ]

    # ------------------------------------------------------------------
    # Audio / text
    # ------------------------------------------------------------------
    async def process_audio(
        self,
        ctx: SessionContext,
        pcm: bytes,
        sample_rate: int,
        provided_text: str | None = None,
    ) -> list[dict]:
        if provided_text and not self.settings.allow_text_utterance:
            return [
                self._error("text_not_allowed", "error_stt", "text utterances are disabled"),
                self._prompt(ctx, "error_stt"),
                self._listening(),
            ]
        result = await self.stt.transcribe(
            pcm, sample_rate, language_hint=ctx.language, provided_text=provided_text
        )
        events: list[dict] = [
            {
                "type": "transcript.final",
                "text": result.text,
                "confidence": result.confidence,
                "provider": result.provider,
                "simulated": result.simulated,
                "timings": {"ttfp_ms": result.ttfp_ms, "final_ms": result.final_ms},
            }
        ]
        if result.is_empty:
            events.append(self._error("no_speech", "no_speech", "no speech detected"))
            events.append(self._prompt(ctx, "no_speech"))
            events.append(self._listening())
            return events
        if result.confidence and result.confidence < 0.45 and not result.simulated:
            events.append(self._error("low_confidence", "unclear", "low confidence transcript"))
            events.append(self._prompt(ctx, "unclear"))
            events.append(self._listening())
            return events
        logger.info(
            "stt provider=%s final_ms=%s ttfp_ms=%s chars=%s",
            result.provider,
            result.final_ms,
            result.ttfp_ms,
            len(result.text),
        )
        events.extend(await self.process_text(ctx, result.text))
        return events

    async def process_text(self, ctx: SessionContext, text: str) -> list[dict]:
        async with ctx.lock:
            ctx.unclear_count = max(0, ctx.unclear_count)
            events = self._expire_events(ctx)
            active = self.transfers.active_for_session(ctx.session_id)
            if active is not None:
                ctx.active_transfer_id = active.id
            else:
                # Keep any partial field collection alive across turns; only
                # drop the pointer to a transfer that no longer exists.
                ctx.active_transfer_id = None

            if active is None:
                events.extend(await self._handle_no_active(ctx, text))
                return events

            state = active.state
            if state in {
                TransferState.DRAFTED.value,
                TransferState.READBACK_PLAYED.value,
            }:
                events.extend(await self._handle_readback_reply(ctx, active, text))
                return events

            if state == TransferState.CONFIRMED_BY_VOICE.value:
                result = await self.intent.classify(text, state=state, language=ctx.language)
                if result.intent in {Intent.NO, Intent.CANCEL}:
                    transfer = self.transfers.cancel(active.id, reason="voice_cancel")
                    events.append(self._state(transfer))
                    events.append(self._prompt(ctx, "cancelled"))
                    self._clear_active(ctx, active.id)
                    events.append(self._listening())
                    return events
                events.extend(await self._begin_approval(ctx, active.id))
                return events

            if state == TransferState.AWAITING_BIOMETRIC.value:
                result = await self.intent.classify(text, state=state, language=ctx.language)
                if result.intent in {Intent.NO, Intent.CANCEL}:
                    transfer = self.transfers.cancel(active.id, reason="voice_cancel")
                    events.append(self._state(transfer))
                    events.append(self._prompt(ctx, "cancelled"))
                    self._clear_active(ctx, active.id)
                    events.append(self._listening())
                    return events
                events.append(self._error("approval_required", "approval_required", "voice cannot approve"))
                events.append(self._prompt(ctx, "approval_required"))
                events.append(self._listening())
                return events

            if state in {TransferState.SUBMITTED.value, TransferState.PENDING_UNKNOWN.value}:
                now = time.monotonic()
                if now - ctx.last_status_check > 3:
                    ctx.last_status_check = now
                    updated = await self.transfers.check_status(active.id)
                    events.append(self._state(updated))
                    events.append(self._result_prompt(ctx, updated))
                    if updated.state in {
                        TransferState.SETTLED.value,
                        TransferState.FAILED.value,
                    }:
                        self._clear_active(ctx, updated.id)
                else:
                    events.append(self._prompt(ctx, "payment_in_progress"))
                events.append(self._listening())
                return events

            events.append(self._prompt(ctx, "payment_in_progress"))
            events.append(self._listening())
            return events

    # ------------------------------------------------------------------
    # No active transfer
    # ------------------------------------------------------------------
    async def _handle_no_active(self, ctx: SessionContext, text: str) -> list[dict]:
        result = await self.intent.classify(text, state=None, language=ctx.language)
        events: list[dict] = []

        if result.intent == Intent.LOCK:
            self.transfers.engage_lock(ctx.user_id, reason="voice_request")
            self._clear_active(ctx)
            events.append({"type": "lock.changed", "locked": True, "reason": "voice_request"})
            events.append(self._prompt(ctx, "locked"))
            events.append(self._listening())
            return events

        if result.intent == Intent.HISTORY:
            self._clear_active(ctx)
            return self._history_events(ctx, text)

        if result.intent == Intent.HELP:
            self._clear_active(ctx)
            events.append(self._prompt(ctx, "help"))
            events.append(self._listening())
            return events

        if result.intent == Intent.TRANSFER:
            return await self._handle_transfer_request(ctx, text)

        # Multi-turn field collection: if we already have part of a draft, a
        # bare answer such as "GTBank" or "0123456789" is a field, not noise.
        if (ctx.accumulator or ctx.collecting) and result.intent == Intent.UNCLEAR:
            return await self._handle_transfer_request(ctx, text)

        if result.intent in {Intent.YES, Intent.NO, Intent.REPEAT, Intent.CANCEL}:
            events.append(self._prompt(ctx, "no_draft"))
            events.append(self._listening())
            return events

        events.append(self._prompt(ctx, "unclear"))
        events.append(self._listening())
        return events

    # ------------------------------------------------------------------
    # Draft creation
    # ------------------------------------------------------------------
    async def _handle_transfer_request(self, ctx: SessionContext, text: str) -> list[dict]:
        events: list[dict] = []
        fields = extract_transfer_fields(text, max_kobo=self.settings.max_amount_kobo)
        acc = ctx.accumulator

        if fields.amount is not None:
            acc["amount"] = fields.amount
            acc.pop("amount_error", None)
        elif fields.amount_error in {
            "ambiguous_amount",
            "invalid_amount",
            "amount_too_large",
            "unsupported_currency",
        }:
            acc.pop("amount", None)
            acc["amount_error"] = fields.amount_error

        if fields.account is not None:
            acc["account"] = fields.account
            acc.pop("account_error", None)
        elif fields.account_error == "ambiguous_account":
            acc.pop("account", None)
            acc["account_error"] = "ambiguous_account"

        if fields.bank is not None:
            acc["bank"] = fields.bank
        if fields.recipient_hint:
            acc["hint"] = fields.recipient_hint

        if acc.get("amount_error") == "amount_too_large":
            acc.pop("amount_error", None)
            events.append(
                self._error("amount_too_large", "amount_too_large", "amount above configured limit")
            )
            events.append(self._prompt(ctx, "amount_too_large", limit=format_naira(self.settings.max_amount_kobo)))
            events.append(self._listening())
            return events
        if acc.get("amount_error") in {"ambiguous_amount", "invalid_amount", "unsupported_currency"}:
            acc.pop("amount_error", None)
            events.append(self._error("ambiguous_amount", "clarify_amount", "unclear amount"))
            events.append(self._prompt(ctx, "clarify_amount"))
            events.append(self._listening())
            return events
        if acc.get("account_error") == "ambiguous_account":
            acc.pop("account_error", None)
            events.append(self._error("ambiguous_account", "clarify_account", "unclear account number"))
            events.append(self._prompt(ctx, "clarify_account"))
            events.append(self._listening())
            return events

        missing = [key for key in ("amount", "account", "bank") if key not in acc]
        if missing:
            ctx.collecting = True
            if len(missing) == 3:
                events.append(self._prompt(ctx, "ask_all"))
            elif missing[0] == "amount":
                events.append(self._prompt(ctx, "ask_amount"))
            elif missing[0] == "account":
                events.append(self._prompt(ctx, "ask_account"))
            else:
                events.append(self._prompt(ctx, "ask_bank"))
            events.append(self._listening())
            return events

        amount = acc["amount"]
        account = acc["account"]
        bank = acc["bank"]
        hint = acc.get("hint")

        started = time.perf_counter()
        try:
            info = await self.bank.name_enquiry(bank.code, account.value)
        except BankNotFound:
            acc.pop("account", None)
            self.transfers.audit.record_standalone(
                "name.not_found",
                details={"bank_code": bank.code, "account_last4": account.value[-4:]},
            )
            events.append(self._error("account_not_found", "name_not_found", "account not found"))
            events.append(self._prompt(ctx, "name_not_found", bank=bank.name))
            events.append(self._listening())
            return events
        except BankUnavailable:
            events.append(self._error("bank_unavailable", "error_bank", "name enquiry unavailable"))
            events.append(self._prompt(ctx, "error_bank"))
            events.append(self._listening())
            return events
        except Exception:
            logger.exception("name enquiry failed")
            events.append(self._error("bank_error", "error_bank", "name enquiry failed"))
            events.append(self._prompt(ctx, "error_bank"))
            events.append(self._listening())
            return events

        name_ms = int((time.perf_counter() - started) * 1000)
        try:
            transfer = self.transfers.create_draft(
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                amount_kobo=amount.kobo,
                bank_code=bank.code,
                account_number=account.value,
                verified_name=info.account_name,
                hint=hint,
                language=ctx.language,
            )
        except TransferLocked:
            events.append(self._error("transfers_locked", "locked", "transfers locked"))
            events.append(self._prompt(ctx, "locked"))
            events.append(self._listening())
            return events

        transfer = self.transfers.mark_readback(transfer.id)
        self.transfers.audit.record_standalone(
            "name.verified",
            transfer_id=transfer.id,
            session_id=ctx.session_id,
            details={
                "bank_code": bank.code,
                "account_last4": account.value[-4:],
                "verified_name": info.account_name,
                "name_enquiry_ms": name_ms,
            },
        )
        logger.info(
            "latency name_enquiry_ms=%s amount_kobo=%s state=%s",
            name_ms,
            transfer.amount_kobo,
            transfer.state,
        )

        ctx.active_transfer_id = transfer.id
        ctx.accumulator = {}
        ctx.collecting = False
        ctx.unclear_count = 0
        events.append(self._state(transfer))
        events.append(self._prompt(ctx, "readback", **_readback_fields(transfer)))
        events.append(self._listening())
        return events

    # ------------------------------------------------------------------
    # Read-back reply
    # ------------------------------------------------------------------
    async def _handle_readback_reply(self, ctx: SessionContext, transfer, text: str) -> list[dict]:
        events: list[dict] = []
        result = await self.intent.classify(text, state=transfer.state, language=ctx.language)

        if result.intent in {Intent.NO, Intent.CANCEL}:
            cancelled = self.transfers.cancel(transfer.id, reason="voice_no")
            events.append(self._state(cancelled))
            events.append(self._prompt(ctx, "cancelled"))
            self._clear_active(ctx, transfer.id)
            events.append(self._listening())
            return events

        if result.intent == Intent.REPEAT:
            events.append(self._state(transfer))
            events.append(self._prompt(ctx, "readback", **_readback_fields(transfer)))
            events.append(self._listening())
            ctx.unclear_count = 0
            return events

        if result.intent == Intent.YES:
            ctx.unclear_count = 0
            outcome = self.transfers.record_confirmation(transfer.id, Intent.YES)
            if outcome.outcome == "more_confirmation":
                events.append(self._state(outcome.transfer))
                events.append(self._prompt(ctx, "readback", **_readback_fields(outcome.transfer)))
                events.append(self._listening())
                return events
            return await self._begin_approval(ctx, transfer.id)

        # A new transfer request while a draft is active must never create a
        # second draft. Repeat the existing read-back instead.
        if result.intent == Intent.TRANSFER:
            events.append(self._error("draft_active", "draft_active", "a draft is already active"))
            events.append(self._prompt(ctx, "draft_active"))
            events.append(self._listening())
            return events

        ctx.unclear_count += 1
        if ctx.unclear_count >= 2:
            cancelled = self.transfers.cancel(transfer.id, reason="two_unclear_replies")
            events.append(self._state(cancelled))
            events.append(self._prompt(ctx, "too_many_attempts"))
            self._clear_active(ctx, transfer.id)
        else:
            events.append(self._error("unclear", "unclear", "unclear confirmation"))
            events.append(self._prompt(ctx, "unclear"))
        events.append(self._listening())
        return events

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------
    async def _begin_approval(self, ctx: SessionContext, transfer_id: str) -> list[dict]:
        events: list[dict] = []
        try:
            options, hold_seconds, transfer = self.transfers.issue_approval(transfer_id, ctx.device_id)
        except TransferLocked:
            events.append(self._error("transfers_locked", "locked", "transfers locked"))
            events.append(self._prompt(ctx, "locked"))
            events.append(self._listening())
            return events
        except Exception as exc:
            logger.warning("approval issue failed: %s", exc)
            events.append(self._error("approval_error", "fingerprint_retry", "approval could not start"))
            events.append(self._prompt(ctx, "fingerprint_retry"))
            events.append(self._listening())
            return events

        if options is None and hold_seconds > 0:
            events.append(self._state(transfer))
            events.append(self._prompt(ctx, "risk_hold", seconds=hold_seconds))
            if ctx.send and not ctx.closed:
                if ctx.hold_task and not ctx.hold_task.done():
                    ctx.hold_task.cancel()
                ctx.hold_task = asyncio.create_task(self._delayed_approval(ctx, transfer_id, hold_seconds))
            events.append(self._listening())
            return events

        events.append(self._state(transfer))
        events.append(self._prompt(ctx, "confirm_voice"))
        events.append(
            {
                "type": "approval.request",
                "transfer_id": transfer.id,
                "options": options,
                "transfer": TransferService.summary(transfer),
                "expires_in_s": self.settings.approval_ttl_s,
            }
        )
        return events

    async def _delayed_approval(self, ctx: SessionContext, transfer_id: str, seconds: int) -> None:
        try:
            await asyncio.sleep(max(0, seconds))
            if ctx.closed:
                return
            events = await self._begin_approval(ctx, transfer_id)
            if ctx.send:
                await ctx.send(events)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("delayed approval failed")

    async def handle_approval(
        self, ctx: SessionContext, credential: dict | None, *, dev: bool = False
    ) -> list[dict]:
        async with ctx.lock:
            active = self.transfers.active_for_session(ctx.session_id)
            if active is None:
                return [self._prompt(ctx, "no_draft"), self._listening()]
            if active.state != TransferState.AWAITING_BIOMETRIC.value:
                return [self._prompt(ctx, "approval_required"), self._listening()]

            try:
                transfer = await self.transfers.approve_and_submit(
                    active.id,
                    ctx.device_id,
                    credential,
                    dev_approval=dev,
                )
            except TransferLocked:
                return [
                    self._error("transfers_locked", "locked", "transfers locked"),
                    self._prompt(ctx, "locked"),
                    self._listening(),
                ]
            except Exception as exc:
                logger.info("approval rejected: %s", exc)
                transfer = self.transfers.get(active.id)
                if transfer.state == TransferState.EXPIRED.value:
                    self._clear_active(ctx, transfer.id)
                    return [
                        self._state(transfer),
                        self._prompt(ctx, "approval_expired"),
                        self._listening(),
                    ]
                ctx.approval_failures += 1
                if ctx.approval_failures >= 3:
                    cancelled = self.transfers.cancel(transfer.id, reason="too_many_failed_reads")
                    self._clear_active(ctx, transfer.id)
                    return [
                        self._state(cancelled),
                        self._prompt(ctx, "too_many_attempts"),
                        self._listening(),
                    ]
                # Fresh challenge; the client prompts the fingerprint again.
                try:
                    options = self.transfers.reissue_approval(transfer.id, ctx.device_id)
                except Exception:
                    return [
                        self._state(transfer),
                        self._prompt(ctx, "fingerprint_retry"),
                        self._listening(),
                    ]
                return [
                    self._state(transfer),
                    self._prompt(ctx, "fingerprint_retry"),
                    {
                        "type": "approval.request",
                        "transfer_id": transfer.id,
                        "options": options,
                        "transfer": TransferService.summary(transfer),
                        "expires_in_s": self.settings.approval_ttl_s,
                    },
                ]

            events = [
                self._state(transfer),
                self._result_prompt(ctx, transfer),
            ]
            if transfer.state in {
                TransferState.SETTLED.value,
                TransferState.FAILED.value,
                TransferState.PENDING_UNKNOWN.value,
            }:
                self._clear_active(ctx, transfer.id)
            events.append(self._listening())
            return events

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    def _history_events(self, ctx: SessionContext, text: str) -> list[dict]:
        query = parse_history_query(text)
        rows = self.transfers.history(ctx.user_id, query.start, query.end, query.limit)
        events: list[dict] = [
            {
                "type": "history.data",
                "period": query.label,
                "transactions": [TransferService.summary(r) for r in rows],
            }
        ]
        if not rows:
            events.append(self._prompt(ctx, "history_empty", period=query.label))
            events.append(self._listening())
            return events

        if "receipt" in normalize(text):
            latest = rows[0]
            events.append(
                self._prompt(
                    ctx,
                    "receipt",
                    amount_display=format_naira(latest.amount_kobo),
                    recipient=latest.verified_recipient_name,
                    status=display_state(latest.state),
                    reference=latest.bank_reference or "not available",
                )
            )
            events.append(self._listening())
            return events

        settled_total = sum(r.amount_kobo for r in rows if r.state == TransferState.SETTLED.value)
        latest = rows[0]
        summary = (
            f"{format_naira(settled_total)} settled. Latest: {latest.verified_recipient_name} "
            f"{format_naira(latest.amount_kobo)}."
        )
        events.append(
            self._prompt(
                ctx,
                "history_summary",
                count=len(rows),
                period=query.label,
                summary=summary,
            )
        )
        events.append(self._listening())
        return events

    # ------------------------------------------------------------------
    # Periodic tick
    # ------------------------------------------------------------------
    async def tick(self, ctx: SessionContext) -> list[dict]:
        events = self._expire_events(ctx)
        active = self.transfers.active_for_session(ctx.session_id)
        if active is not None and active.state in {
            TransferState.SUBMITTED.value,
            TransferState.PENDING_UNKNOWN.value,
        }:
            now = time.monotonic()
            if now - ctx.last_status_check > 5:
                ctx.last_status_check = now
                updated = await self.transfers.check_status(active.id)
                if updated.state != active.state:
                    events.append(self._state(updated))
                    events.append(self._result_prompt(ctx, updated))
                    if updated.state in {
                        TransferState.SETTLED.value,
                        TransferState.FAILED.value,
                    }:
                        self._clear_active(ctx, updated.id)
        return events


def _readback_fields(transfer) -> dict:
    return {
        "amount_words": _amount_words(transfer.amount_kobo),
        "amount_display": format_naira(transfer.amount_kobo),
        "recipient": transfer.verified_recipient_name,
        "last4": transfer.account_number[-4:],
        "bank": BANK_NAMES.get(transfer.bank_code, transfer.bank_code),
    }


def _amount_words(kobo: int) -> str:
    from .money import amount_to_words

    return amount_to_words(kobo)
