from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from ..config import Settings
from .adapter import (
    AccountInfo,
    BankAdapter,
    BankError,
    BankNotFound,
    BankResult,
    BankTimeout,
    BankUnavailable,
)
from .mock import BANK_NAMES, BANK_MODES, DEMO_ACCOUNTS, MockBank, seed_mock_bank
from .sandbox import SandboxBank


def build_bank(settings: Settings, session_factory: sessionmaker) -> BankAdapter:
    if settings.bank_mode == "sandbox":
        return SandboxBank(settings)
    return MockBank(session_factory)


__all__ = [
    "AccountInfo",
    "BankAdapter",
    "BankError",
    "BankNotFound",
    "BankResult",
    "BankTimeout",
    "BankUnavailable",
    "MockBank",
    "SandboxBank",
    "BANK_NAMES",
    "BANK_MODES",
    "DEMO_ACCOUNTS",
    "build_bank",
    "seed_mock_bank",
]
