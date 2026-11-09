"""Hybrid classifier: keyword first, LLM only where it helps, keyword fallback always.

- Closed-set states (read-back yes/no/repeat, waiting for fingerprint): keywords only.
- Clear transfer/history keywords: keywords only (saves free-tier quota); the LLM is
  asked just to find a name when the regex found none.
- Otherwise (paraphrases): ask the LLM. Any LLM failure falls back to keywords.
- An LLM answer is accepted only if that intent makes sense in the current state.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from ..states import allowed_intents, is_idle
from ..types import Intent, IntentClassifier, IntentResult
from .keyword import KeywordIntentClassifier
from .llm import LlamaIntentClassifier


class HybridIntentClassifier(IntentClassifier):
    def __init__(self, keyword: Optional[KeywordIntentClassifier] = None,
                 llm: Optional[LlamaIntentClassifier] = None):
        self.keyword = keyword or KeywordIntentClassifier()
        self.llm = llm

    async def classify(self, transcript: str, state: Optional[str] = None) -> IntentResult:
        kw = self.keyword.classify_sync(transcript, state)
        if not (transcript or "").strip():
            return kw
        if not is_idle(state) or self.llm is None:
            return kw
        if kw.intent in (Intent.TRANSFER, Intent.HISTORY):
            if kw.intent is Intent.TRANSFER and "recipient_name" not in kw.fields:
                try:
                    res = await self.llm.classify(transcript, state)
                    if res.intent is Intent.TRANSFER and res.fields.get("recipient_name"):
                        return replace(kw, fields={**kw.fields, **res.fields})
                except Exception:  # noqa: BLE001 - keyword result stands
                    pass
            return kw
        try:
            res = await self.llm.classify(transcript, state)
        except Exception as exc:  # noqa: BLE001
            return replace(kw, source="keyword_fallback", reason=f"llm_unavailable:{type(exc).__name__}")
        if res.intent is Intent.UNCLEAR:
            return replace(kw, reason=res.reason or kw.reason)
        if res.intent not in allowed_intents(state):
            return IntentResult(Intent.UNCLEAR, source="gated", reason="intent_not_valid_in_state")
        return res
