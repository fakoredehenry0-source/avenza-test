"""Shared types and the three provider interfaces from section 5.3 of the design doc.

STTProvider.stream(language_hint, sample_rate) -> async partial/final transcript events
TTSProvider.synthesize(text, voice, language) -> PCM audio and its sample rate
IntentClassifier.classify(transcript, state) -> intent plus fields, or unclear

Every provider returns timing data so latency can be measured per stage.
"""
from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional

SUPPORTED_LANGUAGES = ("en", "pcm", "yo", "ha", "ig")
LOCAL_LANGUAGES = ("yo", "ha", "ig")


class ProviderError(RuntimeError):
    """A speech or language provider failed (network, auth, rate limit, bad reply)."""


class UnsupportedLanguage(ProviderError):
    """The provider does not offer this language."""


class NotConfigured(ProviderError):
    """Required credentials or URLs are missing."""


@dataclass(frozen=True)
class Timing:
    """Latency in milliseconds.

    STT: time_to_first_partial_ms is measured from the first audio byte sent;
    time_to_final_ms is measured from finish() (end of speech) to the last final
    transcript, which is the delay a customer actually feels.
    TTS: time_to_first_audio_ms is measured from the request start.
    """

    time_to_first_partial_ms: Optional[float] = None
    time_to_final_ms: Optional[float] = None
    time_to_first_audio_ms: Optional[float] = None


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    confidence: Optional[float] = None


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    confidence: Optional[float]
    events: tuple
    timing: Timing


@dataclass(frozen=True)
class TTSResult:
    pcm: bytes  # 16-bit mono little-endian
    sample_rate: int
    timing: Timing


_END = object()


class STTStream(abc.ABC):
    """One speech-to-text stream per turn. Providers implement _send and _finish."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._t_first_audio: Optional[float] = None
        self._t_finish: Optional[float] = None
        self._t_first_partial: Optional[float] = None
        self._t_last_final: Optional[float] = None
        self._ended = False

    # ---- called by the orchestrator -------------------------------------------------
    async def send_audio(self, pcm: bytes) -> None:
        if self._t_first_audio is None:
            self._t_first_audio = time.perf_counter()
        await self._send(pcm)

    async def finish(self) -> None:
        """Signal the end of speech (VAD end)."""
        self._t_finish = time.perf_counter()
        await self._finish()

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        self._emit_end()

    @property
    def timing(self) -> Timing:
        first = None
        if self._t_first_partial is not None and self._t_first_audio is not None:
            first = max(0.0, (self._t_first_partial - self._t_first_audio) * 1000)
        final = None
        if self._t_last_final is not None and self._t_finish is not None:
            final = max(0.0, (self._t_last_final - self._t_finish) * 1000)
        return Timing(time_to_first_partial_ms=first, time_to_final_ms=final)

    # ---- called by provider implementations ----------------------------------------
    def _emit(self, event: TranscriptEvent) -> None:
        now = time.perf_counter()
        if event.text.strip() and self._t_first_partial is None:
            self._t_first_partial = now
        if event.is_final:
            self._t_last_final = now
        self._queue.put_nowait(event)

    def _emit_error(self, exc: BaseException) -> None:
        if not self._ended:
            self._queue.put_nowait(exc)
        self._emit_end()

    def _emit_end(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(_END)

    @abc.abstractmethod
    async def _send(self, pcm: bytes) -> None: ...

    @abc.abstractmethod
    async def _finish(self) -> None: ...


class STTProvider(abc.ABC):
    name: str = "base"
    # True if every final event is the full transcript so far (take the last one).
    cumulative_finals: bool = False

    @abc.abstractmethod
    def stream(self, language_hint: str, sample_rate: int) -> STTStream: ...

    def is_configured(self) -> bool:
        return True

    def supports(self, language: str) -> bool:
        return True

    async def transcribe(
        self,
        pcm: bytes,
        language_hint: str,
        sample_rate: int,
        *,
        chunk_ms: int = 100,
        realtime: bool = False,
        timeout: float = 30.0,
    ) -> TranscriptResult:
        """Convenience wrapper used by the language test: stream a whole utterance."""
        stream = self.stream(language_hint, sample_rate)
        events: list = []

        async def consume() -> None:
            async for ev in stream.events():
                events.append(ev)

        task = asyncio.create_task(consume())
        try:
            step = max(2, int(sample_rate * 2 * chunk_ms / 1000) // 2 * 2)
            for off in range(0, len(pcm), step):
                await stream.send_audio(pcm[off : off + step])
                if realtime:
                    await asyncio.sleep(chunk_ms / 1000)
            await stream.finish()
            await asyncio.wait_for(task, timeout)
        except BaseException:
            task.cancel()
            raise
        finally:
            await stream.aclose()
        finals = [e for e in events if e.is_final and e.text.strip()]
        if not finals:
            text, conf = "", None
        elif self.cumulative_finals:
            text, conf = finals[-1].text.strip(), finals[-1].confidence
        else:
            text = " ".join(e.text.strip() for e in finals)
            confs = [e.confidence for e in finals if e.confidence is not None]
            conf = sum(confs) / len(confs) if confs else None
        return TranscriptResult(text=text, confidence=conf, events=tuple(events), timing=stream.timing)


class TTSProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    async def synthesize(self, text: str, voice: Optional[str], language: str) -> TTSResult: ...

    def is_configured(self) -> bool:
        return True


class Intent(str, Enum):
    TRANSFER = "transfer"
    YES = "yes"
    NO = "no"
    REPEAT = "repeat"
    CANCEL = "cancel"
    HISTORY = "history"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class IntentResult:
    """Intent plus text hints. Never carries an amount: amounts come only from the parser."""

    intent: Intent
    fields: dict = field(default_factory=dict)  # recipient_name, bank (strings from the transcript)
    source: str = "keyword"  # keyword | llm | keyword_fallback | gated | empty
    reason: Optional[str] = None


class IntentClassifier(abc.ABC):
    @abc.abstractmethod
    async def classify(self, transcript: str, state: Optional[str] = None) -> IntentResult: ...
