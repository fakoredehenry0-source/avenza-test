"""Runtime integration of the AI/ML layer.

Safety rules enforced here:

* The backend ``IntentClassifier`` is still the deterministic first pass. LOCK,
  HELP and every confirmation word (yes / no / repeat / cancel) always come from
  the backend layer; the AI layer is never allowed to invent or override them.
* The AI classifier is consulted only when the backend is genuinely unsure, so
  the existing behaviour and state gating are preserved.
* Amounts still come from ``avenza.parser``. The optional AI cross-check can only
  make the orchestrator ask again; it can never replace a parsed amount.
* Read-backs still use the bank-verified name. The AI template is opt-in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from avenza_ai.intent import HybridIntentClassifier, KeywordIntentClassifier, LlamaIntentClassifier
from avenza_ai.parsing.amount import AmountStatus
from avenza_ai.parsing.amount import parse_amount as parse_amount_ai
from avenza_ai.parsing.timephrase import parse_time_phrase
from avenza_ai.speech.readback import ReadbackError, render_readback
from avenza_ai.types import Intent as AIIntent

from ..config import Settings
from ..intent import Intent, IntentClassifier, IntentResult
from ..parser import HistoryQuery

_AI_TO_BACKEND = {
    AIIntent.TRANSFER: Intent.TRANSFER,
    AIIntent.YES: Intent.YES,
    AIIntent.NO: Intent.NO,
    AIIntent.REPEAT: Intent.REPEAT,
    AIIntent.CANCEL: Intent.CANCEL,
    AIIntent.HISTORY: Intent.HISTORY,
    AIIntent.UNCLEAR: Intent.UNCLEAR,
}

# Backend outcomes that are authoritative and must never be replaced.
_DETERMINISTIC = {
    Intent.LOCK,
    Intent.HELP,
    Intent.YES,
    Intent.NO,
    Intent.REPEAT,
    Intent.CANCEL,
}


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class AIWorkflow:
    """A thin, safety-preserving facade over the vendored AI package."""

    def __init__(self, settings: Settings, backend: IntentClassifier):
        self.settings = settings
        self.backend = backend
        self.enabled = settings.ai_enabled
        self.intent_enabled = settings.ai_intent_enabled
        self.amount_crosscheck_enabled = settings.ai_amount_crosscheck
        self.readback_enabled = settings.ai_readback
        self.history_enabled = settings.ai_history
        self.languages = tuple(
            lang.strip() for lang in settings.ai_languages.split(",") if lang.strip()
        ) or ("en",)
        self._hybrid: HybridIntentClassifier | None = None

    # ------------------------------------------------------------------
    # Intent
    # ------------------------------------------------------------------
    @property
    def hybrid(self) -> HybridIntentClassifier:
        if self._hybrid is None:
            llm = None
            if self.settings.llm_enabled and self.settings.llm_api_key:
                llm = LlamaIntentClassifier(
                    base_url=self.settings.llm_base_url,
                    api_key=self.settings.llm_api_key,
                    model=self.settings.llm_model,
                    timeout=self.settings.llm_timeout_s,
                )
            self._hybrid = HybridIntentClassifier(
                keyword=KeywordIntentClassifier(languages=self.languages),
                llm=llm,
            )
        return self._hybrid

    async def classify(
        self, transcript: str, state: str | None = None, language: str = "en"
    ) -> IntentResult:
        base = await self.backend.classify(transcript, state=state, language=language)
        if not (self.enabled and self.intent_enabled):
            return base
        # Deterministic backend outcomes always win.
        if base.intent in _DETERMINISTIC:
            return base
        if base.intent in {Intent.TRANSFER, Intent.HISTORY} and base.confidence >= 0.9:
            return base
        try:
            ai = await self.hybrid.classify(transcript, state=state)
        except Exception:  # AI layer is advisory only
            return base
        mapped = _AI_TO_BACKEND.get(ai.intent)
        if mapped in {Intent.TRANSFER, Intent.HISTORY} and base.intent == Intent.UNCLEAR:
            return IntentResult(
                mapped,
                confidence=0.75,
                source=f"ai:{ai.source}",
                reason=ai.reason or "",
            )
        return base

    # ------------------------------------------------------------------
    # Amount cross-check (opt-in, can only ask again)
    # ------------------------------------------------------------------
    def crosscheck_amount(self, text: str, backend_kobo: int) -> str | None:
        """Return a clarification code when the AI parser disagrees, else None.

        This never supplies an amount. The orchestrator still uses the backend
        parser's value whenever the cross-check passes.
        """
        if not (self.enabled and self.amount_crosscheck_enabled):
            return None
        try:
            result = parse_amount_ai(
                text,
                languages=self.languages,
                max_kobo=self.settings.max_amount_kobo,
            )
        except Exception:
            return "ai_parser_error"
        if result.status is AmountStatus.NONE:
            return "ai_no_amount"
        if not result.ok:
            return f"ai_{result.status.value}"
        if result.kobo != backend_kobo:
            return "ai_amount_mismatch"
        return None

    # ------------------------------------------------------------------
    # Read-back
    # ------------------------------------------------------------------
    def readback(
        self,
        kobo: int,
        verified_name: str | None,
        bank_name: str | None = None,
        language: str = "en",
    ) -> str | None:
        if not (self.enabled and self.readback_enabled):
            return None
        try:
            return render_readback(
                kobo,
                verified_name,
                bank_name=bank_name,
                language=language,
            ).text
        except (ReadbackError, ValueError):
            return None

    # ------------------------------------------------------------------
    # History time phrases
    # ------------------------------------------------------------------
    def history_query(self, text: str, limit: int = 50) -> HistoryQuery | None:
        if not (self.enabled and self.history_enabled):
            return None
        try:
            result = parse_time_phrase(text)
        except Exception:
            return None
        if not result.ok or result.range is None:
            return None
        return HistoryQuery(
            _naive_utc(result.range.start),
            _naive_utc(result.range.end),
            result.range.label,
            limit,
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "intent": {
                "enabled": self.enabled and self.intent_enabled,
                "strategy": "backend_deterministic_then_ai_hybrid",
                "llm_configured": bool(self.settings.llm_enabled and self.settings.llm_api_key),
                "deterministic_confirmations": True,
            },
            "amount_crosscheck": self.enabled and self.amount_crosscheck_enabled,
            "amount_authority": "backend_deterministic_parser",
            "readback_template": self.enabled and self.readback_enabled,
            "history_phrases": self.enabled and self.history_enabled,
            "languages": list(self.languages),
            "approval": "webauthn_backend_verified_only",
        }


def build_intent(settings: Settings) -> IntentClassifier | AIWorkflow:
    """Build the runtime classifier.

    When AI is disabled this returns the plain backend classifier, so the
    application can always fall back to the pre-AI behaviour.
    """
    backend = IntentClassifier(settings)
    if not settings.ai_enabled:
        return backend
    return AIWorkflow(settings, backend)


def build_ai(settings: Settings) -> AIWorkflow | None:
    """Build the AI workflow, or None when it is disabled."""
    if not settings.ai_enabled:
        return None
    return AIWorkflow(settings, IntentClassifier(settings))
