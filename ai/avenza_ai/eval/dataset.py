"""Language-test manifest: 10 sentences each in English, Yoruba, Hausa and Igbo."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Sample:
    id: str
    language: str
    text: str                      # reference transcript (what the speaker was asked to say)
    audio_file: str                # path relative to the audio directory, 16 kHz mono WAV
    expected_kobo: Optional[int]
    expected_name: Optional[str]
    tags: Tuple[str, ...] = ()     # e.g. noise, code_switch, similar_name, teen_ty


def load_manifest(path: Path) -> List[Sample]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Sample(r["id"], r["language"], r["text"], r["audio_file"], r.get("expected_kobo"),
                   r.get("expected_name"), tuple(r.get("tags", []))) for r in raw["samples"]]
