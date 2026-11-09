import asyncio

import httpx
import pytest

from avenza_ai.draft import NEEDS_CLARIFICATION, NOT_A_TRANSFER, READY, build_draft
from avenza_ai.intent import (HybridIntentClassifier, KeywordIntentClassifier, LlamaIntentClassifier,
                              extract_bank_hint, extract_recipient_hint, grounded_in)
from avenza_ai.states import (AWAITING_BIOMETRIC, DRAFTED, READBACK_PLAYED, SUBMITTED, allowed_intents)
from avenza_ai.types import Intent

run = asyncio.run
KW = KeywordIntentClassifier()


def kw(text, state=None, c=KW):
    return run(c.classify(text, state)).intent


# ---- silence and noise never advance anything --------------------------------------
@pytest.mark.parametrize("state", [None, "READBACK_PLAYED", "AWAITING_BIOMETRIC", "SUBMITTED", "DRAFTED"])
@pytest.mark.parametrize("text", ["", "   ", None, "...", "hmm"])
def test_empty_or_noise_is_unclear(text, state):
    assert kw(text, state) is Intent.UNCLEAR


# ---- read-back confirmation --------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("yes", Intent.YES), ("Yes please", Intent.YES), ("yeah that is correct", Intent.YES),
    ("go ahead", Intent.YES), ("e correct", Intent.YES),
    ("no", Intent.NO), ("nope", Intent.NO), ("cancel", Intent.NO), ("stop", Intent.NO), ("wrong", Intent.NO),
    ("repeat", Intent.REPEAT), ("say that again", Intent.REPEAT), ("pardon", Intent.REPEAT),
])
def test_readback_words(text, expected):
    assert kw(text, READBACK_PLAYED) is expected


@pytest.mark.parametrize("text", [
    "yes no", "yes repeat", "not correct", "i am not sure", "no yes", "yes cancel",   # conflicting
    "mmm", "send five thousand naira",                                              # no confirmation word
    "yes i think so but my cousin was saying something about the other account yesterday",  # background talk
])
def test_readback_conflicts_and_long_speech_are_unclear(text):
    assert kw(text, READBACK_PLAYED) is Intent.UNCLEAR


def test_yes_is_ignored_when_no_confirmation_is_pending():
    for st in (None, DRAFTED, SUBMITTED, "SETTLED", "APPROVED"):
        assert kw("yes", st) is Intent.UNCLEAR


def test_only_cancel_works_by_voice_while_waiting_for_fingerprint():
    assert kw("cancel", AWAITING_BIOMETRIC) is Intent.CANCEL
    assert kw("yes", AWAITING_BIOMETRIC) is Intent.UNCLEAR
    assert kw("yes cancel", AWAITING_BIOMETRIC) is Intent.UNCLEAR
    assert kw("send it", AWAITING_BIOMETRIC) is Intent.UNCLEAR


# ---- idle requests --------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("send five thousand naira to Aisha Bello", Intent.TRANSFER),
    ("abeg transfer 5k give Musa", Intent.TRANSFER),
    ("pay Chinedu fifteen thousand", Intent.TRANSFER),
    ("show my transactions yesterday", Intent.HISTORY),
    ("what did I send yesterday", Intent.HISTORY),
    ("get my statement for last week", Intent.HISTORY),
    ("how do I send money", Intent.UNCLEAR),
    ("don't send 5000 to Aisha", Intent.UNCLEAR),
    ("cancel send 5000", Intent.UNCLEAR),
    ("hello", Intent.UNCLEAR),
])
def test_idle_requests(text, expected):
    assert kw(text) is expected


def test_idle_transfer_carries_text_hints_only():
    r = run(KW.classify("send 5000 naira to Aisha Bello at GTBank"))
    assert r.fields == {"recipient_name": "Aisha Bello", "bank": "GTBank"}
    assert "amount" not in r.fields


def test_local_words_only_when_language_enabled():
    assert kw("aika naira dubu biyar zuwa Aisha") is Intent.UNCLEAR
    ha = KeywordIntentClassifier(languages=("en", "ha"))
    assert kw("aika naira dubu biyar zuwa Aisha", c=ha) is Intent.TRANSFER


# ---- entities ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,name", [
    ("send 5000 naira to Aisha Bello", "Aisha Bello"),
    ("transfer fifteen thousand to Chinedu Okafor at GTBank", "Chinedu Okafor"),
    ("send 5000 to Aisha Bello-Okafor please", "Aisha Bello-Okafor"),
    ("send five thousand naira to account 0123456789", None),
    ("send five thousand naira to me", None),
    ("send five thousand naira to the one at gtbank", None),
    ("send money", None),
])
def test_recipient_hint(text, name):
    assert extract_recipient_hint(text) == name


def test_bank_hint():
    assert extract_bank_hint("send to Musa at Zenith") == "Zenith Bank"
    assert extract_bank_hint("from gtb to opay") is None       # two banks: do not guess
    assert extract_bank_hint("I need access to my money") is None


def test_grounding():
    assert grounded_in("send 5000 to Aisha Bello", "aisha bello")
    assert not grounded_in("send 5000 to Aisha Bello", "Aisha Bello Okafor")
    assert not grounded_in("send 5000", "")


