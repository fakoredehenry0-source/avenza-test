"""Small audio helpers: WAV <-> PCM16 mono."""
from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path
from typing import Tuple

from ..types import ProviderError


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_bytes_to_pcm(data: bytes) -> Tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ProviderError("expected mono 16-bit WAV audio")
        return w.readframes(w.getnframes()), w.getframerate()


def read_wav_file(path: Path, expected_rate: int = 16000) -> Tuple[bytes, int]:
    """Read mono 16-bit WAV. Convert other files first:
    ffmpeg -i in.m4a -ac 1 -ar 16000 -sample_fmt s16 out.wav
    """
    pcm, rate = wav_bytes_to_pcm(Path(path).read_bytes())
    if expected_rate and rate != expected_rate:
        raise ProviderError(f"{path}: sample rate {rate}, expected {expected_rate}")
    return pcm, rate


def decode_audio_response(data: bytes, default_rate: int) -> Tuple[bytes, int]:
    """WAV (RIFF) or raw PCM16 mono. MP3/Opus are not decoded here: ask the API for wav or pcm."""
    if data[:4] == b"RIFF":
        return wav_bytes_to_pcm(data)
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or data[:4] == b"OggS":
        raise ProviderError("provider returned compressed audio; request wav or pcm")
    return data, default_rate


def tone_pcm(duration_s: float, sample_rate: int, freq: float = 440.0, amp: float = 0.2) -> bytes:
    n = int(duration_s * sample_rate)
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / sample_rate)))
                    for i in range(n))
