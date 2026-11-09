"""Transfer states from section 4 of the design doc (owned by the backend).

Only used here to decide which spoken intents make sense in which state. The
classifier never moves a transfer between states: only backend code does.
"""
from __future__ import annotations

from typing import Optional

from .types import Intent

DRAFTED = "DRAFTED"
READBACK_PLAYED = "READBACK_PLAYED"
CONFIRMED_BY_VOICE = "CONFIRMED_BY_VOICE"
AWAITING_BIOMETRIC = "AWAITING_BIOMETRIC"
APPROVED = "APPROVED"
SUBMITTED = "SUBMITTED"
SETTLED = "SETTLED"
FAILED = "FAILED"
PENDING_UNKNOWN = "PENDING_UNKNOWN"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"

IDLE_STATES = frozenset({"", "IDLE", "NONE", "NO_ACTIVE_TRANSFER"})

_ALLOWED = {
    READBACK_PLAYED: frozenset({Intent.YES, Intent.NO, Intent.REPEAT}),
    AWAITING_BIOMETRIC: frozenset({Intent.CANCEL}),
}
_IDLE_ALLOWED = frozenset({Intent.TRANSFER, Intent.HISTORY})


def normalise_state(state: Optional[str]) -> str:
    return (state or "").strip().upper()


def is_idle(state: Optional[str]) -> bool:
    return normalise_state(state) in IDLE_STATES


def allowed_intents(state: Optional[str]) -> frozenset:
    """Intents that are meaningful now. UNCLEAR is always possible and is not listed."""
    s = normalise_state(state)
    if s in IDLE_STATES:
        return _IDLE_ALLOWED
    return _ALLOWED.get(s, frozenset())
