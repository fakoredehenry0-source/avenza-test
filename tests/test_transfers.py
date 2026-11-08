from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from avenza.bank import BankTimeout, MockBank
from avenza.intent import Intent
from avenza.models import BankTransfer, Transfer
from avenza.security import ApprovalError
from avenza.services import TransferLocked
from avenza.state_machine import TransferState
from avenza.timeutil import utcnow
from tests.conftest import make_customer


def ledger(services) -> list[BankTransfer]:
    with services["session_factory"]() as db:
        return list(db.scalars(select(BankTransfer)).all())


def draft(services, customer, *, amount_kobo: int = 500_000, hint: str | None = None):
    transfer = services["transfers"].create_draft(
        user_id=customer.user_id,
        session_id=customer.session_id,
        amount_kobo=amount_kobo,
        bank_code="058",
        account_number="0123456789",
        verified_name="Aisha Bello",
        hint=hint,
        language="en",
    )
    services["transfers"].mark_readback(transfer.id)
    return services["transfers"].get(transfer.id)


def to_awaiting(services, customer, **kwargs):
    transfer = draft(services, customer, **kwargs)
    services["transfers"].record_confirmation(transfer.id, Intent.YES)
    options, hold, transfer = services["transfers"].issue_approval(
        transfer.id, customer.device_id
    )
    assert hold == 0 and options is not None
    return transfer


async def settle(services, customer, **kwargs):
    transfer = to_awaiting(services, customer, **kwargs)
    return await services["transfers"].approve_and_submit(
        transfer.id, customer.device_id, None, dev_approval=True
    )


# ---------------------------------------------------------------------------
# The core rule
# ---------------------------------------------------------------------------


async def test_voice_then_fingerprint_settles(services):
    customer = make_customer(services)
    transfer = await settle(services, customer)
    assert transfer.state == TransferState.SETTLED.value
    assert transfer.bank_reference
    rows = ledger(services)
    assert len(rows) == 1
    assert rows[0].amount_kobo == 500_000
    assert rows[0].status == "SETTLED"


async def test_voice_alone_never_moves_money(services):
    customer = make_customer(services)
    transfer = to_awaiting(services, customer)
    assert transfer.state == TransferState.AWAITING_BIOMETRIC.value
    assert ledger(services) == []

    # No assertion, no dev bypass: rejected, and nothing is submitted.
    with pytest.raises(ApprovalError):
        await services["transfers"].approve_and_submit(
            transfer.id, customer.device_id, None, dev_approval=False
        )
    current = services["transfers"].get(transfer.id)
    assert current.state == TransferState.AWAITING_BIOMETRIC.value
    assert ledger(services) == []

    # Calling submit directly cannot skip approval either.
    with pytest.raises(ApprovalError):
        await services["transfers"].submit(transfer.id)
    assert ledger(services) == []


async def test_approval_with_unknown_device_is_rejected(services):
    customer = make_customer(services)
    other = make_customer(services, display_name="Other")
    transfer = to_awaiting(services, customer)
    with pytest.raises(ApprovalError):
        await services["transfers"].approve_and_submit(
            transfer.id, other.device_id, None, dev_approval=True
        )
    assert ledger(services) == []


# ---------------------------------------------------------------------------
# Exactly one payment
# ---------------------------------------------------------------------------


async def test_double_approval_and_repeated_submit_pay_once(services):
    customer = make_customer(services)
    transfer = await settle(services, customer)
    reference = transfer.bank_reference
    assert len(ledger(services)) == 1

    # A repeated approval response is rejected because the state has moved on.
    with pytest.raises(ApprovalError):
        await services["transfers"].approve_and_submit(
            transfer.id, customer.device_id, None, dev_approval=True
        )

    # A repeated submit is a no-op.
    again = await services["transfers"].submit(transfer.id)
    assert again.state == TransferState.SETTLED.value

    # The bank itself deduplicates on the idempotency key.
    duplicate = await services["bank"].create_transfer(
        idempotency_key=transfer.idempotency_key,
        customer_ref=transfer.user_id,
        amount_kobo=transfer.amount_kobo,
        bank_code=transfer.bank_code,
        account_number=transfer.account_number,
        recipient_name=transfer.verified_recipient_name,
        nonce=transfer.nonce,
    )
    assert duplicate.reference == reference
    assert len(ledger(services)) == 1


async def test_repeated_voice_request_does_not_create_a_second_draft(services):
    customer = make_customer(services)
    orchestrator = services["orchestrator"]
    request = "Send five thousand naira to GTBank account 0123456789"

    await orchestrator.process_text(customer.ctx, request)
    await orchestrator.process_text(customer.ctx, request)

    with services["session_factory"]() as db:
        rows = db.scalars(
            select(Transfer).where(Transfer.user_id == customer.user_id)
        ).all()
    assert len(rows) == 1
    assert rows[0].state == TransferState.READBACK_PLAYED.value
    assert ledger(services) == []


async def test_lost_reply_after_commit_settles_once(services):
    customer = make_customer(services)
    services["bank"].set_mode("timeout_after_commit")
    try:
        transfer = await settle(services, customer)
    finally:
        services["bank"].set_mode("normal")

    assert transfer.state == TransferState.SETTLED.value
    assert transfer.bank_reference
    rows = ledger(services)
    assert len(rows) == 1


