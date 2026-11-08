from __future__ import annotations

from enum import Enum


class TransferState(str, Enum):
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


TERMINAL_STATES = {
    TransferState.SETTLED,
    TransferState.FAILED,
    TransferState.CANCELLED,
    TransferState.EXPIRED,
}

# Only explicit code paths may use these. Silence never advances a state.
ALLOWED_TRANSITIONS: dict[TransferState, set[TransferState]] = {
    TransferState.DRAFTED: {
        TransferState.READBACK_PLAYED,
        TransferState.CANCELLED,
        TransferState.EXPIRED,
    },
    TransferState.READBACK_PLAYED: {
        TransferState.READBACK_PLAYED,  # "Repeat"
        TransferState.CONFIRMED_BY_VOICE,
        TransferState.CANCELLED,
        TransferState.EXPIRED,
    },
    TransferState.CONFIRMED_BY_VOICE: {
        TransferState.AWAITING_BIOMETRIC,
        TransferState.CANCELLED,
        TransferState.EXPIRED,
    },
    TransferState.AWAITING_BIOMETRIC: {
        TransferState.APPROVED,
        TransferState.CANCELLED,
        TransferState.EXPIRED,
    },
    TransferState.APPROVED: {TransferState.SUBMITTED},
    TransferState.SUBMITTED: {
        TransferState.SETTLED,
        TransferState.FAILED,
        TransferState.PENDING_UNKNOWN,
    },
    TransferState.PENDING_UNKNOWN: {
        TransferState.SETTLED,
        TransferState.FAILED,
        TransferState.PENDING_UNKNOWN,
    },
    TransferState.SETTLED: set(),
    TransferState.FAILED: set(),
    TransferState.CANCELLED: set(),
    TransferState.EXPIRED: set(),
}

DISPLAY = {
    TransferState.DRAFTED: "Draft",
    TransferState.READBACK_PLAYED: "Waiting for your yes or no",
    TransferState.CONFIRMED_BY_VOICE: "Confirmed by voice",
    TransferState.AWAITING_BIOMETRIC: "Waiting for your fingerprint",
    TransferState.APPROVED: "Approved by fingerprint",
    TransferState.SUBMITTED: "Sending to the bank",
    TransferState.SETTLED: "Payment settled",
    TransferState.FAILED: "Payment failed",
    TransferState.PENDING_UNKNOWN: "Checking with the bank",
    TransferState.CANCELLED: "Cancelled — no money moved",
    TransferState.EXPIRED: "Expired — no money moved",
}


class TransitionError(ValueError):
    def __init__(self, current: TransferState, target: TransferState):
        super().__init__(f"illegal transfer transition {current.value} -> {target.value}")
        self.current = current
        self.target = target


def as_state(value: str | TransferState) -> TransferState:
    return value if isinstance(value, TransferState) else TransferState(value)


def can_transition(current: str | TransferState, target: str | TransferState) -> bool:
    cur, tgt = as_state(current), as_state(target)
    return tgt in ALLOWED_TRANSITIONS[cur]


def ensure_transition(current: str | TransferState, target: str | TransferState) -> TransferState:
    cur, tgt = as_state(current), as_state(target)
    if tgt not in ALLOWED_TRANSITIONS[cur]:
        raise TransitionError(cur, tgt)
    return tgt


def is_terminal(state: str | TransferState) -> bool:
    return as_state(state) in TERMINAL_STATES


def display_state(state: str | TransferState) -> str:
    return DISPLAY[as_state(state)]
