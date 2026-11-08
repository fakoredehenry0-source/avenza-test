from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from avenza.audit import AuditService
from avenza.bank import MockBank, build_bank, seed_mock_bank
from avenza.config import Settings
from avenza.db import init_db, make_engine, make_session_factory
from avenza.intent import IntentClassifier
from avenza.main import create_app
from avenza.orchestrator import SessionContext, TurnOrchestrator
from avenza.prompts import PromptLibrary
from avenza.providers import MockSTT, MockTTS
from avenza.security import WebAuthnService
from avenza.services import DeviceService, SessionService, TransferService
from avenza.timeutil import utcnow


@pytest.fixture()
def settings() -> Settings:
    return Settings().for_test()


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def services(settings):
    """A fully wired service graph on an in-memory database."""
    engine = make_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    seed_mock_bank(session_factory)
    audit = AuditService(session_factory, engine.dialect.name)
    security = WebAuthnService(settings)
    prompts = PromptLibrary(settings.prompt_dir)
    bank = build_bank(settings, session_factory)
    devices = DeviceService(session_factory, audit, security, settings)
    sessions = SessionService(session_factory, audit, settings)
    transfers = TransferService(session_factory, audit, bank, security, prompts, settings)
    intent = IntentClassifier(settings)
    orchestrator = TurnOrchestrator(
        settings, prompts, MockSTT(), MockTTS(), intent, bank, transfers, sessions
    )
    yield {
        "settings": settings,
        "engine": engine,
        "session_factory": session_factory,
        "audit": audit,
        "security": security,
        "prompts": prompts,
        "bank": bank,
        "devices": devices,
        "sessions": sessions,
        "transfers": transfers,
        "intent": intent,
        "orchestrator": orchestrator,
    }
    engine.dispose()


@dataclass
class DemoCustomer:
    user_id: str
    device_id: str
    device_token: str
    consent_id: str
    session_id: str
    ctx: SessionContext


def make_customer(services, *, display_name: str = "Test Customer", language: str = "en") -> DemoCustomer:
    devices = services["devices"]
    sessions = services["sessions"]
    orchestrator = services["orchestrator"]

    registered = devices.dev_register(display_name=display_name, language=language)
    consent = devices.record_consent(
        user_id=registered["user_id"],
        wording_version="2026-11-08-v1",
        language=language,
    )
    session = sessions.start(
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        consent_id=consent.id,
        language=language,
    )
    ctx = SessionContext(
        session_id=session.id,
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        language=language,
        display_name=display_name,
    )
    return DemoCustomer(
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        device_token=registered["device_token"],
        consent_id=consent.id,
        session_id=session.id,
        ctx=ctx,
    )


async def run_turn(orchestrator, ctx, text: str) -> list[dict]:
    return await orchestrator.process_text(ctx, text)


def make_webauthn_customer(services, *, display_name: str = "Passkey Customer"):
    """Enrol a customer with a real software authenticator.

    Returns the DemoCustomer plus the authenticator, so tests can produce real
    WebAuthn assertions for the approval verifier.
    """
    from tests.webauthn_soft import SoftAuthenticator

    settings = services["settings"]
    devices = services["devices"]
    sessions = services["sessions"]
    authenticator = SoftAuthenticator(settings.webauthn_rp_id, settings.webauthn_origin)

    options = devices.begin_registration(display_name=display_name, language="en")
    credential = authenticator.create(options["publicKey"])
    registered = devices.verify_registration(
        challenge_id=options["challenge_id"],
        credential=credential,
    )
    consent = devices.record_consent(
        user_id=registered["user_id"],
        wording_version="2026-11-08-v1",
        language="en",
    )
    session = sessions.start(
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        consent_id=consent.id,
        language="en",
    )
    ctx = SessionContext(
        session_id=session.id,
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        language="en",
        display_name=display_name,
    )
    customer = DemoCustomer(
        user_id=registered["user_id"],
        device_id=registered["device_id"],
        device_token=registered["device_token"],
        consent_id=consent.id,
        session_id=session.id,
        ctx=ctx,
    )
    return customer, authenticator


def draft_to_awaiting(orchestrator, ctx, request: str, confirm: str = "yes") -> list[dict]:
    """Synchronous convenience used by tests that only need the events."""
    import asyncio

    async def _run():
        first = await orchestrator.process_text(ctx, request)
        second = await orchestrator.process_text(ctx, confirm)
        return first + second

    return asyncio.run(_run())


def expire_transfer(services, transfer_id: str, *, seconds_ago: int = 5) -> None:
    with services["session_factory"]() as db:
        from avenza.models import Transfer

        transfer = db.get(Transfer, transfer_id)
        assert transfer is not None
        transfer.approval_expires_at = utcnow() - timedelta(seconds=seconds_ago)
        transfer.expires_at = utcnow() - timedelta(seconds=seconds_ago)
        db.commit()