async def test_timeout_before_commit_checks_status_then_retries_once(services):
    customer = make_customer(services)

    class LostFirstReply(MockBank):
        def __init__(self, session_factory):
            super().__init__(session_factory)
            self.calls = 0

        async def create_transfer(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise BankTimeout("lost before commit")
            return await super().create_transfer(**kwargs)

    bank = LostFirstReply(services["session_factory"])
    services["transfers"].bank = bank

    transfer = await settle(services, customer)
    assert bank.calls == 2
    assert transfer.state == TransferState.SETTLED.value
    assert len(ledger(services)) == 1


async def test_pending_then_status_check_settles(services):
    customer = make_customer(services)
    services["bank"].set_mode("pending", delay_ms=0)
    try:
        transfer = await settle(services, customer)
        assert transfer.state == TransferState.PENDING_UNKNOWN.value
        assert len(ledger(services)) == 1
        updated = await services["transfers"].check_status(transfer.id)
    finally:
        services["bank"].set_mode("normal")
    assert updated.state == TransferState.SETTLED.value
    assert len(ledger(services)) == 1


async def test_failed_payment_stays_failed_and_is_not_retried(services):
    customer = make_customer(services)
    services["bank"].set_mode("fail")
    try:
        transfer = await settle(services, customer)
        assert transfer.state == TransferState.FAILED.value
        updated = await services["transfers"].check_status(transfer.id)
    finally:
        services["bank"].set_mode("normal")
    assert updated.state == TransferState.FAILED.value
    assert len(ledger(services)) == 1


# ---------------------------------------------------------------------------
# Cancellation, expiry, duplicates, risk
# ---------------------------------------------------------------------------


async def test_cancel_before_approval_prevents_payment(services):
    customer = make_customer(services)
    transfer = draft(services, customer)
    services["transfers"].record_confirmation(transfer.id, Intent.YES)
    cancelled = services["transfers"].cancel(transfer.id, reason="customer")
    assert cancelled.state == TransferState.CANCELLED.value

    with pytest.raises(ApprovalError):
        await services["transfers"].approve_and_submit(
            transfer.id, customer.device_id, None, dev_approval=True
        )
    assert ledger(services) == []


def test_expired_draft_expires(services):
    customer = make_customer(services)
    transfer = draft(services, customer)
    with services["session_factory"]() as db:
        row = db.get(Transfer, transfer.id)
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    expired = services["transfers"].expire_due(session_id=customer.session_id)
    assert [row.id for row in expired] == [transfer.id]
    assert services["transfers"].get(transfer.id).state == TransferState.EXPIRED.value
    assert ledger(services) == []


async def test_duplicate_requires_two_voice_confirmations(services):
    customer = make_customer(services)
    await settle(services, customer)

    duplicate = draft(services, customer)
    assert "duplicate" in duplicate.risk_flags
    assert duplicate.confirmations_required == 2

    outcome = services["transfers"].record_confirmation(duplicate.id, Intent.YES)
    assert outcome.outcome == "more_confirmation"
    assert services["transfers"].get(duplicate.id).state == TransferState.READBACK_PLAYED.value

    outcome = services["transfers"].record_confirmation(duplicate.id, Intent.YES)
    assert outcome.outcome == "confirmed"
    assert services["transfers"].get(duplicate.id).state == TransferState.CONFIRMED_BY_VOICE.value


def test_name_mismatch_is_flagged(services):
    customer = make_customer(services)
    transfer = draft(services, customer, hint="Chinedu Okafor")
    assert "name_mismatch" in transfer.risk_flags


def test_lock_blocks_new_drafts_and_approval(services):
    customer = make_customer(services)
    services["transfers"].engage_lock(customer.user_id)
    with pytest.raises(TransferLocked):
        draft(services, customer)

    services["transfers"].mark_unlocked(customer.user_id)
    assert draft(services, customer).state == TransferState.READBACK_PLAYED.value


async def test_large_new_recipient_has_a_risk_hold(services):
    customer = make_customer(services)
    services["transfers"].settings = services["settings"].for_test(new_recipient_hold_s=5)
    transfer = draft(services, customer, amount_kobo=5_000_000)
    assert "new_recipient_large" in transfer.risk_flags
    services["transfers"].record_confirmation(transfer.id, Intent.YES)

    options, hold, current = services["transfers"].issue_approval(
        transfer.id, customer.device_id
    )
    assert options is None
    assert hold > 0
    assert current.state == TransferState.CONFIRMED_BY_VOICE.value
    assert ledger(services) == []


# ---------------------------------------------------------------------------
# State machine enforcement at the service boundary
# ---------------------------------------------------------------------------


def test_record_confirmation_requires_readback_state(services):
    customer = make_customer(services)
    transfer = draft(services, customer)
    services["transfers"].record_confirmation(transfer.id, Intent.YES)
    # Already confirmed: a second YES at the wrong state must not advance.
    with pytest.raises(ApprovalError):
        services["transfers"].record_confirmation(transfer.id, Intent.YES)


def test_issue_approval_requires_voice_confirmation(services):
    customer = make_customer(services)
    transfer = draft(services, customer)
    with pytest.raises(ApprovalError):
        services["transfers"].issue_approval(transfer.id, customer.device_id)
