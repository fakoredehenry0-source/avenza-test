"""Deterministic text hints from a transcript: recipient name, bank, account number.

These are only hints. The bank's name enquiry gives the verified name, and the
customer hears it read back before anything can move.
"""
from __future__ import annotations

import re
from typing import Optional

from ..parsing.amount import parse_account_number  # noqa: F401  (re-exported)
from ..parsing.lexicon import build_lexicon, normalize

BANK_ALIASES = {
    "gtbank": "GTBank", "gtb": "GTBank", "guaranty trust": "GTBank", "access bank": "Access Bank",
    "zenith": "Zenith Bank", "uba": "UBA", "first bank": "First Bank", "firstbank": "First Bank",
    "opay": "OPay", "palmpay": "PalmPay", "kuda": "Kuda", "moniepoint": "Moniepoint",
    "wema": "Wema Bank", "fidelity": "Fidelity Bank", "sterling": "Sterling Bank",
    "union bank": "Union Bank", "polaris": "Polaris Bank", "stanbic": "Stanbic IBTC",
    "ecobank": "Ecobank", "fcmb": "FCMB", "keystone": "Keystone Bank", "providus": "Providus Bank",
}
_STOP = {"at", "in", "on", "with", "from", "via", "using", "account", "acct", "bank", "number", "please",
         "abeg", "now", "today", "tomorrow", "tonight", "and", "by", "through", "naira", "ngn", "for",
         "to", "thousand", "hundred", "million"}
_BAD_FIRST = {"the", "a", "an", "me", "myself", "it", "am", "him", "her", "them", "this", "that"}
_NUMBER_WORDS = set(build_lexicon(()).keys())


def _clean_token(w: str) -> str:
    return re.sub(r"[^\w'\u2019\-.]", "", w, flags=re.UNICODE)


def extract_recipient_hint(transcript: str) -> Optional[str]:
    """'send 5000 to Aisha Bello at GTBank' -> 'Aisha Bello'. None if no usable name."""
    words = transcript.strip().split()
    for i, w in enumerate(words):
        if re.sub(r"[^a-z]", "", w.lower()) not in ("to", "for") or i + 1 >= len(words):
            continue
        first = re.sub(r"[^a-z]", "", words[i + 1].lower())
        if first in _BAD_FIRST or first in _STOP:
            continue
        name = []
        for x in words[i + 1:]:
            norm = re.sub(r"[^a-z'\u2019\-]", "", normalize(x))
            if not norm or norm in _STOP or norm in _NUMBER_WORDS or x[0].isdigit() or x[0] == "\u20a6":
                break
            name.append(_clean_token(x))
            if x[-1] in ",;.!?" or len(name) >= 5:
                break
        name = [n for n in name if n]
        if name:
            return " ".join(name).strip(" .,-")
    return None


def extract_bank_hint(transcript: str) -> Optional[str]:
    t = " " + " ".join(re.findall(r"[a-z]+", normalize(transcript))) + " "
    hits = {canon for alias, canon in BANK_ALIASES.items() if f" {alias} " in t}
    return hits.pop() if len(hits) == 1 else None


def grounded_in(transcript: str, candidate: Optional[str]) -> bool:
    """True if `candidate` really appears in the transcript (guards against model inventions)."""
    if not candidate or not candidate.strip():
        return False
    norm = lambda s: " ".join(re.findall(r"[a-z]+", normalize(s)))  # noqa: E731
    c = norm(candidate)
    return bool(c) and f" {c} " in f" {norm(transcript)} "
