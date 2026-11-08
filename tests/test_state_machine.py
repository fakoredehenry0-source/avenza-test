from __future__ import annotations

import pytest

from avenza.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    TransitionError,
    TransferState,
    can_transition,
    display_state,
    ensure_transition,
    is_terminal,
)


def test_happy_path_is_linear():
    path = [
        TransferState.DRAFTED,
        TransferState.READBACK_PLAYED,
        TransferState.CONFIRMED_BY_VOICE,
        TransferState.AWAITING_BIOMETRIC,
        TransferState.APPROVED,
        TransferState.SUBMITTED,
        TransferState.SETTLED,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target)
        assert ensure_transition(current, target) == target


def test_voice_alone_cannot_reach_approved():
    assert not can_transition(TransferState.READBACK_PLAYED, TransferState.APPROVED)
    assert not can_transition(TransferState.CONFIRMED_BY_VOICE, TransferState.APPROVED)
    with pytest.raises(TransitionError):
        ensure_transition(TransferState.READBACK_PLAYED, TransferState.APPROVED)


def test_draft_cannot_jump_to_submitted():
    assert not can_transition(TransferState.DRAFTED, TransferState.SUBMITTED)
    assert not can_transition(TransferState.DRAFTED, TransferState.SETTLED)


def test_pending_unknown_can_settle_or_fail():
    assert can_transition(TransferState.PENDING_UNKNOWN, TransferState.SETTLED)
    assert can_transition(TransferState.PENDING_UNKNOWN, TransferState.FAILED)
    assert can_transition(TransferState.PENDING_UNKNOWN, TransferState.PENDING_UNKNOWN)


def test_terminal_states_have_no_exit():
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == set()
        assert is_terminal(state)


def test_cancel_and_expire_from_pre_approval_states():
    for state in (
        TransferState.DRAFTED,
        TransferState.READBACK_PLAYED,
        TransferState.CONFIRMED_BY_VOICE,
        TransferState.AWAITING_BIOMETRIC,
    ):
        assert can_transition(state, TransferState.CANCELLED)
        assert can_transition(state, TransferState.EXPIRED)


def test_submitted_cannot_be_cancelled():
    assert not can_transition(TransferState.SUBMITTED, TransferState.CANCELLED)


def test_illegal_transition_raises_with_detail():
    with pytest.raises(TransitionError) as excinfo:
        ensure_transition("SETTLED", "SUBMITTED")
    assert excinfo.value.current == TransferState.SETTLED
    assert excinfo.value.target == TransferState.SUBMITTED


def test_display_strings_exist_for_every_state():
    for state in TransferState:
        assert display_state(state)
