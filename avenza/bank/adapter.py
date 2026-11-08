from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AccountInfo:
    bank_code: str
    bank_name: str
    account_number: str
    account_name: str


@dataclass(frozen=True)
class BankResult:
    status: str  # SETTLED | FAILED | PENDING | NOT_FOUND
    reference: str | None
    amount_kobo: int = 0
    account_number: str = ""
    recipient_name: str = ""
    failure_reason: str | None = None
    raw: dict | None = None


class BankError(RuntimeError):
    code = "bank_error"


class BankNotFound(BankError):
    code = "account_not_found"


class BankTimeout(BankError):
    """The outcome is unknown. The caller must check status before any retry."""

    code = "bank_timeout"


class BankUnavailable(BankError):
    code = "bank_unavailable"


class BankAdapter(ABC):
    name: str = "base"
    simulated: bool = False

    @abstractmethod
    async def name_enquiry(self, bank_code: str, account_number: str) -> AccountInfo: ...

    @abstractmethod
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
    ) -> BankResult: ...

    @abstractmethod
    async def get_transfer_status(
        self, reference: str | None = None, idempotency_key: str | None = None
    ) -> BankResult: ...

    @abstractmethod
    async def list_transactions(
        self, customer_ref: str, start: datetime, end: datetime, limit: int = 50
    ) -> list[BankResult]: ...

    @abstractmethod
    async def get_receipt(self, reference: str) -> dict | None: ...
