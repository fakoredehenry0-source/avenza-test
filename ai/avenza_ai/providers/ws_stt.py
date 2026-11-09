"""WebSocket speech-to-text providers: Deepgram (concrete) and YarnGPT/Sahara (CONFIRM scaffolds)."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Dict, List, Optional
from urllib.parse import urlencode

from ..types import (NotConfigured, ProviderError, STTProvider, STTStream, TranscriptEvent,
                     UnsupportedLanguage)


class _WSStream(STTStream):
    def __init__(self, provider: "WebSocketSTTProvider", language: str, sample_rate: int):
        super().__init__()
        self._p, self._lang, self._rate = provider, language, sample_rate
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._open_lock = asyncio.Lock()

    async def _ensure_open(self) -> None:
        async with self._open_lock:
            if self._ws is not None:
                return
            if not self._p.is_configured():
                raise NotConfigured(f"{self._p.name} is not configured (see .env.example)")
            from websockets.asyncio.client import connect  # imported lazily

            try:
                self._ws = await connect(self._p.url(self._lang, self._rate),
                                         additional_headers=self._p.headers(),
                                         open_timeout=self._p.connect_timeout, max_size=None)
                start = self._p.start_message(self._lang, self._rate)
                if start:
                    await self._ws.send(start)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"{self._p.name}: could not connect: {exc}") from exc
            self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for msg in self._ws:
                if isinstance(msg, str):
                    for ev in self._p.parse_message(msg):
                        self._emit(ev)
        except Exception as exc:  # noqa: BLE001
            self._emit_error(ProviderError(f"{self._p.name}: {exc}"))
            return
        self._emit_end()

    async def _send(self, pcm: bytes) -> None:
        await self._ensure_open()
        await self._ws.send(pcm)

    async def _finish(self) -> None:
        await self._ensure_open()
        end = self._p.end_message()
        if end is not None:
            await self._ws.send(end)

    async def aclose(self) -> None:
        if self._reader and not self._reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), 0.05)
            except Exception:  # noqa: BLE001
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader and not self._reader.done():
            self._reader.cancel()
        self._emit_end()


class WebSocketSTTProvider(STTProvider):
    """Subclasses define url/headers/messages. One stream per turn."""

    connect_timeout = 5.0

    def stream(self, language_hint: str, sample_rate: int) -> STTStream:
        if not self.supports(language_hint):
            raise UnsupportedLanguage(f"{self.name} does not support {language_hint}")
        return _WSStream(self, language_hint, sample_rate)

    # hooks -------------------------------------------------------------------------
    def url(self, language: str, sample_rate: int) -> str: raise NotImplementedError
    def headers(self) -> Dict[str, str]: return {}
    def start_message(self, language: str, sample_rate: int) -> Optional[str]: return None
    def end_message(self) -> Optional[str]: return None
    def parse_message(self, raw: str) -> List[TranscriptEvent]: raise NotImplementedError


class DeepgramSTTProvider(WebSocketSTTProvider):
    """Deepgram live streaming (wss://api.deepgram.com/v1/listen).

    Deepgram is the reliable English baseline. It may not cover Yoruba, Hausa or
    Igbo: the language test decides. Override the map with DEEPGRAM_LANG_MAP,
    e.g. 'ha=sw' (never do this without measuring the result).
    """

    name = "deepgram"
    DEFAULT_LANGS = {"en": "en", "pcm": "en"}

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 base_url: str = "wss://api.deepgram.com/v1/listen", endpointing_ms: int = 300,
                 lang_map: Optional[Dict[str, str]] = None):
        self.api_key = api_key if api_key is not None else os.getenv("DEEPGRAM_API_KEY", "")
        self.model = model or os.getenv("DEEPGRAM_MODEL", "nova-2")
        self.base_url, self.endpointing_ms = base_url, endpointing_ms
        self.lang_map = dict(self.DEFAULT_LANGS)
        env_map = os.getenv("DEEPGRAM_LANG_MAP", "")
        for pair in filter(None, env_map.split(",")):
            k, _, v = pair.partition("=")
            self.lang_map[k.strip()] = v.strip()
        if lang_map:
            self.lang_map.update(lang_map)

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def supports(self, language: str) -> bool:
        return language in self.lang_map

    def url(self, language: str, sample_rate: int) -> str:
        q = {"model": self.model, "language": self.lang_map[language], "encoding": "linear16",
             "sample_rate": sample_rate, "channels": 1, "interim_results": "true",
             "punctuate": "false", "endpointing": self.endpointing_ms}
        return f"{self.base_url}?{urlencode(q)}"

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Token {self.api_key}"}

    def end_message(self) -> Optional[str]:
        return json.dumps({"type": "CloseStream"})

    def parse_message(self, raw: str) -> List[TranscriptEvent]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if msg.get("type") == "Error" or "err_code" in msg:
            raise ProviderError(f"deepgram error: {msg.get('description') or msg.get('err_msg') or msg}")
        if msg.get("type") != "Results":
            return []
        alts = (msg.get("channel") or {}).get("alternatives") or []
        if not alts:
            return []
        text = (alts[0].get("transcript") or "").strip()
        if not text:
            return []
        return [TranscriptEvent(text, bool(msg.get("is_final")), alts[0].get("confidence"))]


class _GenericWSProvider(WebSocketSTTProvider):
    """CONFIRM scaffold for providers whose live-ASR protocol is not documented to us yet.

    Assumptions to verify on Day 1 (change the hooks if they differ):
    - URL comes from <PREFIX>_ASR_WS_URL, key from <PREFIX>_API_KEY as a Bearer token.
    - Audio is sent as binary 16-bit mono PCM frames.
    - Replies are JSON with a text under 'transcript' or 'text' and a final flag.
    Until the URL is set, is_configured() is False and the language test skips this provider.
    """

    prefix = "GENERIC"
    cumulative_finals = False

    def __init__(self, url: Optional[str] = None, api_key: Optional[str] = None,
                 languages: Optional[Dict[str, str]] = None):
        self._url = url if url is not None else os.getenv(f"{self.prefix}_ASR_WS_URL", "")
        self._key = api_key if api_key is not None else os.getenv(f"{self.prefix}_API_KEY", "")
        # language code sent to the provider; CONFIRM which of yo/ha/ig/en it accepts
        self.languages = languages or {"en": "en", "yo": "yo", "ha": "ha", "ig": "ig", "pcm": "pcm"}

    def is_configured(self) -> bool:
        return bool(self._url)

    def supports(self, language: str) -> bool:
        return language in self.languages

    def url(self, language: str, sample_rate: int) -> str:
        sep = "&" if "?" in self._url else "?"
        return f"{self._url}{sep}{urlencode({'language': self.languages[language], 'sample_rate': sample_rate})}"

    def headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def parse_message(self, raw: str) -> List[TranscriptEvent]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(msg, dict):
            return []
        if msg.get("error"):
            raise ProviderError(f"{self.name} error: {msg['error']}")
        body = msg.get("result") if isinstance(msg.get("result"), dict) else msg
        text = (body.get("transcript") or body.get("text") or "").strip()
        if not text:
            return []
        final = body.get("is_final", body.get("final", body.get("isFinal", msg.get("type") == "final")))
        return [TranscriptEvent(text, bool(final), body.get("confidence"))]


class YarnGPTSTTProvider(_GenericWSProvider):
    name = "yarngpt"
    prefix = "YARNGPT"


class SaharaSTTProvider(_GenericWSProvider):
    name = "sahara"
    prefix = "SAHARA"
