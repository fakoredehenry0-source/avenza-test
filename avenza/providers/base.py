from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    confidence: float
    provider: str
    ttfp_ms: int = 0
    final_ms: int = 0
    language: str = "en"
    simulated: bool = False
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True)
class AudioResult:
    pcm: bytes
    sample_rate: int
    provider: str
    ttfa_ms: int = 0
    total_ms: int = 0
    format: str = "pcm_s16le"
    simulated: bool = False
    text: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.pcm


class ProviderNotConfigured(RuntimeError):
    pass


class STTStream(ABC):
    @abstractmethod
    async def push(self, pcm: bytes) -> None: ...

    @abstractmethod
    async def end(self) -> TranscriptResult: ...


class STTProvider(ABC):
    name: str = "base"
    simulated: bool = False

    @abstractmethod
    async def transcribe(
        self,
        pcm: bytes,
        sample_rate: int,
        language_hint: str = "en",
        provided_text: str | None = None,
    ) -> TranscriptResult: ...

    def stream(self, language_hint: str = "en", sample_rate: int = 16000) -> STTStream:
        return _BufferedStream(self, language_hint, sample_rate)


class _BufferedStream(STTStream):
    def __init__(self, provider: STTProvider, language_hint: str, sample_rate: int):
        self.provider = provider
        self.language_hint = language_hint
        self.sample_rate = sample_rate
        self.buffer = bytearray()

    async def push(self, pcm: bytes) -> None:
        self.buffer.extend(pcm)

    async def end(self) -> TranscriptResult:
        return await self.provider.transcribe(bytes(self.buffer), self.sample_rate, self.language_hint)


class TTSProvider(ABC):
    name: str = "base"
    simulated: bool = False

    @abstractmethod
    async def synthesize(self, text: str, voice: str = "", language: str = "en") -> AudioResult: ...
