"""Deterministic amount parser.

Rules that keep this safe:
- No model is involved. The LLM never decides an amount.
- Digits, spoken English/Pidgin numbers and (opt-in) Yoruba/Hausa/Igbo numbers,
  including mixed-language numbers such as "dubu five" or "5 thousand".
- Ambiguity is rejected, never guessed: two amounts, odd grammar ("fifteen
  fifty"), kobo, numbers out of range. The caller then asks the customer again.
- Account numbers and phone numbers (10+ digits, or a long run of spoken
  digits) are never read as amounts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable, List, Optional, Sequence, Tuple

from .lexicon import (CONN, CONN_LOC, CURRENCY_MARKERS, HUNDRED, PSCALE, SCALE, SPOKEN_DIGITS,
                      TEEN, TENS, UNIT, Entry, build_lexicon, normalize, tokenize)

DEFAULT_MIN_KOBO = 100                  # 1 naira
DEFAULT_MAX_KOBO = 10_000_000 * 100     # 10 million naira; the bank keeps its own limits too


class AmountStatus(str, Enum):
    OK = "ok"
    NONE = "none"                  # no amount heard -> ask "how much?"
    AMBIGUOUS = "ambiguous"        # could not read it safely -> ask again
    OUT_OF_RANGE = "out_of_range"


@dataclass(frozen=True)
class AmountResult:
    status: AmountStatus
    kobo: Optional[int] = None
    reason: Optional[str] = None
    matched_text: Optional[str] = None
    has_currency_marker: bool = False
    needs_review: bool = False     # a Yoruba/Hausa/Igbo number word was used
    candidates_kobo: Tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is AmountStatus.OK


class _ParseError(Exception):
    pass


@dataclass
class _Tok:
    text: str
    kind: str
    entry: Optional[Entry] = None

    @property
    def value(self) -> int:
        return self.entry.value if self.entry else 0


_NUMERIC = {"digit", UNIT, TEEN, TENS, HUNDRED, SCALE, PSCALE, "article", "suffix_k"}
_CONNS = {CONN, CONN_LOC}
_NUM_FORMAT = re.compile(r"^(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_ARTICLE_BLOCKERS_PREV = {"the", "which", "that", "this", "any", "every", "each", "another"}
_ARTICLE_BLOCKERS_NEXT = {"of", "who", "which"}


# ----------------------------------------------------------------------------- tokens
def _mask_id_runs(raw: List[str]) -> List[str]:
    """Replace account/phone-like runs with an inert token so they never become amounts."""
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        w = raw[i]
        if re.fullmatch(r"\d+", w):
            j, total = i, 0
            while j < n and re.fullmatch(r"\d+", raw[j]):
                total += len(raw[j])
                j += 1
            if total >= 10:
                out.append("<id>")
            else:
                out.extend(raw[i:j])
            i = j
        elif w in SPOKEN_DIGITS:
            j = i
            while j < n and raw[j] in SPOKEN_DIGITS:
                j += 1
            if j - i >= 6:
                out.append("<id>")
            else:
                out.extend(raw[i:j])
            i = j
        else:
            out.append(w)
            i += 1
    return out


def _classify(raw: List[str], lex) -> List[_Tok]:
    toks: List[_Tok] = []
    for idx, w in enumerate(raw):
        if w == "<id>":
            toks.append(_Tok(w, "id"))
        elif re.fullmatch(r"\d+(?:,\d+)*(?:\.\d+)?", w):
            toks.append(_Tok(w, "digit"))
        elif w == "k" and toks and toks[-1].kind in ("digit", UNIT, TEEN, TENS):
            toks.append(_Tok(w, "suffix_k"))
        elif w in CURRENCY_MARKERS:
            toks.append(_Tok(w, "marker"))
        elif w == "kobo":
            toks.append(_Tok(w, "kobo"))
        elif w in ("a", "an") and idx + 1 < len(raw) and lex.get(raw[idx + 1]) is not None \
                and lex[raw[idx + 1]].kind in (HUNDRED, SCALE) and lex[raw[idx + 1]].lang == "en":
            toks.append(_Tok(w, "article"))
        elif w in lex:
            e = lex[w]
            toks.append(_Tok(w, e.kind, e))
        else:
            toks.append(_Tok(w, "word"))
    return toks


def _spans(toks: List[_Tok]) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i].kind in _NUMERIC and toks[i].kind != "suffix_k":
            j = i + 1
            while j < n:
                if toks[j].kind in _NUMERIC:
                    j += 1
                elif toks[j].kind in _CONNS and j + 1 < n and toks[j + 1].kind in _NUMERIC \
                        and toks[j + 1].kind != "article":
                    j += 2
                else:
                    break
            spans.append((i, j))
            i = j
        else:
            i += 1
    return spans


# ----------------------------------------------------------------------------- grammar
def _parse_pre(t: Sequence[_Tok], i: int):
    """unit | teen | tens [conn_loc unit]  ->  (value, next_index) or (None, i)."""
    n = len(t)
    if i >= n:
        return None, i
    k = t[i].kind
    if k == TEEN:
        return t[i].value, i + 1
    if k == UNIT:
        return t[i].value, i + 1
    if k == TENS:
        j = i + 1
        m = j
        if m < n and t[m].kind == CONN_LOC:
            m += 1
        if m < n and t[m].kind == UNIT:
            return t[i].value + t[m].value, m + 1
        return t[i].value, j
    return None, i


def _parse_group(t: Sequence[_Tok], i: int):
    n = len(t)
    pre, j = _parse_pre(t, i)
    if pre is None:
        raise _ParseError("invalid_number_grammar")
    if j < n and t[j].kind == HUNDRED:
        if not 1 <= pre <= 99:
            raise _ParseError("invalid_number_grammar")
        val = pre * 100
        j += 1
        k = j
        if k < n and t[k].kind in _CONNS:
            k += 1
        if k < n:
            tail, k2 = _parse_pre(t, k)
            if tail is not None:
                val += tail
                j = k2
        return val, j
    return pre, j


def _parse_postfix(t: List[_Tok]) -> int:
    n = len(t)
    if t and t[0].kind == "article":
        t = [_Tok("a", UNIT, Entry(UNIT, 1, "en"))] + t[1:]
    if any(x.kind == "article" for x in t):
        raise _ParseError("invalid_number_grammar")
    i, total, last_scale = 0, 0, None
    while i < n:
        val, i = _parse_group(t, i)
        if i < n and t[i].kind == SCALE:
            if val >= 1000:
                raise _ParseError("invalid_number_grammar")
            s = t[i].value
            if last_scale is not None and s >= last_scale:
                raise _ParseError("invalid_number_grammar")
            total += val * s
            last_scale = s
            i += 1
            if i < n and t[i].kind in _CONNS:
                i += 1
                if i >= n:
                    raise _ParseError("invalid_number_grammar")
            continue
        if last_scale is not None and val >= last_scale:
            raise _ParseError("invalid_number_grammar")
        total += val
        if i < n:
            raise _ParseError("invalid_number_grammar")
        break
    return total


def _parse_prefix(t: List[_Tok]) -> int:
    """Prefix scales: 'dubu biyar da dari biyu' (Hausa) = 5000 + 200."""
    if any(x.kind in ("article", SCALE, HUNDRED) for x in t):
        raise _ParseError("invalid_number_grammar")
    n = len(t)
    i, total, last_scale = 0, 0, None
    while i < n:
        x = t[i]
        if x.kind == PSCALE:
            e = x.entry
            j = i + 1
            mult = 1
            # An Igbo multiplier like "puku iri abuo" (thousand ten two = 20,000)
            if e.value >= 1000 and j < n and t[j].kind == PSCALE and t[j].value == 10:
                k = j + 1
                if k < n and t[k].kind == UNIT:
                    mult, j = 10 * t[k].value, k + 1
                elif k < n and t[k].kind == CONN_LOC:
                    raise _ParseError("ambiguous_compound")
                else:
                    mult, j = 10, k
            else:
                pre, j2 = _parse_pre(t, j)
                if pre is not None:
                    if pre > e.max_mult:
                        raise _ParseError("invalid_number_grammar")
                    mult, j = pre, j2
            if last_scale is not None and e.value >= last_scale:
                raise _ParseError("invalid_number_grammar")
            total += e.value * mult
            last_scale = e.value
            i = j
        else:
            pre, j2 = _parse_pre(t, i)
            if pre is None or last_scale is None or pre >= last_scale:
                raise _ParseError("invalid_number_grammar")
            total += pre
            i = j2
            if i < n:
                raise _ParseError("invalid_number_grammar")
            break
        if i < n:
            if t[i].kind in _CONNS:
                i += 1
                if i >= n:
                    raise _ParseError("invalid_number_grammar")
            else:
                raise _ParseError("invalid_number_grammar")
    return total


def _digit_value(tok: _Tok) -> Decimal:
    if not _NUM_FORMAT.match(tok.text):
        raise _ParseError("invalid_number_format")
    whole = tok.text.split(".")[0].replace(",", "")
    if len(whole) > 1 and whole.startswith("0"):
        raise _ParseError("invalid_number_format")
    return Decimal(tok.text.replace(",", ""))


def _eval_span(t: List[_Tok]) -> Decimal:
    """Value in naira for one run of number tokens."""
    kinds = [x.kind for x in t]
    if kinds[-1] == "suffix_k" and "digit" not in kinds:      # "five k", "twenty five k"
        body = t[:-1]
        if any(x.kind in (SCALE, HUNDRED, PSCALE, "article") for x in body):
            raise _ParseError("invalid_number_grammar")
        return Decimal(_parse_postfix(body)) * 1000
    if "digit" in kinds:
        if len(t) == 1:
            return _digit_value(t[0])
        if len(t) == 2 and kinds[1] == "suffix_k":
            return _digit_value(t[0]) * 1000
        if len(t) == 2 and kinds[0] == "digit" and kinds[1] in (SCALE, HUNDRED):
            return _digit_value(t[0]) * t[1].value
        if len(t) == 2 and kinds[0] == PSCALE and kinds[1] == "digit":
            d = _digit_value(t[1])
            if d != d.to_integral_value() or d > t[0].entry.max_mult:
                raise _ParseError("invalid_number_grammar")
            return d * t[0].value
        raise _ParseError("mixed_digits_and_words")
    if any(k == PSCALE for k in kinds):
        return Decimal(_parse_prefix(t))
    return Decimal(_parse_postfix(t))


# ----------------------------------------------------------------------------- public API
def parse_amount(
    text: str,
    *,
    languages: Iterable[str] = ("en",),
    min_kobo: int = DEFAULT_MIN_KOBO,
    max_kobo: int = DEFAULT_MAX_KOBO,
) -> AmountResult:
    """Read one naira amount from a transcript, or say why not.

    `languages` lists the languages whose number words are enabled (English and
    Pidgin are always on). Enable yo/ha/ig only after the language test passes.
    """
    if not text or not text.strip():
        return AmountResult(AmountStatus.NONE, reason="no_amount")
    lex = build_lexicon(tuple(sorted(set(languages) & {"yo", "ha", "ig"})))
    raw = _mask_id_runs(tokenize(normalize(text)))
    toks = _classify(raw, lex)
    if any(x.kind == "kobo" for x in toks):
        return AmountResult(AmountStatus.AMBIGUOUS, reason="kobo_not_supported_by_voice")
    marker_idx = {i for i, x in enumerate(toks) if x.kind == "marker"}

    valid: List[tuple] = []     # (kobo, marked, text, review)
    error: Optional[Tuple[str, str]] = None
    for s, e in _spans(toks):
        span = toks[s:e]
        marked = (s - 1) in marker_idx or e in marker_idx
        matched = " ".join(x.text for x in span)
        # a lone "one" is usually a pronoun, a lone local-language "one" an article
        if len(span) == 1 and span[0].kind == UNIT and span[0].value == 1 and not marked:
            prev = toks[s - 1].text if s > 0 else ""
            nxt = toks[e].text if e < len(toks) else ""
            if span[0].entry.lang != "en" or prev in _ARTICLE_BLOCKERS_PREV or nxt in _ARTICLE_BLOCKERS_NEXT:
                continue
        try:
            naira = _eval_span(span)
            kobo = naira * 100
            if kobo != kobo.to_integral_value():
                raise _ParseError("fractional_kobo")
        except _ParseError as exc:
            error = error or (str(exc), matched)
            continue
        review = any(x.entry is not None and x.entry.lang != "en" for x in span)
        valid.append((int(kobo), marked, matched, review))

    if error:
        return AmountResult(AmountStatus.AMBIGUOUS, reason=error[0], matched_text=error[1],
                            candidates_kobo=tuple(v[0] for v in valid))
    if not valid:
        return AmountResult(AmountStatus.NONE, reason="no_amount")
    marked_c = [v for v in valid if v[1]]
    # A marked amount wins only if every other number is tiny (< 100 naira, e.g. "account 12");
    # "5000 or 7000 naira" must be asked again, not resolved by the position of the word naira.
    others = [v for v in valid if not v[1]]
    if len(marked_c) == 1 and all(v[0] < 100 * 100 for v in others):
        chosen = marked_c[0]
    elif len(marked_c) == 0 and len(valid) == 1:
        chosen = valid[0]
    else:
        return AmountResult(AmountStatus.AMBIGUOUS, reason="multiple_amounts",
                            candidates_kobo=tuple(v[0] for v in valid))
    kobo, marked, matched, review = chosen
    if kobo <= 0:
        return AmountResult(AmountStatus.OUT_OF_RANGE, reason="zero_amount", matched_text=matched)
    if kobo < min_kobo:
        return AmountResult(AmountStatus.OUT_OF_RANGE, reason="below_minimum", matched_text=matched)
    if kobo > max_kobo:
        return AmountResult(AmountStatus.OUT_OF_RANGE, reason="above_maximum", matched_text=matched)
    return AmountResult(AmountStatus.OK, kobo=kobo, matched_text=matched, has_currency_marker=marked,
                        needs_review=review, candidates_kobo=(kobo,))


def parse_account_number(text: str) -> Optional[str]:
    """A 10-digit account number from digits or a run of spoken digits, else None."""
    norm = normalize(text)
    found: List[str] = []
    for m in re.finditer(r"\d+(?:[\s-]\d+)*", norm):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 10:
            found.append(digits)
    toks = tokenize(norm)
    i = 0
    while i < len(toks):
        if toks[i] in SPOKEN_DIGITS:
            j = i
            while j < len(toks) and toks[j] in SPOKEN_DIGITS:
                j += 1
            if j - i == 10:
                found.append("".join(str(SPOKEN_DIGITS[w]) for w in toks[i:j]))
            i = j
        else:
            i += 1
    return found[0] if len(set(found)) == 1 else None
