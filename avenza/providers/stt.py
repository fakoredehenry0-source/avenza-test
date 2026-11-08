from __future__ import annotations

import base64
import io
import json
import time
import wave

import httpx

from ..config import Settings
from .base import ProviderNotConfigured, STTProvider, TranscriptResult


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class MockSTT(STTProvider):
    """Demo/test provider.

    With a browser speech-recognition transcript or typed text it returns that
    text. With raw audio and no text it honestly reports that it heard nothing;
    it never invents a transcript.
    """

    name = "mock"
    simulated = True

    async def transcribe(
        self,
        pcm: bytes,
        sample_rate: int,
        language_hint: str = "en",
        provided_text: str | None = None,
    ) -> TranscriptResult:
        start = time.perf_counter()
        text = (provided_text or "").strip()
        elapsed = int((time.perf_counter() - start) * 1000)
        if not text:
            return TranscriptResult(
                text="",
                confidence=0.0,
                provider=self.name,
                final_ms=elapsed,
                language=language_hint,
                simulated=True,
                error="no_speech",
            )
        return TranscriptResult(
            text=text,
            confidence=0.99,
            provider=self.name,
            ttfp_ms=0,
            final_ms=elapsed,
            language=language_hint,
            simulated=True,
        )


class DeepgramSTT(STTProvider):
    """Deepgram streaming STT (second provider, used when a key is present)."""

    name = "deepgram"
    simulated = False

    def __init__(self, settings: Settings):
        if not settings.deepgram_api_key:
            raise ProviderNotConfigured("DEEPGRAM_API_KEY is not set")
        self.settings = settings

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

        import websockets

        start = time.perf_counter()
        first_partial_ms = 0
        final_text = ""
        confidence = 0.0
        lang = {"en": "en-NG", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "en-NG"}.get(language_hint, "en-NG")
        url = (
            "wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16&sample_rate={sample_rate}&channels=1"
            f"&model={self.settings.deepgram_model}&language={lang}"
            "&smart_format=true&interim_results=true&punctuate=true"
        )
        headers = {"Authorization": f"Token {self.settings.deepgram_api_key}"}
        try:
            async with websockets.connect(url, additional_headers=headers, open_timeout=8) as ws:
                # Send in ~100 ms chunks to behave like a stream.
                chunk = max(2, sample_rate // 10 * 2)
                for i in range(0, len(pcm), chunk):
                    await ws.send(pcm[i : i + chunk])
                await ws.send(json.dumps({"type": "CloseStream"}))
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("type") != "Results":
                        continue
                    alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                    text = alt.get("transcript", "")
                    if text and not first_partial_ms:
                        first_partial_ms = int((time.perf_counter() - start) * 1000)
                    if msg.get("is_final") and text:
                        final_text = (final_text + " " + text).strip()
                        confidence = max(confidence, float(alt.get("confidence", 0.0)))
                    if msg.get("speech_final"):
                        break
        except Exception as exc:
            return TranscriptResult(
                "",
                0.0,
                self.name,
                language=language_hint,
                error=f"deepgram_error:{type(exc).__name__}",
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        return TranscriptResult(
            final_text,
            confidence or 0.0,
            self.name,
            ttfp_ms=first_partial_ms,
            final_ms=elapsed,
            language=language_hint,
            error=None if final_text else "no_speech",
        )


class HttpSTT(STTProvider):
    """Generic HTTP adapter for YarnGPT / Sahara.

    The vendors' live-stream wire formats are not public in the documentation
    we have, so this adapter is deliberately marked CONFIRM. It accepts either
    raw audio bytes or a JSON envelope and looks for common transcript fields.
    """

    def __init__(self, name: str, url: str, api_key: str, simulated: bool = False):
        if not url:
            raise ProviderNotConfigured(f"{name} URL is not set")
        self.name = name
        self.url = url
        self.api_key = api_key
        self.simulated = simulated

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
        start = time.perf_counter()
        headers = {"Content-Type": "audio/wav"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    self.url,
                    content=pcm_to_wav(pcm, sample_rate),
                    headers=headers,
                    params={"language": language_hint, "sample_rate": sample_rate},
                )
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "application/json" in ctype:
                    data = resp.json()
                    text = (
                        data.get("text")
                        or data.get("transcript")
                        or data.get("data", {}).get("text", "")
                        or data.get("result", {}).get("text", "")
                    )
                    conf = float(data.get("confidence", 0.0) or 0.0)
                else:
                    text = resp.text.strip()
                    conf = 0.0
        except Exception as exc:
            return TranscriptResult(
                "", 0.0, self.name, language=language_hint, error=f"{self.name}_error:{type(exc).__name__}"
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        return TranscriptResult(
            text,
            conf,
            self.name,
            final_ms=elapsed,
            language=language_hint,
            error=None if text else "no_speech",
        )


def build_stt(settings: Settings) -> STTProvider:
    choice = settings.stt_provider
    try:
        if choice == "deepgram":
            return DeepgramSTT(settings)
        if choice == "yarn":
            return HttpSTT("yarn", settings.yarn_asr_url, settings.yarn_api_key)
        if choice == "sahara":
            return HttpSTT("sahara", settings.sahara_asr_url, settings.sahara_api_key)
    except ProviderNotConfigured:
        pass
    return MockSTT()


class YarnTTS:
    """YarnGPT conversation TTS adapter.

    CONFIRM: the exact request/response shape must be checked against the
    vendor documentation and the organisers' credentials. If it returns JSON,
    we accept base64 PCM in `audio`/`data.audio`; if it returns raw audio we
    use it directly. The client also has a local speech fallback.
    """

    name = "yarn"
    simulated = False

    def __init__(self, settings: Settings):
        if not settings.yarn_tts_url:
            raise ProviderNotConfigured("YARN_TTS_URL is not set")
        self.settings = settings

    async def synthesize(self, text: str, voice: str = "", language: str = "en") -> "AudioResult":
        from .base import AudioResult

        start = time.perf_counter()
        payload = {
            "text": text,
            "voice": voice or self.settings.yarn_tts_voice,
            "language": language,
            "format": "pcm_s16le",
        }
        headers = {"Authorization": f"Bearer {self.settings.yarn_api_key}"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(self.settings.yarn_tts_url, json=payload, headers=headers)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                sample_rate = int(resp.headers.get("x-sample-rate", "24000"))
                if "application/json" in ctype:
                    data = resp.json()
                    raw = data.get("audio") or data.get("data", {}).get("audio") or ""
                    pcm = base64.b64decode(raw)
                    sample_rate = int(data.get("sample_rate", sample_rate))
                else:
                    pcm = resp.content
                    if pcm[:4] == b"RIFF":
                        pcm, sample_rate = _wav_to_pcm(pcm)
        except Exception:
            return AudioResult(b"", 24000, self.name, text=text, simulated=False)
        elapsed = int((time.perf_counter() - start) * 1000)
        return AudioResult(pcm, sample_rate, self.name, ttfa_ms=elapsed, total_ms=elapsed, text=text)


def _wav_to_pcm(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        rate = wf.getframerate()
        return wf.readframes(wf.getnframes()), rate


class MockTTS:
    """No vendor key: the PWA speaks with the device's local speech engine.

    Returning empty PCM is intentional. The server still sends the exact text
    in prompt.play, so the client can speak it and the UI always shows it.
    """

    name = "mock"
    simulated = True

    async def synthesize(self, text: str, voice: str = "", language: str = "en") -> "AudioResult":
        from .base import AudioResult

        return AudioResult(b"", 24000, self.name, simulated=True, text=text)


def build_tts(settings: Settings):
    try:
        if settings.tts_provider == "yarn":
            return YarnTTS(settings)
    except ProviderNotConfigured:
        pass
    return MockTTS()
