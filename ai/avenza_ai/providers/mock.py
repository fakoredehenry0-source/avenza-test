"""Mock providers for development, tests and the language-test dry run."""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Iterable, Optional, Union

from ..types import (ProviderError, STTProvider, STTStream, TTSProvider, TTSResult, Timing,
                     TranscriptEvent, UnsupportedLanguage)
from .audio import tone_pcm

Script = Union[str, Iterable[str], Callable[[bytes, str], str]]


class _MockStream(STTStream):
    def __init__(self, provider: "MockSTTProvider", language: str):
        super().__init__()
        self._p, self._lang, self._audio = provider, language, bytearray()
        self._task: Optional[asyncio.Task] = None

    async def _send(self, pcm: bytes) -> None:
        self._audio.extend(pcm)

    async def _finish(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        p = self._p
        try:
            if p.fail_with is not None:
                raise p.fail_with
            text = p._next_text(bytes(self._audio), self._lang)
            await asyncio.sleep(p.first_partial_delay)
            words = text.split()
            if len(words) > 1:
                self._emit(TranscriptEvent(" ".join(words[: len(words) // 2]), False, p.confidence))
            await asyncio.sleep(p.final_delay)
            self._emit(TranscriptEvent(text, True, p.confidence))
        except BaseException as exc:  # noqa: BLE001 - surfaced through events()
            self._emit_error(exc)
            return
        self._emit_end()

    async def aclose(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._emit_end()


class MockSTTProvider(STTProvider):
    """Returns scripted transcripts: a string, a list used in order, or a callable(pcm, language)."""

    def __init__(self, script: Script = "", *, name: str = "mock", first_partial_delay: float = 0.005,
                 final_delay: float = 0.01, confidence: Optional[float] = 0.95,
                 fail_with: Optional[BaseException] = None, languages: Optional[Iterable[str]] = None):
        self.name = name
        self._script = iter(script) if not isinstance(script, (str, bytes)) and not callable(script) else script
        self.first_partial_delay, self.final_delay = first_partial_delay, final_delay
        self.confidence, self.fail_with = confidence, fail_with
        self._languages = set(languages) if languages is not None else None

    def _next_text(self, pcm: bytes, language: str) -> str:
        s = self._script
        if callable(s):
            return s(pcm, language)
        if isinstance(s, str):
            return s
        try:
            return next(s)
        except StopIteration:
            return ""

    def supports(self, language: str) -> bool:
        return self._languages is None or language in self._languages

    def stream(self, language_hint: str, sample_rate: int) -> STTStream:
        if not self.supports(language_hint):
            raise UnsupportedLanguage(f"{self.name} does not support {language_hint}")
        return _MockStream(self, language_hint)


class MockTTSProvider(TTSProvider):
    name = "mock"

    def __init__(self, sample_rate: int = 24000, fail_with: Optional[BaseException] = None):
        self.sample_rate, self.fail_with, self.calls = sample_rate, fail_with, []

    async def synthesize(self, text: str, voice: Optional[str], language: str) -> TTSResult:
        t0 = time.perf_counter()
        self.calls.append((text, voice, language))
        if self.fail_with is not None:
            raise self.fail_with
        await asyncio.sleep(0)
        pcm = tone_pcm(min(0.05 + 0.01 * len(text), 1.0), self.sample_rate)
        return TTSResult(pcm, self.sample_rate, Timing(time_to_first_audio_ms=(time.perf_counter() - t0) * 1000))
