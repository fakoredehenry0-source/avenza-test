"""Deterministic parsers for amounts, accounts, banks and time phrases.

The LLM never decides an amount. This module is the only source of amounts.
Ambiguity is rejected and turned into a clarifying question by the orchestrator.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .money import KOBO_PER_NAIRA, format_naira
from .timeutil import utcnow


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ParseError(ValueError):
    code = "parse_error"


class MissingAmount(ParseError):
    code = "missing_amount"


class AmbiguousAmount(ParseError):
    code = "ambiguous_amount"


class InvalidAmount(ParseError):
    code = "invalid_amount"


class AmountTooLarge(ParseError):
    code = "amount_too_large"


class UnsupportedCurrency(ParseError):
    code = "unsupported_currency"


class AmbiguousAccount(ParseError):
    code = "ambiguous_account"


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

_EN_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_EN_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
# Pidgin spellings that appear in real transcripts.
_PIDGIN = {
    "wan": 1,
    "tu": 2,
    "tri": 3,
    "fo": 4,
    "fọ": 4,
    "faiv": 5,
    "siks": 6,
    "eit": 8,
    "nain": 9,
}
_HAUSA = {
    "daya": 1,
    "biyu": 2,
    "uku": 3,
    "hudu": 4,
    "biyar": 5,
    "shida": 6,
    "bakwai": 7,
    "takwas": 8,
    "tara": 9,
    "goma": 10,
    "ashirin": 20,
    "talatin": 30,
    "arbain": 40,
    "hamsin": 50,
    "sittin": 60,
    "sabain": 70,
    "tamanin": 80,
    "casain": 90,
}
_IGBO = {
    "otu": 1,
    "abuo": 2,
    "ato": 3,
    "ano": 4,
    "ise": 5,
    "isii": 6,
    "asaa": 7,
    "asato": 8,
    "itoolu": 9,
    "iri": 10,
    "nari": 100,
    "puku": 1000,
    "nde": 1_000_000,
}
_YORUBA = {
    "okan": 1,
    "eni": 1,
    "eji": 2,
    "eta": 3,
    "erin": 4,
    "arun": 5,
    "efa": 6,
    "eje": 7,
    "ejo": 8,
    "esan": 9,
    "ewa": 10,
    # Counting forms used after a multiplier ("egberun marun" = five thousand).
    "meji": 2,
    "meta": 3,
    "merin": 4,
    "marun": 5,
    "mefa": 6,
    "meje": 7,
    "mejo": 8,
    "mesan": 9,
    "mewa": 10,
    "mokanla": 11,
    "mejila": 12,
    "metala": 13,
    "merinla": 14,
    "meedogun": 15,
    "merindilogun": 16,
    "metadilogun": 17,
    "mejidilogun": 18,
    "mokandilogun": 19,
    "ogun": 20,
    "ogbon": 30,
    "ogojí": 40,
    "ogojí": 40,
    "aadota": 50,
    "ogota": 60,
    "aadorin": 70,
    "ogorin": 80,
    "aadorun": 90,
    "ogorun": 100,
    "igba": 200,
    "egberun": 1000,
    "milionu": 1_000_000,
}
_MULTIPLIERS = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "trillion": 1_000_000_000_000,
    "bn": 1_000_000_000,
    "dubu": 1_000,
    "puku": 1_000,
    "egberun": 1_000,
    "miliyan": 1_000_000,
    "milionu": 1_000_000,
    "nde": 1_000_000,
}
_CURRENCY = {"naira", "naria", "ngn", "naira"}
_KOBO = {"kobo", "kobos", "kobó"}
_CONNECTORS = {"and", "da", "sha", "na", "ni", "ati", "pelu", "with"}
_UNSUPPORTED_CURRENCY = {"dollar", "dollars", "usd", "pound", "pounds", "gbp", "euro", "euros", "eur"}
_DIGIT_WORDS = {
    "zero": 0,
    "oh": 0,
    "o": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "daya": 1,
    "biyu": 2,
    "uku": 3,
    "hudu": 4,
    "biyar": 5,
    "shida": 6,
    "bakwai": 7,
    "takwas": 8,
    "tara": 9,
    "otu": 1,
    "abuo": 2,
    "ato": 3,
    "ano": 4,
    "ise": 5,
    "isii": 6,
    "asaa": 7,
    "asato": 8,
    "itoolu": 9,
    "okan": 1,
    "eni": 1,
    "eji": 2,
    "eta": 3,
    "erin": 4,
    "arun": 5,
    "efa": 6,
    "eje": 7,
    "ejo": 8,
    "esan": 9,
}
_SINGLE: dict[str, tuple[str, int]] = {}
for _word, _value in {**_EN_ONES, **_EN_TENS, **_PIDGIN, **_HAUSA, **_IGBO, **_YORUBA}.items():
    _SINGLE[_word] = ("num", _value)
for _word, _value in _MULTIPLIERS.items():
    _SINGLE[_word] = ("mult", _value)
_SINGLE["a"] = ("num", 1)

# Multi-word numerals whose meaning is not simply additive.
_PHRASES: dict[tuple[str, ...], tuple[str, int]] = {
    ("goma", "sha", "daya"): ("num", 11),
    ("goma", "sha", "biyu"): ("num", 12),
    ("goma", "sha", "uku"): ("num", 13),
    ("goma", "sha", "hudu"): ("num", 14),
    ("goma", "sha", "biyar"): ("num", 15),
    ("goma", "sha", "shida"): ("num", 16),
    ("goma", "sha", "bakwai"): ("num", 17),
    ("goma", "sha", "takwas"): ("num", 18),
    ("goma", "sha", "tara"): ("num", 19),
    ("iri", "abuo"): ("num", 20),
    ("iri", "ato"): ("num", 30),
    ("iri", "ano"): ("num", 40),
    ("iri", "ise"): ("num", 50),
    ("iri", "isii"): ("num", 60),
    ("iri", "asaa"): ("num", 70),
    ("iri", "asato"): ("num", 80),
    ("iri", "itoolu"): ("num", 90),
    ("iri", "na", "otu"): ("num", 11),
    ("iri", "na", "abuo"): ("num", 12),
    ("iri", "na", "ato"): ("num", 13),
    ("iri", "na", "ano"): ("num", 14),
    ("iri", "na", "ise"): ("num", 15),
    ("iri", "na", "isii"): ("num", 16),
    ("iri", "na", "asaa"): ("num", 17),
    ("iri", "na", "asato"): ("num", 18),
    ("iri", "na", "itoolu"): ("num", 19),
    ("marun", "un"): ("num", 5),
    ("ogorun", "un"): ("num", 100),
    ("ogbon", "un"): ("num", 30),
}

_BANKS: list[tuple[tuple[str, ...], str, str]] = [
    (("guaranty trust", "gtbank", "gtb", "guaranty"), "058", "GTBank"),
    (("access bank", "access", "accessbank"), "044", "Access Bank"),
    (("zenith bank", "zenith"), "057", "Zenith Bank"),
    (("first bank", "firstbank", "first"), "011", "First Bank"),
    (("united bank for africa", "uba"), "033", "UBA"),
    (("kuda", "kuda bank"), "50211", "Kuda"),
    (("opay", "o pay"), "999992", "OPay"),
    (("moniepoint", "monie point"), "50515", "Moniepoint"),
    (("palmpay", "palm pay"), "999991", "PalmPay"),
    (("wema", "wema bank", "alat"), "035", "Wema Bank"),
    (("sterling", "sterling bank"), "232", "Sterling Bank"),
    (("fidelity", "fidelity bank"), "070", "Fidelity Bank"),
    (("union bank", "union"), "032", "Union Bank"),
    (("polaris", "polaris bank"), "076", "Polaris Bank"),
    (("providus", "providus bank"), "101", "Providus Bank"),
]

_ACCOUNT_RE = re.compile(r"(?<!\d)(\d{10})(?!\d)")
_DIGIT_RE = re.compile(
    r"(?<![\d.])(\d+)(?:\.(\d+))?\s*"
    r"(k|thousand|million|billion|trillion|bn|m|dubu|puku|egberun|miliyan|milionu)?\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    text = (text or "").lower().replace("₦", " naira ").replace("₦", " naira ")
    text = text.replace("–", " ").replace("—", " ").replace("-", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    text = re.sub(r"[^a-z0-9.,\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass
class Token:
    word: str
    start: int
    end: int
    ignored: bool = False


def tokenize(text: str) -> list[Token]:
    return [Token(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", text)]


@dataclass(frozen=True)
class ParsedAmount:
    kobo: int
    raw: str
    source: str
    confidence: float

    @property
    def display(self) -> str:
        return format_naira(self.kobo)


@dataclass(frozen=True)
class BankInfo:
    code: str
    name: str


@dataclass(frozen=True)
class AccountNumber:
    value: str
    raw: str


@dataclass
class TransferFields:
    amount: ParsedAmount | None
    account: AccountNumber | None
    bank: BankInfo | None
    recipient_hint: str | None
    amount_error: str | None = None
    account_error: str | None = None
    missing: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Number sequence parsing
# ---------------------------------------------------------------------------


def _classify(token: str) -> tuple[str, int] | None:
    if token in _SINGLE:
        return _SINGLE[token]
    if token in _CURRENCY:
        return ("currency", 0)
    if token in _KOBO:
        return ("kobo", 0)
    if token in _CONNECTORS:
        return ("connector", 0)
    if token == "point":
        return ("point", 0)
    return None


def _classify_tokens(words: list[str]) -> list[tuple[str, int] | None]:
    result: list[tuple[str, int] | None] = [None] * len(words)
    i = 0
    while i < len(words):
        matched = False
        for size in (3, 2):
            if i + size <= len(words):
                key = tuple(words[i : i + size])
                if key in _PHRASES:
                    for j in range(size):
                        result[i + j] = _PHRASES[key]
                    i += size
                    matched = True
                    break
        if matched:
            continue
        result[i] = _classify(words[i])
        i += 1
    return result


def _parse_items(items: list[tuple[str, int]]) -> int:
    # Local languages put the multiplier first ("dubu biyar"). Swap that pair
    # into the additive order used by English before evaluating.
    items = list(items)
    if len(items) >= 2 and items[0][0] == "mult" and items[0][1] >= 100 and items[1][0] == "num":
        items[0], items[1] = items[1], items[0]

    total = 0
    current = 0
    for kind, value in items:
        if kind == "num":
            current += value
        elif kind == "mult":
            if current == 0:
                current = 1
            current *= value
            if value >= 1000:
                total += current
                current = 0
        elif kind == "point":
            # Handled by the digit parser; a word decimal is ambiguous here.
            raise AmbiguousAmount("spoken decimals are not accepted; say the amount in naira")
    return total + current


def _word_runs(tokens: list[Token], classifications: list[tuple[str, int] | None]) -> list[tuple[int, int, int]]:
    """Return (start_index, end_index_exclusive, value) for number runs."""
    runs: list[tuple[int, int, int]] = []
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i].ignored or classifications[i] is None or classifications[i][0] not in {"num", "mult", "connector"}:
            i += 1
            continue
        start = i
        items: list[tuple[str, int]] = []
        while i < n and not tokens[i].ignored and classifications[i] is not None:
            kind, value = classifications[i]  # type: ignore[misc]
            if kind in {"num", "mult"}:
                items.append((kind, value))
                i += 1
            elif kind == "connector":
                # Only a connector between two number items is meaningful.
                if items and i + 1 < n and classifications[i + 1] is not None and classifications[i + 1][0] in {"num", "mult"}:
                    i += 1
                else:
                    break
            else:
                break
        if items:
            try:
                value = _parse_items(items)
            except AmbiguousAmount:
                raise
            runs.append((start, i, value))
        else:
            i = start + 1
    return runs


def _raw(tokens: list[Token], start: int, end: int) -> str:
    return " ".join(t.word for t in tokens[start:end])


def _spoken_accounts(tokens: list[Token]) -> list[tuple[str, int, int]]:
    found: list[tuple[str, int, int]] = []
    i = 0
    while i < len(tokens):
        if tokens[i].word in _DIGIT_WORDS:
            j = i
            digits: list[str] = []
            while j < len(tokens) and tokens[j].word in _DIGIT_WORDS:
                digits.append(str(_DIGIT_WORDS[tokens[j].word]))
                j += 1
            if len(digits) == 10:
                found.append(("".join(digits), i, j))
            i = j
        else:
            i += 1
    return found


def parse_account_number(text: str) -> AccountNumber | None:
    norm = normalize(text)
    tokens = tokenize(norm)
    accounts: list[AccountNumber] = []

    for m in _ACCOUNT_RE.finditer(norm):
        accounts.append(AccountNumber(m.group(1), m.group(0)))

    for digits, start, end in _spoken_accounts(tokens):
        accounts.append(AccountNumber(digits, _raw(tokens, start, end)))

    unique = {a.value for a in accounts}
    if len(unique) > 1:
        raise AmbiguousAccount("more than one account number was heard")
    if not accounts:
        return None
    return accounts[0]


def parse_amount(text: str, max_kobo: int = 100_000_000) -> ParsedAmount:
    norm = normalize(text)
    tokens = tokenize(norm)

    if any(word in _UNSUPPORTED_CURRENCY for word in norm.split()):
        raise UnsupportedCurrency("only naira transfers are supported")

    # Mask account numbers so their digits are never read as an amount.
    account_spans: list[tuple[int, int]] = []
    for m in _ACCOUNT_RE.finditer(norm):
        account_spans.append((m.start(), m.end()))
        for t in tokens:
            if t.start >= m.start() and t.end <= m.end():
                t.ignored = True
    for digits, start, end in _spoken_accounts(tokens):
        account_spans.append((tokens[start].start, tokens[end - 1].end))
        for t in tokens[start:end]:
            t.ignored = True

    words = [t.word for t in tokens]
    classifications = _classify_tokens(words)

    candidates: list[tuple[int, str, str, float, str]] = []  # kobo, raw, source, confidence, currency

    # 1) Explicit digits, optionally with a scale suffix.
    for m in _DIGIT_RE.finditer(norm):
        if any(m.start() < end and m.end() > start for start, end in account_spans):
            continue
        if any(t.ignored and t.start >= m.start() and t.end <= m.end() for t in tokens):
            continue
        whole = int(m.group(1))
        frac = m.group(2)
        suffix = (m.group(3) or "").lower()
        span_text = m.group(0).strip()

        # A bare 10-digit run is an account number, not money.
        if len(m.group(1)) == 10 and not frac and not suffix:
            continue
        if len(m.group(1)) > 10 and not suffix:
            continue

        multiplier = _MULTIPLIERS.get(suffix, 1)
        if suffix == "k":
            multiplier = 1_000
        elif suffix == "m":
            multiplier = 1_000_000

        if frac:
            base_units = (whole + int(frac) / (10 ** len(frac))) * multiplier
        else:
            base_units = whole * multiplier

        # Determine naira vs kobo from adjacent words.
        currency = "naira"
        if m.end() < len(norm):
            tail = norm[m.end() :].lstrip().split()
            if tail and tail[0] in _KOBO:
                currency = "kobo"
        if m.start() > 0:
            head = norm[: m.start()].rstrip().split()
            if head and head[-1] in _KOBO:
                currency = "kobo"

        # Kobo amounts are already in kobo; naira amounts need the x100.
        kobo = int(round(base_units)) if currency == "kobo" else int(round(base_units * KOBO_PER_NAIRA))

        candidates.append((kobo, span_text, "digits", 0.99, currency))
        # Do not let the word scanner reuse these digits or their suffix.
        for t in tokens:
            if t.start >= m.start() and t.end <= m.end():
                t.ignored = True

    # 2) Spoken numbers. A run needs a multiplier or a currency word; a lone
    #    "five" without "naira" is not enough to draft a payment.
    for start, end, value in _word_runs(tokens, classifications):
        if value <= 0:
            continue
        before = tokens[start - 1].word if start > 0 else ""
        after = tokens[end].word if end < len(tokens) else ""
        currency = "naira"
        if after in _KOBO or before in _KOBO:
            currency = "kobo"
        has_multiplier = any(
            classifications[i] is not None and classifications[i][0] == "mult" for i in range(start, end)
        )
        has_currency = before in _CURRENCY or after in _CURRENCY or before in _KOBO or after in _KOBO
        if not has_multiplier and not has_currency:
            continue
        # "one five" is ambiguous: it could be a misheard 15. Reject it.
        bare_units = [
            classifications[i][1]
            for i in range(start, end)
            if classifications[i] is not None
            and classifications[i][0] == "num"
            and classifications[i][1] < 10
        ]
        if len(bare_units) >= 2 and not has_multiplier:
            raise AmbiguousAmount("the amount was heard as separate digits; say the number in full")
        kobo = value if currency == "kobo" else value * KOBO_PER_NAIRA
        candidates.append((kobo, _raw(tokens, start, end), "words", 0.92, currency))

    if not candidates:
        raise MissingAmount("no amount was heard")

    naira_values = {c[0] for c in candidates if c[4] == "naira"}
    kobo_values = {c[0] for c in candidates if c[4] == "kobo"}
    if len(naira_values) > 1:
        shown = ", ".join(sorted({format_naira(v) for v in naira_values}))
        raise AmbiguousAmount(f"more than one amount was heard: {shown}")
    if len(kobo_values) > 1:
        shown = ", ".join(str(v) for v in sorted(kobo_values))
        raise AmbiguousAmount(f"more than one kobo amount was heard: {shown}")

    naira_kobo = next(iter(naira_values), 0)
    kobo_part = next(iter(kobo_values), 0)
    if kobo_part >= 100 and naira_kobo > 0:
        raise InvalidAmount("kobo must be below one hundred")
    total = naira_kobo + kobo_part
    if total <= 0:
        raise InvalidAmount("the amount must be greater than zero")
    if total > max_kobo:
        raise AmountTooLarge(f"the amount is above the limit of {format_naira(max_kobo)}")

    best = max(candidates, key=lambda c: c[3])
    source = "mixed" if len({c[2] for c in candidates}) > 1 else best[2]
    return ParsedAmount(kobo=total, raw=best[1], source=source, confidence=best[3])


# ---------------------------------------------------------------------------
# Bank and recipient
# ---------------------------------------------------------------------------


def detect_bank(text: str) -> BankInfo | None:
    norm = normalize(text)
    best: tuple[int, str, str] | None = None
    for aliases, code, name in _BANKS:
        for alias in aliases:
            if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", norm):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), code, name)
    if best is None:
        return None
    return BankInfo(code=best[1], name=best[2])


def extract_recipient_hint(text: str) -> str | None:
    norm = normalize(text)
    match = re.search(r"\bto\s+(.+)$", norm)
    if not match:
        return None
    hint = match.group(1)
    # Remove account numbers, bank names and generic trailing words.
    hint = _ACCOUNT_RE.sub(" ", hint)
    hint = re.sub(r"\b(?:account|acct|number|bank)\b", " ", hint)
    for aliases, _code, _name in _BANKS:
        for alias in aliases:
            hint = re.sub(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", " ", hint)
    hint = re.sub(r"\b(?:please|abeg|now|jare|o)\b", " ", hint)
    hint = re.sub(r"\s+", " ", hint).strip(" .,")
    if not hint or len(hint) < 2 or re.fullmatch(r"[\d\s]+", hint):
        return None
    return hint[:120]


def names_compatible(hint: str | None, verified: str | None) -> bool:
    if not hint or not verified:
        return True
    h = {w for w in re.findall(r"[a-z]+", normalize(hint)) if len(w) > 1}
    v = {w for w in re.findall(r"[a-z]+", normalize(verified)) if len(w) > 1}
    if not h or not v:
        return True
    return h.issubset(v) or v.issubset(h)


def extract_transfer_fields(text: str, max_kobo: int = 100_000_000) -> TransferFields:
    amount: ParsedAmount | None = None
    amount_error: str | None = None
    try:
        amount = parse_amount(text, max_kobo=max_kobo)
    except ParseError as exc:
        amount_error = exc.code

    account: AccountNumber | None = None
    account_error: str | None = None
    try:
        account = parse_account_number(text)
    except ParseError as exc:
        account_error = exc.code

    bank = detect_bank(text)
    hint = extract_recipient_hint(text)

    missing: list[str] = []
    if amount is None:
        missing.append("amount")
    if account is None:
        missing.append("account")
    if bank is None:
        missing.append("bank")

    return TransferFields(
        amount=amount,
        account=account,
        bank=bank,
        recipient_hint=hint,
        amount_error=amount_error,
        account_error=account_error,
        missing=missing,
    )


# ---------------------------------------------------------------------------
# History time phrases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryQuery:
    start: datetime
    end: datetime
    label: str
    limit: int = 50


def _small_number_from_words(text: str) -> int | None:
    norm = normalize(text)
    tokens = tokenize(norm)
    words = [t.word for t in tokens]
    classes = _classify_tokens(words)
    for start, end, value in _word_runs(tokens, classes):
        if value > 0:
            return value
    return None


def parse_history_query(text: str, now: datetime | None = None) -> HistoryQuery:
    now = now or utcnow()
    norm = normalize(text)

    limit = 50
    m = re.search(r"\b(?:last|past|recent)\s+([a-z0-9\s]{1,30}?)\s+(?:transactions|payments|transfers)\b", norm)
    if m:
        n = _small_number_from_words(m.group(1))
        if n:
            limit = max(1, min(100, n))
    elif re.search(r"\b(?:last|past|recent)\s+(?:transactions|payments|transfers)\b", norm):
        limit = 10

    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if "today" in norm:
        return HistoryQuery(start_of_today, now, "today", limit)
    if "yesterday" in norm:
        start = start_of_today - timedelta(days=1)
        return HistoryQuery(start, start_of_today, "yesterday", limit)
    if "last week" in norm:
        this_monday = start_of_today - timedelta(days=start_of_today.weekday())
        return HistoryQuery(this_monday - timedelta(days=7), this_monday, "last week", limit)
    if "this week" in norm:
        this_monday = start_of_today - timedelta(days=start_of_today.weekday())
        return HistoryQuery(this_monday, now, "this week", limit)
    if "last month" in norm:
        first_this = start_of_today.replace(day=1)
        first_last = (first_this - timedelta(days=1)).replace(day=1)
        return HistoryQuery(first_last, first_this, "last month", limit)
    if "this month" in norm:
        return HistoryQuery(start_of_today.replace(day=1), now, "this month", limit)

    m = re.search(r"\b(?:last|past)\s+([a-z0-9\s]{1,20}?)\s+days?\b", norm)
    if m:
        n = _small_number_from_words(m.group(1)) or 7
        n = max(1, min(365, n))
        return HistoryQuery(now - timedelta(days=n), now, f"the last {n} days", limit)

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for idx, day in enumerate(weekdays):
        if re.search(rf"\b(?:on|since)\s+{day}\b", norm):
            delta = (start_of_today.weekday() - idx) % 7
            start = start_of_today - timedelta(days=delta)
            if "since" in norm and start == start_of_today and idx != start_of_today.weekday():
                start -= timedelta(days=7)
            return HistoryQuery(start, now, f"since {day.title()}", limit)

    return HistoryQuery(now - timedelta(days=30), now, "the last 30 days", limit)
