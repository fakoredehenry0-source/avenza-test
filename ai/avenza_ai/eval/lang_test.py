"""Language and latency test.

    python -m avenza_ai.eval.lang_test --audio-dir recordings --providers deepgram,yarngpt,sahara

Record each manifest sentence as <audio-dir>/<audio_file> (16 kHz mono WAV), run it,
and read reports/report.md. Reports word accuracy, amount accuracy (split into
correct / rejected / WRONG), name accuracy and delay per provider and language, and
writes reports/provider_choice.json: the provider per language, and which languages
PASSED. Only passing languages may be offered at Demo Day.

Amount "wrong" means a wrong amount was accepted by the parser. That is the number
that matters most; "rejected" only means the customer is asked to repeat.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..providers.audio import read_wav_file
from ..providers.registry import FACTORIES
from ..types import STTProvider
from . import metrics
from .dataset import Sample, load_manifest

DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "data" / "language_test" / "manifest.json"
LANGS = ("en", "yo", "ha", "ig")


async def run_samples(samples: List[Sample], providers: List[STTProvider], audio_dir: Path, *,
                      enabled_number_languages: Iterable[str] = LANGS, sample_rate: int = 16000,
                      realtime: bool = False, chunk_ms: int = 100) -> List[dict]:
    rows: List[dict] = []
    for prov in providers:
        for s in samples:
            row = dict(provider=prov.name, sample_id=s.id, language=s.language, tags=",".join(s.tags),
                       reference=s.text, hypothesis="", status="ok", error="", wer_errors=None, ref_words=None,
                       amount_outcome=None, name_ok=None, first_partial_ms=None, final_ms=None)
            if not prov.is_configured():
                row.update(status="not_configured")
            elif not prov.supports(s.language):
                row.update(status="unsupported_language")
            else:
                try:
                    pcm, rate = read_wav_file(audio_dir / s.audio_file, sample_rate)
                    t0 = time.perf_counter()
                    res = await prov.transcribe(pcm, s.language, rate, chunk_ms=chunk_ms, realtime=realtime)
                    e, n = metrics.word_errors(s.text, res.text)
                    row.update(hypothesis=res.text, wer_errors=e, ref_words=n,
                               amount_outcome=metrics.amount_outcome(s.expected_kobo, res.text, enabled_number_languages)
                               if s.expected_kobo is not None else None,
                               name_ok=metrics.name_correct(s.expected_name, res.text),
                               first_partial_ms=res.timing.time_to_first_partial_ms,
                               final_ms=res.timing.time_to_final_ms,
                               wall_ms=(time.perf_counter() - t0) * 1000)
                except FileNotFoundError:
                    row.update(status="missing_audio")
                except Exception as exc:  # noqa: BLE001 - recorded, never hidden
                    row.update(status="error", error=f"{type(exc).__name__}: {exc}")
            rows.append(row)
    return rows


def summarise(rows: List[dict]) -> Dict[tuple, dict]:
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["provider"], r["language"])].append(r)
    out = {}
    for key, rs in groups.items():
        ok = [r for r in rs if r["status"] == "ok"]
        amt = [r for r in ok if r["amount_outcome"] is not None]
        nm = [r for r in ok if r["name_ok"] is not None]
        ref = sum(r["ref_words"] for r in ok)
        out[key] = dict(
            samples=len(rs), scored=len(ok),
            skipped=len(rs) - len(ok),
            statuses=sorted({r["status"] for r in rs if r["status"] != "ok"}),
            wer=(sum(r["wer_errors"] for r in ok) / ref) if ref else None,
            amount_correct=(sum(r["amount_outcome"] == "correct" for r in amt) / len(amt)) if amt else None,
            amount_rejected=(sum(r["amount_outcome"] == "rejected" for r in amt) / len(amt)) if amt else None,
            amount_wrong=(sum(r["amount_outcome"] == "wrong" for r in amt) / len(amt)) if amt else None,
            name_accuracy=(sum(bool(r["name_ok"]) for r in nm) / len(nm)) if nm else None,
            first_partial_median_ms=metrics.median([r["first_partial_ms"] for r in ok if r["first_partial_ms"] is not None]),
            final_median_ms=metrics.median([r["final_ms"] for r in ok if r["final_ms"] is not None]),
            final_p90_ms=metrics.percentile([r["final_ms"] for r in ok if r["final_ms"] is not None], 90),
        )
    return out


def choose(summary: Dict[tuple, dict], *, min_scored: int = 10, min_amount_correct: float = 0.9,
           max_amount_wrong: float = 0.0, min_name_accuracy: float = 0.8) -> dict:
    """Best passing provider per language. Pass = enough samples scored, amounts mostly right,
    no wrong amount accepted, names mostly right. Rank: lowest WER, then lowest final delay."""
    thresholds = dict(min_scored=min_scored, min_amount_correct=min_amount_correct,
                      max_amount_wrong=max_amount_wrong, min_name_accuracy=min_name_accuracy)
    choice: Dict[str, str] = {}
    failed: Dict[str, str] = {}
    for lang in LANGS:
        cands = []
        for (prov, l), s in summary.items():
            if l != lang:
                continue
            if s["scored"] < min_scored:
                continue
            if (s["amount_correct"] or 0) < min_amount_correct or (s["amount_wrong"] or 0) > max_amount_wrong \
                    or (s["name_accuracy"] if s["name_accuracy"] is not None else 1) < min_name_accuracy:
                continue
            cands.append(((s["wer"] if s["wer"] is not None else 9, s["final_median_ms"] or 1e9), prov))
        if cands:
            choice[lang] = min(cands)[1]
        else:
            failed[lang] = "no provider passed (or too few samples were scored)"
    if "en" in choice:
        choice["pcm"] = choice["en"]   # Pidgin reuses the English provider; still say it is unmeasured
    return {"choice": choice, "enabled_languages": sorted(choice), "not_passed": failed,
            "note": "pcm (Pidgin) is not in the test set: treat it as unproven until measured",
            "thresholds": thresholds}


def _fmt(v, pct=False, ms=False):
    if v is None:
        return "-"
    return f"{v * 100:.0f}%" if pct else (f"{v:.0f} ms" if ms else f"{v:.2f}")


def write_reports(rows: List[dict], out_dir: Path, **choose_kw) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarise(rows)
    result = choose(summary, **choose_kw)
    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        cols = ["provider", "sample_id", "language", "tags", "status", "error", "reference", "hypothesis",
                "wer_errors", "ref_words", "amount_outcome", "name_ok", "first_partial_ms", "final_ms"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    lines = ["# Language and latency test", "",
             "Measured numbers only. Amount wrong = a wrong amount was ACCEPTED (must be 0).",
             "Delay = end of speech to final transcript. Add the VAD silence tolerance separately.", "",
             "| Provider | Lang | Scored/Total | WER | Amount correct | Rejected | WRONG | Name acc. | First partial | Final (median / p90) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for (prov, lang), s in sorted(summary.items()):
        note = f" ({', '.join(s['statuses'])})" if s["statuses"] else ""
        lines.append(f"| {prov} | {lang} | {s['scored']}/{s['samples']}{note} | {_fmt(s['wer'], True)} | "
                     f"{_fmt(s['amount_correct'], True)} | {_fmt(s['amount_rejected'], True)} | {_fmt(s['amount_wrong'], True)} | "
                     f"{_fmt(s['name_accuracy'], True)} | {_fmt(s['first_partial_median_ms'], ms=True)} | "
                     f"{_fmt(s['final_median_ms'], ms=True)} / {_fmt(s['final_p90_ms'], ms=True)} |")
    lines += ["", "## Decision", ""]
    for lang in LANGS:
        lines.append(f"- {lang}: " + (f"**PASS** with {result['choice'][lang]}" if lang in result["choice"]
                                       else f"NOT PASSED ({result['not_passed'][lang]}). Do not demo or claim this language."))
    lines += ["", f"Thresholds: {json.dumps(result['thresholds'])}", ""]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "provider_choice.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    ap.add_argument("--audio-dir", type=Path, required=True)
    ap.add_argument("--providers", default="deepgram,yarngpt,sahara")
    ap.add_argument("--out", type=Path, default=Path("reports"))
    ap.add_argument("--realtime", action="store_true", help="send audio at real-time speed (slower, realistic delay)")
    ap.add_argument("--enable-number-languages", default="yo,ha,ig",
                    help="languages whose number words the parser may use while scoring")
    a = ap.parse_args(argv)
    providers = [FACTORIES[n.strip()]() for n in a.providers.split(",") if n.strip()]
    rows = asyncio.run(run_samples(load_manifest(a.manifest), providers, a.audio_dir, realtime=a.realtime,
                                   enabled_number_languages=a.enable_number_languages.split(",")))
    result = write_reports(rows, a.out)
    print(f"Wrote {a.out}/report.md, results.csv, provider_choice.json")
    print("Passed:", ", ".join(result["enabled_languages"]) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
