from __future__ import annotations

from datetime import timedelta

import pytest

from avenza.intent import Intent
from avenza.models import Device, Transfer
from avenza.security import ApprovalError, WebAuthnService
from avenza.timeutil import utcnow
from tests.conftest import make_webauthn_customer


def _awaiting_transfer(services, customer, *, amount_kobo: int = 500_000) -> tuple[dict, str]:
    transfers = services["transfers"]
    transfer = transfers.create_draft(
        user_id=customer.user_id,
        session_id=customer.session_id,
        amount_kobo=amount_kobo,
        bank_code="058",
        account_number="0123456789",
        verified_name="Aisha Bello",
        hint=None,
        language="en",
    )
    transfers.mark_readback(transfer.id)
    transfers.record_confirmation(transfer.id, Intent.YES)
    options, hold, transfer = transfers.issue_approval(transfer.id, customer.device_id)
    assert hold == 0 and options is not None
    return options, transfer.id


def _verify(services, customer, transfer_id: str, credential: dict):
    with services["audit"].unit_of_work() as db:
        transfer = db.get(Transfer, transfer_id)
        device = db.get(Device, customer.device_id)
        return services["security"].verify_approval(db, transfer, device, credential)


def test_real_webauthn_registration_creates_device(services):
    customer, authenticator = make_webauthn_customer(services)
    device = services["devices"].get_device(customer.device_id)
    assert device is not None
    assert device.public_key
    assert device.credential_id
    assert device.aaguid != "dev"


def test_valid_fingerprint_approval_is_accepted(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)
    credential = authenticator.get(options)
    verified = _verify(services, customer, transfer_id, credential)
    assert verified.user_verified is True


def test_approval_without_assertion_is_rejected(services):
    customer, _ = make_webauthn_customer(services)
    _, transfer_id = _awaiting_transfer(services, customer)
    with services["audit"].unit_of_work() as db:
        transfer = db.get(Transfer, transfer_id)
        device = db.get(Device, customer.device_id)
        with pytest.raises(ApprovalError):
            services["security"].verify_approval(db, transfer, device, {})


def test_replayed_assertion_is_rejected(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)
    credential = authenticator.get(options)
    _verify(services, customer, transfer_id, credential)

    with pytest.raises(ApprovalError, match="already used"):
        _verify(services, customer, transfer_id, credential)


def test_expired_approval_is_rejected(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)
    credential = authenticator.get(options)

    with services["session_factory"]() as db:
        transfer = db.get(Transfer, transfer_id)
        transfer.approval_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

    with pytest.raises(ApprovalError, match="expired"):
        _verify(services, customer, transfer_id, credential)


def test_modified_transfer_is_rejected(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)
    credential = authenticator.get(options)

    # Change the amount after the challenge was issued. The backend rebuilds
    # the challenge from stored fields, so this must not verify.
    with services["session_factory"]() as db:
        transfer = db.get(Transfer, transfer_id)
        transfer.amount_kobo = 900_000
        db.commit()

    with pytest.raises(ApprovalError, match="no longer matches"):
        _verify(services, customer, transfer_id, credential)


def test_tampered_signature_is_rejected(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)
    credential = authenticator.get(options)

    signature = credential["response"]["signature"]
    credential["response"]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]

    with pytest.raises(ApprovalError):
        _verify(services, customer, transfer_id, credential)


def test_wrong_device_cannot_approve(services):
    customer, authenticator = make_webauthn_customer(services)
    options, transfer_id = _awaiting_transfer(services, customer)

    other_customer, other_authenticator = make_webauthn_customer(services, display_name="Other")
    # The other authenticator's key does not match the stored credential.
    credential = other_authenticator.get(options)

    with services["audit"].unit_of_work() as db:
        transfer = db.get(Transfer, transfer_id)
        device = db.get(Device, customer.device_id)
        with pytest.raises(ApprovalError):
            services["security"].verify_approval(db, transfer, device, credential)


def test_registration_challenge_cannot_be_reused(services):
    from tests.webauthn_soft import SoftAuthenticator

    settings = services["settings"]
    devices = services["devices"]
    authenticator = SoftAuthenticator(settings.webauthn_rp_id, settings.webauthn_origin)
    options = devices.begin_registration(display_name="Reuse", language="en")
    credential = authenticator.create(options["publicKey"])
    devices.verify_registration(challenge_id=options["challenge_id"], credential=credential)

    with pytest.raises(ApprovalError, match="already used"):
        devices.verify_registration(challenge_id=options["challenge_id"], credential=credential)


def test_dev_approval_blocked_in_production(settings):
    production = settings.for_test(env="production", allow_dev_approval=True, dev_tools=True)
    service = WebAuthnService(production)
    assert service.dev_approval_allowed() is False


def test_dev_enrolment_blocked_in_production(settings):
    from avenza.audit import AuditService
    from avenza.db import init_db, make_engine, make_session_factory
    from avenza.services import DeviceService

    production = settings.for_test(env="production", allow_dev_approval=True, dev_tools=True)
    engine = make_engine(production.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    audit = AuditService(factory, engine.dialect.name)
    service = DeviceService(factory, audit, WebAuthnService(production), production)
    assert service.dev_tools_allowed() is False
    with pytest.raises(ApprovalError):
        service.dev_register(display_name="Nope")
    engine.dispose()
