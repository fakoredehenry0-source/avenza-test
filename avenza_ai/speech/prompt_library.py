"""Cached prompt library: generate fixed prompts once, ship them to the frontend.

Recorded audio wins over generated audio: put a reviewed recording at
<recorded_dir>/<language>/<prompt_id>.wav and it is used instead. Generated files
are keyed by a hash of (text, voice), so changing a sentence regenerates only it.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..providers.audio import pcm_to_wav_bytes, wav_bytes_to_pcm
from ..types import TTSProvider
from .prompts import PROMPT_IDS, get_prompts

MANIFEST = "manifest.json"


def _hash(text: str, voice: Optional[str]) -> str:
    return hashlib.sha256(f"{voice or ''}\x00{text}".encode("utf-8")).hexdigest()[:16]


def load_manifest(out_dir: Path) -> Dict[str, dict]:
    p = Path(out_dir) / MANIFEST
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


async def build_library(tts: TTSProvider, out_dir: Path, languages: Iterable[str] = ("en", "pcm"), *,
                        voice_by_language: Optional[Dict[str, str]] = None,
                        recorded_dir: Optional[Path] = None, force: bool = False) -> Dict[str, dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(out_dir)
    for lang in languages:
        prompts, used = get_prompts(lang)
        voice = (voice_by_language or {}).get(lang)
        for pid in PROMPT_IDS:
            key = f"{lang}/{pid}"
            rel = f"{lang}/{pid}.wav"
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            rec = Path(recorded_dir) / lang / f"{pid}.wav" if recorded_dir else None
            text = prompts[pid].text
            if rec and rec.exists():
                shutil.copyfile(rec, dest)
                _, rate = wav_bytes_to_pcm(dest.read_bytes())
                manifest[key] = {"id": pid, "language": lang, "text": text, "file": rel,
                                 "sample_rate": rate, "source": "recorded", "text_hash": _hash(text, None),
                                 "text_language": used}
                continue
            h = _hash(text, voice)
            cur = manifest.get(key)
            if not force and cur and cur.get("text_hash") == h and cur.get("source") == "generated" and dest.exists():
                continue
            res = await tts.synthesize(text, voice, used)
            dest.write_bytes(pcm_to_wav_bytes(res.pcm, res.sample_rate))
            manifest[key] = {"id": pid, "language": lang, "text": text, "file": rel,
                             "sample_rate": res.sample_rate, "source": "generated", "text_hash": h,
                             "text_language": used}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def verify_library(out_dir: Path, languages: Iterable[str]) -> List[str]:
    """Missing prompt files as 'lang/id'. The frontend needs every one of them offline."""
    out_dir, manifest = Path(out_dir), load_manifest(Path(out_dir))
    missing = []
    for lang in languages:
        for pid in PROMPT_IDS:
            entry = manifest.get(f"{lang}/{pid}")
            if not entry or not (out_dir / entry["file"]).exists():
                missing.append(f"{lang}/{pid}")
    return missing


def export_review_sheet(path: Path, languages: Iterable[str]) -> None:
    """CSV for the native-speaker check of tone and number errors."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["language", "prompt_id", "text", "claims_no_money_moved", "reviewed_ok", "corrected_text"])
        for lang in languages:
            prompts, used = get_prompts(lang)
            for pid in PROMPT_IDS:
                p = prompts[pid]
                w.writerow([lang if used == lang else f"{lang} (English fallback)", pid, p.text,
                            p.claims_no_money_moved, "", ""])