# ---- LLM + fallback --------------------------------------------------------------------
def llm_with(handler, **kw_):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LlamaIntentClassifier(base_url="http://llm.test/v1", api_key="k", model="llama-3", client=client, **kw_)


def chat(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_llm_paraphrase_becomes_transfer_with_grounded_name():
    seen = {}

    def handler(req):
        seen["body"] = req.read().decode()
        return chat('```json\n{"intent":"transfer","recipient_name":"my guy Chinedu","bank":null,"amount":999999}\n```')

    h = HybridIntentClassifier(llm=llm_with(handler))
    r = run(h.classify("abeg help me move small money give my guy Chinedu", None))
    assert r.intent is Intent.TRANSFER and r.source == "llm"
    assert r.fields == {"recipient_name": "my guy Chinedu"} and "amount" not in r.fields
    assert "never output" in seen["body"].lower()


def test_llm_invented_name_is_dropped():
    h = HybridIntentClassifier(llm=llm_with(lambda r: chat('{"intent":"transfer","recipient_name":"Bola Tinubu"}')))
    r = run(h.classify("abeg help me move small money give my guy", None))
    assert r.intent is Intent.TRANSFER and r.fields == {}


def test_llm_cannot_say_yes_outside_readback():
    h = HybridIntentClassifier(llm=llm_with(lambda r: chat('{"intent":"yes"}')))
    r = run(h.classify("hmm go on na", None))
    assert r.intent is Intent.UNCLEAR


def test_llm_is_not_called_for_closed_set_states():
    calls = []
    h = HybridIntentClassifier(llm=llm_with(lambda r: calls.append(1) or chat('{"intent":"yes"}')))
    assert run(h.classify("yes", READBACK_PLAYED)).intent is Intent.YES
    assert run(h.classify("hmm", READBACK_PLAYED)).intent is Intent.UNCLEAR
    assert calls == []


def test_llm_not_called_for_clear_transfer_with_name():
    calls = []
    h = HybridIntentClassifier(llm=llm_with(lambda r: calls.append(1) or chat("{}")))
    assert run(h.classify("send 5000 to Aisha Bello", None)).intent is Intent.TRANSFER
    assert calls == []


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500),
    lambda r: chat("not json at all"),
    lambda r: httpx.Response(200, json={"oops": 1}),
])
def test_llm_failures_fall_back_to_keywords(handler):
    h = HybridIntentClassifier(llm=llm_with(handler))
    r = run(h.classify("send 5000 to Aisha Bello", None))
    assert r.intent is Intent.TRANSFER
    r2 = run(h.classify("what a day", None))
    assert r2.intent is Intent.UNCLEAR


def test_rate_limit_sets_cooldown_and_skips_calls():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})

    llm = llm_with(handler)
    h = HybridIntentClassifier(llm=llm)
    r = run(h.classify("abeg help me move small money give my guy", None))
    assert r.source == "keyword_fallback" and "llm_unavailable" in r.reason and llm.cooling_down
    run(h.classify("abeg help me again", None))
    assert len(calls) == 1          # second request never left the machine


def test_llm_timeout_falls_back():
    def handler(req):
        raise httpx.ConnectTimeout("slow")

    h = HybridIntentClassifier(llm=llm_with(handler))
    assert run(h.classify("send 5000 to Aisha", None)).intent is Intent.TRANSFER


def test_gated_state_allowed_intents():
    assert allowed_intents(READBACK_PLAYED) == {Intent.YES, Intent.NO, Intent.REPEAT}
    assert allowed_intents("settled") == frozenset()


# ---- draft builder -----------------------------------------------------------------------
def draft(text, **kw_):
    return run(build_draft(text, HybridIntentClassifier(), **kw_))


def test_draft_ready():
    d = draft("Send fifteen thousand naira to Aisha Bello at GTBank")
    assert d.status == READY and d.amount.kobo == 1500000 and d.recipient_hint == "Aisha Bello" and d.bank_hint == "GTBank"


def test_draft_with_account_number_needs_no_name():
    d = draft("send 5000 naira to 0123456789")
    assert d.status == READY and d.account_number == "0123456789" and d.amount.kobo == 500000


@pytest.mark.parametrize("text,prompt", [
    ("send to Aisha Bello", "ask_amount"),
    ("send fifteen fifty naira to Aisha Bello", "amount_unclear"),
    ("send 5000 or 7000 naira to Aisha", "amount_unclear"),
    ("send five thousand naira", "ask_recipient"),
])
def test_draft_asks_instead_of_guessing(text, prompt):
    d = draft(text)
    assert d.status == NEEDS_CLARIFICATION and d.clarify_prompt_id == prompt


def test_draft_low_confidence_asks_again():
    d = draft("send 5000 to Aisha Bello", stt_confidence=0.3)
    assert d.status == NEEDS_CLARIFICATION and d.clarify_prompt_id == "didnt_catch"


def test_draft_not_a_transfer():
    assert draft("show my transactions yesterday").status == NOT_A_TRANSFER


def test_llm_never_sets_the_amount_in_a_draft():
    llm = llm_with(lambda r: chat('{"intent":"transfer","recipient_name":"Aisha","amount":1000000}'))
    d = run(build_draft("abeg help me move money give Aisha", HybridIntentClassifier(llm=llm)))
    assert d.status == NEEDS_CLARIFICATION and d.clarify_prompt_id == "ask_amount" and d.amount.kobo is None
