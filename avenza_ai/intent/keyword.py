"""Keyword intent classifier: fast, free, works with no network or LLM quota.

Used for yes / no / repeat (closed set), as the fallback when the LLM is rate
limited or down, and as the first pass for transfer and history requests.

Safety rules:
- Silence or an empty transcript is never an intent.
- Conflicting words ("yes ... no", "not correct") are unclear, never a guess.
- In the read-back state, a long utterance is unclear (background talk, TV).
- Yoruba/Hausa/Igbo words are UNVERIFIED and only used for enabled languages.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Set

from ..parsing.lexicon import words as _words
from ..parsing.timephrase import parse_time_phrase
from ..states import AWAITING_BIOMETRIC, READBACK_PLAYED, is_idle, normalise_state
from ..types import Intent, IntentClassifier, IntentResult
from .entities import extract_bank_hint, extract_recipient_hint

_BASE = {
    "yes": {"words": {"yes", "yeah", "yep", "yup", "yea", "correct", "sure", "okay", "ok", "confirm", "confirmed", "proceed"},
            "phrases": {"go ahead", "that is right", "that is correct", "thats right", "thats correct",
                        "send it", "send am", "do am", "e correct", "na so", "na im"}},
    "no": {"words": {"no", "nope", "nah", "wrong", "incorrect", "negative", "not", "dont", "never", "nay"},
           "phrases": {"no be am", "no be so"}},
    "cancel": {"words": {"cancel", "stop", "abort", "nevermind"},
               "phrases": {"never mind", "forget it", "leave am"}},
    "repeat": {"words": {"repeat", "again", "pardon"},
               "phrases": {"say that again", "come again", "say am again"}},
    "transfer": {"words": {"send", "transfer", "pay", "remit", "dash"}, "phrases": set()},
    "history": {"words": {"history", "transactions", "transaction", "statement", "statements",
                          "receipt", "receipts", "sent", "paid", "spent"}, "phrases": set()},
}
# UNVERIFIED: needs native-speaker review before a language is enabled.
_LOCAL = {
    "ha": {"yes": {"eh", "ee", "to"}, "no": {"aa", "babu"}, "cancel": {"soke", "dakata"},
           "repeat": {"sake"}, "transfer": {"aika", "tura", "biya"}},
    "yo": {"yes": {"beeni", "beni"}, "no": {"rara", "beeko"}, "cancel": {"fagile", "duro"},
           "repeat": {"tun"}, "transfer": {"ranse", "san"}},
    "ig": {"yes": {"ee", "eeh"}, "no": {"mba"}, "cancel": {"kwusi"},
           "repeat": {"ozo"}, "transfer": {"zipu", "kwuo", "nyefe"}},
}
_QUESTION_CUES = {"did", "what", "when", "which", "how", "have", "show", "list", "check"}
_CONFIRM_CATS = ("yes", "no", "cancel", "repeat")


class KeywordIntentClassifier(IntentClassifier):
    def __init__(self, languages: Iterable[str] = ("en", "pcm"), max_confirm_tokens: int = 8):
        self.max_confirm_tokens = max_confirm_tokens
        self._words: Dict[str, Set[str]] = {k: set(v["words"]) for k, v in _BASE.items()}
        self._phrases: Dict[str, Set[str]] = {k: set(v["phrases"]) for k, v in _BASE.items()}
        for lang in languages:
            for cat, ws in _LOCAL.get(lang, {}).items():
                self._words[cat] |= ws

    def _hits(self, tokens: list) -> Set[str]:
        joined = " " + " ".join(tokens) + " "
        hits = set()
        for cat in self._words:
            if self._words[cat].intersection(tokens) or any(f" {p} " in joined for p in self._phrases[cat]):
                hits.add(cat)
        return hits

    async def classify(self, transcript: str, state: Optional[str] = None) -> IntentResult:
        return self.classify_sync(transcript, state)

    def classify_sync(self, transcript: str, state: Optional[str] = None) -> IntentResult:
        tokens = _words(transcript or "")
        if not tokens:
            return IntentResult(Intent.UNCLEAR, source="empty", reason="empty_transcript")
        hits = self._hits(tokens)
        s = normalise_state(state)

        if s == READBACK_PLAYED:
            if len(tokens) > self.max_confirm_tokens:
                return IntentResult(Intent.UNCLEAR, reason="too_long_for_confirmation")
            cats = {"no" if c == "cancel" else c for c in hits if c in _CONFIRM_CATS}
            if len(cats) == 1:
                return IntentResult(Intent(cats.pop()))
            return IntentResult(Intent.UNCLEAR, reason="conflicting_words" if cats else "no_confirmation_word")

        if s == AWAITING_BIOMETRIC:
            if "cancel" in hits and not (hits & {"yes", "repeat", "transfer"}):
                return IntentResult(Intent.CANCEL)
            return IntentResult(Intent.UNCLEAR, reason="only_cancel_by_voice_while_waiting_for_fingerprint")

        if is_idle(state):
            negated = bool(hits & {"no", "cancel"})
            if "transfer" in hits and negated:
                return IntentResult(Intent.UNCLEAR, reason="transfer_word_with_negation")
            if "history" in hits and "transfer" in hits - {"history"} and not (set(tokens) & _QUESTION_CUES):
                pass
            if "transfer" in hits:
                if set(tokens) & _QUESTION_CUES:
                    if parse_time_phrase(transcript).ok:
                        return IntentResult(Intent.HISTORY)
                    return IntentResult(Intent.UNCLEAR, reason="transfer_word_in_question")
                fields = {}
                name, bank = extract_recipient_hint(transcript), extract_bank_hint(transcript)
                if name:
                    fields["recipient_name"] = name
                if bank:
                    fields["bank"] = bank
                return IntentResult(Intent.TRANSFER, fields=fields)
            if "history" in hits:
                return IntentResult(Intent.HISTORY)
            return IntentResult(Intent.UNCLEAR, reason="no_keyword")

        return IntentResult(Intent.UNCLEAR, source="gated", reason="no_voice_input_expected_in_this_state")
