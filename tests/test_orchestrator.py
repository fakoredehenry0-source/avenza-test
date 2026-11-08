from __future__ import annotations

import pytest
from sqlalchemy import select

from avenza.models import Transfer
from avenza.state_machine import TransferState
from tests.conftest import make_customer

REQUEST = "Send five thousand naira to GTBank account 0123456789"


def prompt_ids(events: list[dict]) -> list[str]:
    return [event["id"] for event in events if event.get("type") == "prompt.play"]


def error_codes(events: list[dict]) -> list[str]:
    return [event["code"] for event in events if event.get("type") == "error"]


def states(events: list[dict]) -> list[str]:
    return [event["state"] for event in events if event.get("type") == "state.changed"]


async def test_full_voice_flow_settles(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]

    first = await orchestrator.process_text(customer.ctx, REQUEST)
    assert "readback" in prompt_ids(first)
    assert states(first) == [TransferState.READBACK_PLAYED.value]

    second = await orchestrator.process_text(customer.ctx, "yes")
    assert states(second) == [TransferState.AWAITING_BIOMETRIC.value]
    assert any(event["type"] == "approval.request" for event in second)

    third = await orchestrator.handle_approval(customer.ctx, None, dev=True)
    assert states(third) == [TransferState.SETTLED.value]
    assert "settled" in prompt_ids(third)


async def test_multi_turn_field_collection(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    ctx = customer.ctx

    events = await orchestrator.process_text(ctx, "send money")
    assert "ask_all" in prompt_ids(events)

    events = await orchestrator.process_text(ctx, "five thousand naira")
    assert "ask_account" in prompt_ids(events)

    events = await orchestrator.process_text(ctx, "0123456789")
    assert "ask_bank" in prompt_ids(events)

    events = await orchestrator.process_text(ctx, "GTBank")
    assert "readback" in prompt_ids(events)
    assert states(events) == [TransferState.READBACK_PLAYED.value]


async def test_repeat_replays_readback_without_advancing(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)

    events = await orchestrator.process_text(customer.ctx, "repeat")
    assert "readback" in prompt_ids(events)
    assert states(events) == [TransferState.READBACK_PLAYED.value]


async def test_two_unclear_replies_cancel_the_draft(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)

    await orchestrator.process_text(customer.ctx, "blah blah")
    events = await orchestrator.process_text(customer.ctx, "blah blah blah")
    assert "too_many_attempts" in prompt_ids(events)
    assert TransferState.CANCELLED.value in states(events)


async def test_new_transfer_request_during_readback_does_not_double_draft(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)

    events = await orchestrator.process_text(
        customer.ctx, "Send seven thousand naira to Access Bank account 1111222333"
    )
    assert "draft_active" in prompt_ids(events)
    with services["session_factory"]() as db:
        rows = db.scalars(select(Transfer).where(Transfer.user_id == customer.user_id)).all()
    assert len(rows) == 1
    assert rows[0].amount_kobo == 500_000


async def test_voice_cannot_approve_at_biometric_step(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)
    await orchestrator.process_text(customer.ctx, "yes")

    events = await orchestrator.process_text(customer.ctx, "yes")
    assert "approval_required" in error_codes(events)
    assert states(events) == []
    with services["session_factory"]() as db:
        from avenza.models import BankTransfer

        assert db.scalars(select(BankTransfer)).all() == []


async def test_no_draft_confirmation_gets_a_clear_answer(services):
    customer = make_customer(services)
    events = await services["orchestrator"].process_text(customer.ctx, "yes")
    assert "no_draft" in prompt_ids(events)


async def test_history_reads_settled_transactions(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)
    await orchestrator.process_text(customer.ctx, "yes")
    await orchestrator.handle_approval(customer.ctx, None, dev=True)

    events = await orchestrator.process_text(customer.ctx, "show my transactions today")
    history = [event for event in events if event["type"] == "history.data"]
    assert history and len(history[0]["transactions"]) == 1
    assert history[0]["transactions"][0]["state"] == TransferState.SETTLED.value
    assert "history_summary" in prompt_ids(events)


async def test_locked_customer_cannot_draft(services):
    customer = make_customer(services)
    services["transfers"].engage_lock(customer.user_id)
    events = await services["orchestrator"].process_text(customer.ctx, REQUEST)
    assert "locked" in prompt_ids(events)
    with services["session_factory"]() as db:
        assert db.scalars(select(Transfer)).all() == []


async def test_help_does_not_create_a_draft(services):
    customer = make_customer(services)
    events = await services["orchestrator"].process_text(customer.ctx, "help")
    assert "help" in prompt_ids(events)
    with services["session_factory"]() as db:
        assert db.scalars(select(Transfer)).all() == []


async def test_start_events_resumes_an_existing_readback(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    await orchestrator.process_text(customer.ctx, REQUEST)

    resumed = await orchestrator.start_events(customer.ctx)
    assert "readback" in prompt_ids(resumed)


async def test_amount_too_large_is_refused_before_any_draft(services):
    customer = make_customer(services)
    events = await services["orchestrator"].process_text(
        customer.ctx, "Send two million naira to GTBank account 0123456789"
    )
    assert "amount_too_large" in error_codes(events)
    with services["session_factory"]() as db:
        assert db.scalars(select(Transfer)).all() == []


async def test_unsupported_currency_is_refused(services):
    customer = make_customer(services)
    events = await services["orchestrator"].process_text(
        customer.ctx, "Send five thousand dollars to GTBank account 0123456789"
    )
    assert "ambiguous_amount" in error_codes(events)
    with services["session_factory"]() as db:
        assert db.scalars(select(Transfer)).all() == []
