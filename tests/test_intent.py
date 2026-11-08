from __future__ import annotations

import pytest

from avenza.config import Settings
from avenza.intent import Intent, IntentClassifier, IntentResult, keyword_intent


@pytest.mark.parametrize(
    "text,intent",
    [
        ("yes", Intent.YES),
        ("yeah", Intent.YES),
        ("correct", Intent.YES),
        ("send it", Intent.YES),
        ("no", Intent.NO),
        ("nope", Intent.NO),
        ("cancel", Intent.CANCEL),
        ("stop", Intent.CANCEL),
        ("cancel my transfer", Intent.CANCEL),
        ("repeat", Intent.REPEAT),
        ("say again", Intent.REPEAT),
        ("emergency lock", Intent.LOCK),
        ("lock transfers", Intent.LOCK),
        ("show my transactions", Intent.HISTORY),
        ("transaction history", Intent.HISTORY),
        ("help", Intent.HELP),
        ("send five thousand naira to GTBank account 0123456789", Intent.TRANSFER),
        ("transfer money to my mother", Intent.TRANSFER),
    ],
)
def test_keyword_intents(text, intent):
    assert keyword_intent(text).intent == intent


@pytest.mark.parametrize("text", ["", "   ", "blah blah blah", "what is the weather"])
def test_unclear(text):
    assert keyword_intent(text).intent == Intent.UNCLEAR


def test_confirmation_words_are_deterministic():
    # "yes" is a confirmation, never a transfer request, even though it is short.
    result = keyword_intent("yes")
    assert result.intent == Intent.YES
    assert result.confidence >= 0.9


def test_long_sentence_with_yes_is_still_confirmation():
    result = keyword_intent("yes please go ahead and send it")
    assert result.intent in {Intent.YES, Intent.TRANSFER}


def test_intent_result_confirmation_flag():
    assert IntentResult(Intent.YES, 1.0, "keyword").is_confirmation
    assert IntentResult(Intent.NO, 1.0, "keyword").is_confirmation
    assert not IntentResult(Intent.TRANSFER, 1.0, "keyword").is_confirmation


async def test_classifier_uses_keyword_when_llm_disabled():
    settings = Settings().for_test(llm_enabled=False)
    classifier = IntentClassifier(settings)
    result = await classifier.classify("send five thousand naira to GTBank account 0123456789")
    assert result.intent == Intent.TRANSFER
    assert result.source == "keyword"


async def test_classifier_never_lets_llm_decide_confirmation():
    settings = Settings().for_test(llm_enabled=True, llm_api_key="not-a-real-key")
    classifier = IntentClassifier(settings)
    result = await classifier.classify("yes")
    assert result.intent == Intent.YES
    assert result.source == "keyword"


async def test_classifier_falls_back_when_llm_unreachable():
    settings = Settings().for_test(
        llm_enabled=True,
        llm_api_key="bad",
        llm_base_url="http://127.0.0.1:1",
        llm_timeout_s=0.2,
    )
    classifier = IntentClassifier(settings)
    result = await classifier.classify("maybe do something with money")
    assert result.source in {"keyword", "keyword_fallback"}
