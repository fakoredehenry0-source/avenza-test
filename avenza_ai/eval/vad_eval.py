"""VAD evaluation and threshold tuning (with the frontend).

The frontend exports, for labelled test clips, the Silero speech probability per frame:

  {"frame_ms": 32, "clips": [
     {"id": "yo_01", "kind": "speech", "noise": "generator", "gt": [[400, 3100]], "probs": [0.01, ...]},
     {"id": "market_noise_1", "kind": "noise", "gt": [], "probs": [...]} ]}

gt is the true start/end of the utterance in ms (one utterance per clip; pauses inside it are allowed).
This tool replays the probabilities through the same start/end rule as the browser
(thresholds + silence tolerance + minimum speech), scores every setting, and picks the one with
the smallest silence tolerance that still meets the limits. It does not replace listening tests.

    python -m avenza_ai.eval.vad_eval --log vad_log.json --emit-config vad_config.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

DEFAULTS = dict(start_threshold=0.5, end_threshold=0.35, silence_ms=800, min_speech_ms=250, pre_roll_ms=300)


@dataclass(frozen=True)
class VadParams:
    start_threshold: float = 0.5
    end_threshold: float = 0.35
    silence_ms: int = 800        # silence tolerance; counts toward the latency budget
    min_speech_ms: int = 250
    pre_roll_ms: int = 300


def simulate(probs: List[float], frame_ms: float, p: VadParams) -> List[Tuple[float, float]]:
    """Detected utterances as (start_ms, end_ms) where end is the last speech frame."""
    segs: List[Tuple[float, float]] = []
    in_speech, run_start, run_len, sil_len, last_speech = False, 0, 0, 0, 0
    for i, pr in enumerate(probs):
        t = i * frame_ms
        if not in_speech:
            if pr >= p.start_threshold:
                if run_len == 0:
                    run_start = t
                run_len += 1
                if run_len * frame_ms >= p.min_speech_ms:
                    in_speech, sil_len, last_speech = True, 0, t + frame_ms
            else:
                run_len = 0
        else:
            if pr >= p.end_threshold:
                sil_len, last_speech = 0, t + frame_ms
            else:
                sil_len += 1
                if sil_len * frame_ms >= p.silence_ms:
                    segs.append((run_start, last_speech))
                    in_speech, run_len = False, 0
    if in_speech:
        segs.append((run_start, last_speech))
    return segs


def score(clips: List[dict], frame_ms: float, p: VadParams, tol_ms: float = 200) -> Dict[str, float]:
    noise = [c for c in clips if c["kind"] == "noise"]
    speech = [c for c in clips if c["kind"] == "speech"]
    false_trig = sum(1 for c in noise if simulate(c["probs"], frame_ms, p))
    missed = cutoff = 0
    late_start: List[float] = []
    for c in speech:
        gs, ge = c["gt"][0]
        segs = [s for s in simulate(c["probs"], frame_ms, p) if s[1] > gs and s[0] < ge]
        if not segs:
            missed += 1
            continue
        if len(segs) > 1 or segs[0][1] < ge - tol_ms:
            cutoff += 1          # sentence split or ended early: words lost
        late_start.append(max(0.0, segs[0][0] - gs))
    n_n, n_s = max(len(noise), 1), max(len(speech), 1)
    return dict(false_trigger_rate=false_trig / n_n, miss_rate=missed / n_s, cutoff_rate=cutoff / n_s,
                max_late_start_ms=max(late_start) if late_start else 0.0, silence_ms=p.silence_ms)


def sweep(clips: List[dict], frame_ms: float, *, starts=(0.4, 0.5, 0.6), ends=(0.25, 0.35),
          silences=(400, 500, 600, 800, 1000, 1200, 1500), min_speeches=(150, 250, 400),
          max_false_trigger=0.05, max_miss=0.02, max_cutoff=0.05):
    table = []
    for st, en, sil, ms in itertools.product(starts, ends, silences, min_speeches):
        if en > st:
            continue
        p = VadParams(st, en, sil, ms)
        s = score(clips, frame_ms, p)
        s.update(params=asdict(p), meets_limits=(s["false_trigger_rate"] <= max_false_trigger and
                                                 s["miss_rate"] <= max_miss and s["cutoff_rate"] <= max_cutoff))
        table.append(s)
    ok = [r for r in table if r["meets_limits"]]
    best = min(ok, key=lambda r: (r["silence_ms"], r["false_trigger_rate"], r["cutoff_rate"])) if ok else None
    return best, table


def recommend_config(best: dict, margin_ms: int = 100) -> dict:
    """Config handed to the frontend. pre_roll covers the slowest speech onset seen, plus a margin."""
    p = dict(best["params"])
    p["pre_roll_ms"] = int(round((best["max_late_start_ms"] + margin_ms) / 50.0) * 50) + 100
    return {**p, "browser_option_names": {
        "start_threshold": "positiveSpeechThreshold", "end_threshold": "negativeSpeechThreshold",
        "silence_ms": "redemptionMs", "min_speech_ms": "minSpeechMs", "pre_roll_ms": "preSpeechPadMs"},
        "note": "Option names are for @ricky0123/vad-web; verify against the installed version. Starting values only."}


def synthesize_clips(seed: int = 7, n_speech: int = 40, n_noise: int = 40, frame_ms: float = 32) -> List[dict]:
    """Synthetic probabilities for tests and demos. Real tuning needs the frontend's recorded log."""
    rng = random.Random(seed)
    clips = []
    for i in range(n_speech):
        lead, tail = rng.randint(8, 20), rng.randint(15, 40)
        probs = [rng.uniform(0, 0.15) for _ in range(lead)]
        blocks = rng.randint(2, 4)
        for w in range(blocks):
            probs += [rng.uniform(0.7, 0.98) for _ in range(rng.randint(12, 30))]
            if w < blocks - 1:   # pause only BETWEEN words/phrases (100-500 ms), never after the last
                probs += [rng.uniform(0.0, 0.2) for _ in range(rng.randint(3, 16))]
        gt_end = len(probs) * frame_ms
        probs += [rng.uniform(0, 0.15) for _ in range(tail)]
        clips.append(dict(id=f"s{i}", kind="speech", gt=[[lead * frame_ms, gt_end]], probs=probs))
    for i in range(n_noise):
        probs = [rng.uniform(0, 0.2) for _ in range(120)]
        if rng.random() < 0.4:
            at = rng.randint(10, 100)
            for k in range(rng.randint(1, 5)):
                probs[at + k] = rng.uniform(0.4, 0.75)       # short noise spike (door, clap)
        clips.append(dict(id=f"n{i}", kind="noise", gt=[], probs=probs))
    return clips


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--emit-config", type=Path)
    ap.add_argument("--report", type=Path, default=Path("reports/vad_sweep.json"))
    a = ap.parse_args(argv)
    data = json.loads(a.log.read_text(encoding="utf-8"))
    best, table = sweep(data["clips"], data["frame_ms"])
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps({"best": best, "table": table}, indent=1), encoding="utf-8")
    if not best:
        print("No setting met the limits. Record more varied clips or relax limits knowingly.")
        return 1
    cfg = recommend_config(best)
    if a.emit_config:
        a.emit_config.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(json.dumps(cfg, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
