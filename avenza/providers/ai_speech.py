"""AI speech providers adapted to the backend ``STTProvider`` / ``TTSProvider`` contracts.

The AI package speaks a streaming protocol (``send_audio`` / ``finish`` /
``events``). The backend orchestrator consumes a single ``transcribe`` result, so
these adapters use the AI package's own ``transcribe`` convenience wrapper. No
behaviour in the approval path changes: speech only produces text.
"""

from __future__ import annotations

import time

from avenza_ai.providers import (
    DeepgramSTTProvider,
    ProviderChoice,
    SaharaSTTProvider,
    YarnGPTSTTProvider,
    YarnGPTTTS,
)
from avenza_ai.types import NotConfigured, ProviderError

from ..config import Settings
from .base import AudioResult, ProviderNotConfigured, STTProvider, TranscriptResult, TTSProvider


class AIWebSocketSTT(STTProvider):
    """Language-aware streaming STT from the AI provider registry."""

    name = "ai"
    simulated = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.choice = ProviderChoice.load(settings.ai_provider_choice)
        self.default_language = settings.ai_default_language
        self.factories = {
            "deepgram": lambda: DeepgramSTTProvider(
                api_key=settings.deepgram_api_key,
                model=settings.deepgram_model,
            ),
            "yarngpt": lambda: YarnGPTSTTProvider(
                url=settings.yarngpt_asr_url or settings.yarn_asr_url or None,
                api_key=settings.yarngpt_api_key or settings.yarn_api_key or None,
            ),
            "sahara": lambda: SaharaSTTProvider(
                url=settings.sahara_asr_url or None,
                api_key=settings.sahara_api_key or None,
            ),
        }
        try:
            default = self.choice.stt_for(self.default_language, self.factories)
        except (KeyError, LookupError) as exc:
            raise ProviderNotConfigured(f"AI STT default language unavailable: {exc}") from exc
        if not default.is_configured():
            raise ProviderNotConfigured(
                f"AI STT provider {default.name!r} has no credentials or URL"
            )
        self.name = f"ai:{default.name}"

    def _provider_for(self, language: str):
        language = language if self.choice.is_enabled(language) else self.default_language
        return language, self.choice.stt_for(language, self.factories)

    async def transcribe(
        self,
        pcm: bytes,
        sample_rate: int,
        language_hint: str = "en",
        provided_text: str | None = None,
    ) -> TranscriptResult:
        if provided_text:
            return TranscriptResult(provided_text.strip(), 0.99, self.name, language=language_hint)
        if not pcm:
            return TranscriptResult("", 0.0, self.name, language=language_hint, error="no_speech")

        try:
            language, provider = self._provider_for(language_hint)
            result = await provider.transcribe(
                pcm,
                language,
                sample_rate,
                timeout=self.settings.ai_stt_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced honestly to the client
            return TranscriptResult(
                "",
                0.0,
                self.name,
                language=language_hint,
                error=f"ai_stt_error:{type(exc).__name__}",
            )

        text = (result.text or "").strip()
        timing = result.timing
        return TranscriptResult(
            text=text,
            confidence=float(result.confidence or 0.0),
            provider=f"ai:{provider.name}",
            ttfp_ms=int(timing.time_to_first_partial_ms or 0),
            final_ms=int(timing.time_to_final_ms or 0),
            language=language,
            simulated=False,
            error=None if text else "no_speech",
        )


class AIYarnTTS(TTSProvider):
    """YarnGPT TTS from the AI package, mapped to ``AudioResult``."""

    name = "ai:yarngpt"
    simulated = False

    def __init__(self, settings: Settings):
        self.settings = settings
        self.tts = YarnGPTTTS(
            api_key=settings.yarngpt_api_key or settings.yarn_api_key or None,
            url=settings.yarngpt_tts_url or settings.yarn_tts_url or None,
            default_voice=settings.yarngpt_default_voice or settings.yarn_tts_voice or None,
        )
        if not self.tts.is_configured():
            raise ProviderNotConfigured("YARNGPT_API_KEY / YARN_API_KEY is not set")

    async def synthesize(self, text: str, voice: str = "", language: str = "en") -> AudioResult:
        start = time.perf_counter()
        try:
            result = await self.tts.synthesize(text, voice or None, language)
        except (NotConfigured, ProviderError):
            return AudioResult(b"", 24000, self.name, text=text, simulated=False)
        elapsed = int((time.perf_counter() - start) * 1000)
        return AudioResult(
            result.pcm,
            result.sample_rate,
            self.name,
            ttfa_ms=int(result.timing.time_to_first_audio_ms or elapsed),
            total_ms=elapsed,
            text=text,
        )
