from __future__ import annotations

import asyncio
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..models import BankAccount, BankControl, BankTransfer
from ..timeutil import utcnow
from .adapter import AccountInfo, BankAdapter, BankNotFound, BankResult, BankTimeout, BankUnavailable

BANK_NAMES = {
    "058": "GTBank",
    "044": "Access Bank",
    "057": "Zenith Bank",
    "011": "First Bank",
    "033": "UBA",
    "50211": "Kuda",
    "999992": "OPay",
    "50515": "Moniepoint",
    "999991": "PalmPay",
    "035": "Wema Bank",
    "232": "Sterling Bank",
    "070": "Fidelity Bank",
    "032": "Union Bank",
    "076": "Polaris Bank",
    "101": "Providus Bank",
}

DEMO_ACCOUNTS = [
    ("058", "0123456789", "Aisha Bello"),
    ("058", "0987654321", "Aisha Bello-Okafor"),
    ("044", "1111222333", "Chinedu Okafor"),
    ("057", "5555666777", "Fatima Yusuf"),
    ("50211", "9999000011", "Muiz Adeyemi"),
    ("011", "2222333344", "Ngozi Eze"),
    ("999992", "3333444455", "Tunde Bakare"),
    ("50515", "4444555566", "Zainab Musa"),
]

BANK_MODES = {
    "normal",
    "fail",
    "pending",
    "timeout_before_commit",
    "timeout_after_commit",
    "slow",
    "unavailable",
    "name_unavailable",
}


def seed_mock_bank(session_factory: sessionmaker) -> None:
    with session_factory() as db:
        for bank_code, account_number, name in DEMO_ACCOUNTS:
            existing = db.scalar(
                select(BankAccount).where(
                    BankAccount.bank_code == bank_code,
                    BankAccount.account_number == account_number,
                )
            )
            if existing is None:
                db.add(
                    BankAccount(
                        id=f"acct_{uuid.uuid4().hex}",
                        bank_code=bank_code,
                        bank_name=BANK_NAMES.get(bank_code, bank_code),
                        account_number=account_number,
                        account_name=name,
                    )
                )
        control = db.get(BankControl, 1)
        if control is None:
            db.add(BankControl(id=1, mode="normal", delay_ms=0))
        db.commit()


