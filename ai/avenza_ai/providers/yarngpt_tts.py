"""YarnGPT text-to-speech.

CONFIRM on Day 1 against the YarnGPT documentation: endpoint, auth header, request
fields, the 'conversation route (PCM)' format and the sample rate. This class
asks for WAV/PCM and decodes it; compressed audio is rejected with a clear error.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import httpx

from ..types import NotConfigured, ProviderError, TTSProvider, TTSResult, Timing
from .audio import decode_audio_response


class YarnGPTTTS(TTSProvider):
    name = "yarngpt"

    def __init__(self, api_key: Optional[str] = None, url: Optional[str] = None,
                 default_voice: Optional[str] = None, response_format: str = "wav",
                 default_sample_rate: int = 24000, timeout: float = 15.0,
                 client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key if api_key is not None else os.getenv("YARNGPT_API_KEY", "")
        self.url = url or os.getenv("YARNGPT_TTS_URL", "https://yarngpt.ai/api/v1/tts")  # CONFIRM
        self.default_voice = default_voice or os.getenv("YARNGPT_DEFAULT_VOICE") or None
        self.response_format, self.default_sample_rate = response_format, default_sample_rate
        self.timeout, self._client = timeout, client

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def synthesize(self, text: str, voice: Optional[str], language: str) -> TTSResult:
        if not self.is_configured():
            raise NotConfigured("YARNGPT_API_KEY is not set")
        if not text.strip():
            raise ProviderError("nothing to synthesize")
        payload = {"text": text, "response_format": self.response_format}  # CONFIRM field names
        v = voice or self.default_voice
        if v:
            payload["voice"] = v
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        t0 = time.perf_counter()
        try:
            resp = await client.post(self.url, json=payload, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise ProviderError(f"yarngpt tts: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code != 200:
            raise ProviderError(f"yarngpt tts: HTTP {resp.status_code}")
        pcm, rate = decode_audio_response(resp.content, self.default_sample_rate)
        return TTSResult(pcm, rate, Timing(time_to_first_audio_ms=(time.perf_counter() - t0) * 1000))
