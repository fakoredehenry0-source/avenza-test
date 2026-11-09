"""Metrics for the language and latency test."""
from __future__ import annotations

import re
import statistics
from typing import Iterable, List, Optional, Sequence

from ..parsing.amount import AmountStatus, parse_amount
from ..parsing.lexicon import normalize


def normalize_words(text: str) -> List[str]:
    """Lower-case, no tone marks, no punctuation (ASR often drops tone marks)."""
    return re.findall(r"[a-z0-9]+", normalize(text))


def edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def word_errors(ref: str, hyp: str):
    """(errors, reference_word_count); sum both over a set, then divide, for a micro-averaged WER."""
    r, h = normalize_words(ref), normalize_words(hyp)
    return edit_distance(r, h), len(r)


def word_error_rate(ref: str, hyp: str) -> float:
    e, n = word_errors(ref, hyp)
    return e / n if n else (0.0 if not normalize_words(hyp) else 1.0)


def amount_outcome(expected_kobo: Optional[int], hyp: str, languages: Iterable[str]) -> str:
    """'correct' | 'rejected' (parser asked again: safe) | 'wrong' (a wrong amount was accepted: dangerous)."""
    res = parse_amount(hyp, languages=languages)
    if res.status is not AmountStatus.OK:
        return "rejected"
    return "correct" if res.kobo == expected_kobo else "wrong"


def name_correct(expected: Optional[str], hyp: str) -> Optional[bool]:
    """Whole expected name present, as words in order. None if the sample has no name."""
    if not expected:
        return None
    e, h = normalize_words(expected), normalize_words(hyp)
    return any(h[i:i + len(e)] == e for i in range(len(h) - len(e) + 1))


def median(xs: Sequence[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def percentile(xs: Sequence[float], p: float) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)