class MockBank(BankAdapter):
    """Local simulated bank. Clearly labelled as simulated in /health and UI.

    The mock keeps its own ledger with a unique idempotency key, so tests can
    prove that a double tap or a lost reply produces exactly one payment.
    """

    name = "mock"
    simulated = True

    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory

    def _control(self, db) -> BankControl:
        control = db.get(BankControl, 1)
        if control is None:
            control = BankControl(id=1, mode="normal", delay_ms=0)
            db.add(control)
            db.commit()
        return control

    def set_mode(self, mode: str, delay_ms: int | None = None) -> None:
        if mode not in BANK_MODES:
            raise ValueError(f"unknown bank mode {mode}")
        with self.session_factory() as db:
            control = self._control(db)
            control.mode = mode
            if delay_ms is not None:
                control.delay_ms = max(0, min(60_000, int(delay_ms)))
            control.updated_at = utcnow()
            db.commit()

    def get_mode(self) -> dict:
        with self.session_factory() as db:
            control = self._control(db)
            return {"mode": control.mode, "delay_ms": control.delay_ms}

    async def name_enquiry(self, bank_code: str, account_number: str) -> AccountInfo:
        with self.session_factory() as db:
            control = self._control(db)
            if control.mode == "name_unavailable":
                raise BankUnavailable("name enquiry unavailable (simulated)")
            account = db.scalar(
                select(BankAccount).where(
                    BankAccount.bank_code == bank_code,
                    BankAccount.account_number == account_number,
                )
            )
            if account is None:
                raise BankNotFound("account not found (simulated)")
            return AccountInfo(
                bank_code=account.bank_code,
                bank_name=account.bank_name,
                account_number=account.account_number,
                account_name=account.account_name,
            )

    async def create_transfer(
        self,
        *,
        idempotency_key: str,
        customer_ref: str,
        amount_kobo: int,
        bank_code: str,
        account_number: str,
        recipient_name: str,
        nonce: str,
    ) -> BankResult:
        # Idempotency first: a repeat returns the first result, never a second payment.
        with self.session_factory() as db:
            existing = db.scalar(select(BankTransfer).where(BankTransfer.idempotency_key == idempotency_key))
            if existing is not None:
                return self._to_result(existing)

        with self.session_factory() as db:
            control = self._control(db)
            mode = control.mode
            delay_ms = control.delay_ms

        if mode == "unavailable":
            raise BankUnavailable("bank unavailable (simulated)")
        if mode == "timeout_before_commit":
            raise BankTimeout("bank timed out before committing (simulated)")
        if mode == "slow" and delay_ms:
            await asyncio.sleep(delay_ms / 1000)

        status = "SETTLED"
        failure = None
        if mode == "fail":
            status = "FAILED"
            failure = "insufficient_funds"
        elif mode == "pending":
            status = "PENDING"

        now = utcnow()
        reference = "AVZ" + secrets.token_hex(5).upper()
        row = BankTransfer(
            id=f"bt_{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            customer_ref=customer_ref,
            bank_code=bank_code,
            account_number=account_number,
            recipient_name=recipient_name,
            amount_kobo=amount_kobo,
            status=status,
            reference=reference,
            failure_reason=failure,
            created_at=now,
            settled_at=now if status == "SETTLED" else None,
        )
        try:
            with self.session_factory() as db:
                db.add(row)
                db.commit()
        except IntegrityError:
            # Concurrent duplicate: return the winner.
            with self.session_factory() as db:
                existing = db.scalar(select(BankTransfer).where(BankTransfer.idempotency_key == idempotency_key))
                if existing is None:
                    raise
                return self._to_result(existing)

        result = self._to_result(row)
        if mode == "timeout_after_commit":
            # The payment committed but the reply was lost. The caller must
            # check status; it must not retry blindly.
            raise BankTimeout("bank committed but the reply was lost (simulated)")
        return result

    async def get_transfer_status(
        self, reference: str | None = None, idempotency_key: str | None = None
    ) -> BankResult:
        with self.session_factory() as db:
            row = None
            if reference:
                row = db.scalar(select(BankTransfer).where(BankTransfer.reference == reference))
            if row is None and idempotency_key:
                row = db.scalar(select(BankTransfer).where(BankTransfer.idempotency_key == idempotency_key))
            if row is None:
                return BankResult(status="NOT_FOUND", reference=None)
            control = self._control(db)
            if row.status == "PENDING" and control.mode == "pending":
                settle_after = row.created_at + timedelta(milliseconds=max(0, control.delay_ms))
                if utcnow() >= settle_after:
                    row.status = "SETTLED"
                    row.settled_at = utcnow()
                    db.commit()
            return self._to_result(row)

    async def list_transactions(
        self, customer_ref: str, start: datetime, end: datetime, limit: int = 50
    ) -> list[BankResult]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(BankTransfer)
                .where(
                    BankTransfer.customer_ref == customer_ref,
                    BankTransfer.created_at >= start,
                    BankTransfer.created_at <= end,
                )
                .order_by(BankTransfer.created_at.desc())
                .limit(max(1, min(100, limit)))
            ).all()
            return [self._to_result(r) for r in rows]

    async def get_receipt(self, reference: str) -> dict | None:
        with self.session_factory() as db:
            row = db.scalar(select(BankTransfer).where(BankTransfer.reference == reference))
            if row is None:
                return None
            return {
                "reference": row.reference,
                "status": row.status,
                "amount_kobo": row.amount_kobo,
                "bank_code": row.bank_code,
                "bank_name": BANK_NAMES.get(row.bank_code, row.bank_code),
                "account_number": row.account_number,
                "recipient_name": row.recipient_name,
                "created_at": row.created_at.isoformat(),
                "settled_at": row.settled_at.isoformat() if row.settled_at else None,
                "failure_reason": row.failure_reason,
                "simulated": True,
            }

    @staticmethod
    def _to_result(row: BankTransfer) -> BankResult:
        return BankResult(
            status=row.status,
            reference=row.reference,
            amount_kobo=row.amount_kobo,
            account_number=row.account_number,
            recipient_name=row.recipient_name,
            failure_reason=row.failure_reason,
            raw={
                "id": row.id,
                "bank_code": row.bank_code,
                "created_at": row.created_at.isoformat(),
                "settled_at": row.settled_at.isoformat() if row.settled_at else None,
                "simulated": True,
            },
        )
