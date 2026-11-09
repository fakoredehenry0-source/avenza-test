from __future__ import annotations

from datetime import datetime

from avenza.ai import AIWorkflow
from avenza.config import Settings
from avenza.intent import Intent, IntentClassifier
from avenza.orchestrator import TurnOrchestrator
from avenza.providers import MockSTT, MockTTS, build_stt, build_tts
from tests.conftest import make_customer

REQUEST = "Send five thousand naira to GTBank account 0123456789"


def ai_workflow(**changes) -> AIWorkflow:
    settings = Settings().for_test(**changes)
    return AIWorkflow(settings, IntentClassifier(settings))


async def test_ai_classifier_understands_a_paraphrase_the_keyword_layer_misses():
    backend = IntentClassifier(Settings().for_test())
    phrase = "dash five thousand naira at GTBank account 0123456789"
    assert (await backend.classify(phrase)).intent == Intent.UNCLEAR

    ai = AIWorkflow(Settings().for_test(), backend)
    result = await ai.classify(phrase)
    assert result.intent == Intent.TRANSFER
    assert result.source.startswith("ai:")


async def test_ai_never_overrides_deterministic_confirmation_or_lock():
    ai = ai_workflow()
    assert (await ai.classify("yes")).intent == Intent.YES
    assert (await ai.classify("emergency lock")).intent == Intent.LOCK
    assert (await ai.classify("cancel my transfer")).intent == Intent.CANCEL


async def test_ai_classifier_never_turns_silence_into_an_intent():
    ai = ai_workflow()
    assert (await ai.classify("   ")).intent == Intent.UNCLEAR


def test_amount_crosscheck_is_off_by_default():
    ai = ai_workflow()
    assert ai.crosscheck_amount("five thousand naira", 500_000) is None


def test_amount_crosscheck_can_only_force_a_clarification():
    ai = ai_workflow(ai_amount_crosscheck=True)
    assert ai.crosscheck_amount("five thousand naira", 500_000) is None
    assert ai.crosscheck_amount("five thousand naira", 700_000) == "ai_amount_mismatch"
    # Kobo is rejected by the AI voice parser, so the safe outcome is "ask again".
    assert ai.crosscheck_amount("five hundred kobo", 50_000) is not None


def test_ai_readback_uses_the_bank_verified_name_and_never_the_hint():
    ai = ai_workflow(ai_readback=True)
    text = ai.readback(1_500_000, "Aisha Bello", bank_name="GTBank")
    assert text is not None
    assert "Aisha Bello" in text
    assert "fifteen thousand naira" in text
    assert ai.readback(1_500_000, None) is None


def test_ai_history_phrase_adds_months_and_years():
    ai = ai_workflow()
    query = ai.history_query("show my transactions in October")
    assert query is not None
    assert query.label == "october"
    assert query.start.tzinfo is None  # backend stores naive UTC
    assert query.end > query.start


def test_ai_speech_providers_fall_back_when_credentials_are_missing():
    settings = Settings().for_test(stt_provider="ai", tts_provider="ai")
    assert build_stt(settings).name == "mock"
    assert build_tts(settings).name == "mock"


async def test_ai_wired_orchestrator_still_requires_fingerprint(services):
    customer = make_customer(services)
    settings = services["settings"].for_test(ai_enabled=True)
    ai = AIWorkflow(settings, services["intent"])
    orchestrator = TurnOrchestrator(
        settings,
        services["prompts"],
        MockSTT(),
        MockTTS(),
        ai,
        services["bank"],
        services["transfers"],
        services["sessions"],
        ai=ai,
    )

    first = await orchestrator.process_text(customer.ctx, REQUEST)
    assert any(event.get("id") == "readback" for event in first)

    second = await orchestrator.process_text(customer.ctx, "yes")
    assert any(event.get("type") == "approval.request" for event in second)

    # Voice cannot approve, even with the AI classifier wired in.
    third = await orchestrator.process_text(customer.ctx, "yes")
    assert any(event.get("code") == "approval_required" for event in third)
    from avenza.models import BankTransfer

    with services["session_factory"]() as db:
        from sqlalchemy import select

        assert db.scalars(select(BankTransfer)).all() == []


async def test_ai_paraphrase_creates_a_draft_through_the_orchestrator(services):
    customer = make_customer(services)
    settings = services["settings"].for_test(ai_enabled=True)
    ai = AIWorkflow(settings, services["intent"])
    orchestrator = TurnOrchestrator(
        settings,
        services["prompts"],
        MockSTT(),
        MockTTS(),
        ai,
        services["bank"],
        services["transfers"],
        services["sessions"],
        ai=ai,
    )

    events = await orchestrator.process_text(
        customer.ctx, "dash five thousand naira at GTBank account 0123456789"
    )
    assert any(event.get("id") == "readback" for event in events)
    assert any(event.get("state") == "READBACK_PLAYED" for event in events)


def test_ai_status_endpoint_is_honest_about_authority(client):
    data = client.get("/ai/status").json()
    assert data["amount_authority"] == "backend_deterministic_parser"
    assert data["approval"] == "webauthn_backend_verified_only"
    assert data["ai"]["intent"]["deterministic_confirmations"] is True
