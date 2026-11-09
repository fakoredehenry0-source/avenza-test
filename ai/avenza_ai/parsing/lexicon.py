"""Number words and text normalisation for the deterministic amount parser.

English (and Nigerian Pidgin, which uses English number words) is always on.
Yoruba, Hausa and Igbo number words are OFF unless the caller enables that
language, so a language only counts once it has passed the language test.

UNVERIFIED: the Yoruba, Hausa and Igbo tables below were written from general
knowledge and MUST be reviewed by a native speaker before any language claim.
Forms the parser cannot read with full confidence are rejected, never guessed.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, Tuple

UNIT = "unit"          # 1-9
TEEN = "teen"          # 10-19 (a whole number, nothing is added to it)
TENS = "tens"          # 20, 30 ... 90 (a unit may follow)
HUNDRED = "hundred"    # postfix: "five hundred"
SCALE = "scale"        # postfix: "five thousand", "two million"
PSCALE = "pscale"      # prefix scale used by Hausa/Igbo/Yoruba: "dubu biyar" = thousand five = 5000
CONN = "conn"          # English "and"
CONN_LOC = "conn_loc"  # Hausa da/sha, Igbo na


@dataclass(frozen=True)
class Entry:
    kind: str
    value: int
    lang: str
    max_mult: int = 9  # for PSCALE: the largest multiplier that may follow


def _table(lang: str, kind: str, words: Dict[str, int], **kw) -> Dict[str, Entry]:
    return {w: Entry(kind, v, lang, **kw) for w, v in words.items()}


def _english() -> Dict[str, Entry]:
    t: Dict[str, Entry] = {}
    t.update(_table("en", UNIT, {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                                 "six": 6, "seven": 7, "eight": 8, "nine": 9}))
    t.update(_table("en", TEEN, {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
                                 "fourteen": 14, "fifteen": 15, "sixteen": 16,
                                 "seventeen": 17, "eighteen": 18, "nineteen": 19}))
    t.update(_table("en", TENS, {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40,
                                 "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                                 "ninety": 90}))
    t["hundred"] = Entry(HUNDRED, 100, "en")
    t["thousand"] = Entry(SCALE, 1_000, "en")
    t["million"] = Entry(SCALE, 1_000_000, "en")
    t["and"] = Entry(CONN, 0, "en")
    return t


def _hausa() -> Dict[str, Entry]:
    t: Dict[str, Entry] = {}
    t.update(_table("ha", UNIT, {"daya": 1, "biyu": 2, "uku": 3, "hudu": 4, "biyar": 5,
                                 "shida": 6, "bakwai": 7, "takwas": 8, "tara": 9}))
    t.update(_table("ha", TENS, {"goma": 10, "ashirin": 20, "talatin": 30, "arbain": 40,
                                 "hamsin": 50, "sittin": 60, "sabain": 70, "tamanin": 80,
                                 "casain": 90, "tisain": 90}))
    t["dari"] = Entry(PSCALE, 100, "ha", max_mult=9)
    t["dubu"] = Entry(PSCALE, 1_000, "ha", max_mult=99)
    t["miliyan"] = Entry(PSCALE, 1_000_000, "ha", max_mult=99)
    t["da"] = Entry(CONN_LOC, 0, "ha")
    t["sha"] = Entry(CONN_LOC, 0, "ha")
    return t


def _igbo() -> Dict[str, Entry]:
    t: Dict[str, Entry] = {}
    t.update(_table("ig", UNIT, {"otu": 1, "abuo": 2, "ato": 3, "ano": 4, "ise": 5,
                                 "isii": 6, "asaa": 7, "asato": 8, "itoolu": 9}))
    t["iri"] = Entry(PSCALE, 10, "ig", max_mult=9)
    t["nari"] = Entry(PSCALE, 100, "ig", max_mult=9)
    t["puku"] = Entry(PSCALE, 1_000, "ig", max_mult=99)
    t["na"] = Entry(CONN_LOC, 0, "ig")
    return t


def _yoruba() -> Dict[str, Entry]:
    t: Dict[str, Entry] = {}
    t.update(_table("yo", UNIT, {
        "okan": 1, "kan": 1, "eji": 2, "meji": 2, "eta": 3, "meta": 3, "erin": 4, "merin": 4,
        "arun": 5, "marunun": 5, "efa": 6, "mefa": 6, "eje": 7, "meje": 7, "ejo": 8, "mejo": 8,
        "esan": 9, "mesan": 9, "mesanan": 9}))
    t.update(_table("yo", TEEN, {"ewa": 10, "ewaa": 10, "mewa": 10, "mewaa": 10}))
    t.update(_table("yo", TENS, {"ogun": 20, "ogbon": 30, "ogoji": 40, "aadota": 50,
                                 "ogota": 60, "aadorin": 70, "ogorin": 80, "aadorunun": 90}))
    # 100 only as a bare word: Yoruba hundreds above this are irregular and are rejected.
    t["ogorunun"] = Entry(PSCALE, 100, "yo", max_mult=1)
    t["egberun"] = Entry(PSCALE, 1_000, "yo", max_mult=99)
    return t


_LOCAL = {"ha": _hausa, "ig": _igbo, "yo": _yoruba}


@lru_cache(maxsize=32)
def build_lexicon(languages: Tuple[str, ...]) -> Dict[str, Entry]:
    lex = _english()
    for lang in languages:
        if lang in _LOCAL:
            lex.update(_LOCAL[lang]())
    return lex


CURRENCY_MARKERS = frozenset({"naira", "nairas", "ngn"})
SPOKEN_DIGITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9}

_SPECIAL = {"\u0257": "d", "\u0199": "k", "\u0253": "b", "'": "", "\u2019": "", "\u02bc": "", "`": ""}
_HYPHEN_JOIN = ("marun-un", "ogorun-un", "aadorun-un", "mesan-an")
_TOKEN_RE = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?|[a-z]+|[^\sa-z\d]")


def normalize(text: str) -> str:
    """Lower-case, strip tone marks and dots under letters, unify naira symbols."""
    t = text.lower().replace("\u20a6", " naira ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    for k, v in _SPECIAL.items():
        t = t.replace(k, v)
    for j in _HYPHEN_JOIN:
        t = t.replace(j, j.replace("-", ""))
    t = re.sub(r"(?<![a-z])n(?=\d)", " naira ", t)  # "N5000"
    return t.replace("-", " ")


def tokenize(normalized: str) -> list:
    return _TOKEN_RE.findall(normalized)


def words(text: str) -> list:
    """Plain a-z words after normalisation (used by the intent keyword matcher)."""
    return re.findall(r"[a-z]+", normalize(text))
