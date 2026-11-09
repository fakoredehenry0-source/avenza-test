"""Read-back templates. The text is built from a template, never by a model.

The recipient name is the bank-verified name, read in full (never shortened) so
similar names such as "Aisha Bello" and "Aisha Bello-Okafor" stay distinct.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .numwords import digits_to_words, has_teen_ty_risk, naira_to_words
from .prompts import get_prompts


class ReadbackError(ValueError):
    """Cannot produce a safe read-back (missing name or bad amount): ask a question instead."""


@dataclass(frozen=True)
class Readback:
    text: str
    language_used: str
    spelled_digits: bool


def speakable_name(name: Optional[str]) -> str:
    """Tidy a bank name for speech: 'ADEBAYO  OGUNLEYE' -> 'Adebayo Ogunleye'. Never truncates."""
    if not name or not name.strip():
        raise ReadbackError("verified name is missing")
    cleaned = re.sub(r"[^\w\s'\u2019.\-]", " ", name, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not re.search(r"[^\W\d_]", cleaned, flags=re.UNICODE):
        raise ReadbackError("verified name has no letters")
    if cleaned.isupper() or cleaned.islower():
        cleaned = cleaned.title()
    return cleaned


_TEMPLATES = {
    "en": {
        "readback": "You want to send {amount} to {name}{bank}. {hint}Is that right? Say yes, no, or repeat.",
        "hint": "That is {digits}. ",
        "bank": " at {bank}",
        "settled": "Done. {amount} has been sent to {name}. Your reference is on the screen.",
    },
    "pcm": {
        "readback": "You wan send {amount} to {name}{bank}. {hint}E correct? Talk yes, no, or repeat.",
        "hint": "Na {digits}. ",
        "bank": " for {bank}",
        "settled": "E don go. {amount} don reach {name}. Your reference dey for the screen.",
    },
}


def _template(language: str):
    lang = language if language in _TEMPLATES else "en"
    return lang, _TEMPLATES[lang]


def render_readback(kobo: int, verified_name: Optional[str], *, bank_name: Optional[str] = None,
                    language: str = "en", spell_digits: bool = True) -> Readback:
    if not isinstance(kobo, int) or kobo <= 0:
        raise ReadbackError("amount must be a positive integer number of kobo")
    name = speakable_name(verified_name)
    lang, t = _template(language)
    spell = spell_digits and kobo % 100 == 0 and has_teen_ty_risk(kobo // 100)
    hint = t["hint"].format(digits=digits_to_words(kobo // 100) + " naira") if spell else ""
    bank = t["bank"].format(bank=bank_name.strip()) if bank_name and bank_name.strip() else ""
    text = t["readback"].format(amount=naira_to_words(kobo), name=name, bank=bank, hint=hint)
    return Readback(text=text, language_used=lang, spelled_digits=spell)


def render_settled(kobo: int, verified_name: str, *, language: str = "en") -> str:
    if not isinstance(kobo, int) or kobo <= 0:
        raise ReadbackError("amount must be a positive integer number of kobo")
    _, t = _template(language)
    return t["settled"].format(amount=naira_to_words(kobo), name=speakable_name(verified_name))


def render_failed(language: str = "en") -> str:
    prompts, _ = get_prompts(language)
    return prompts["result_failed"].text
