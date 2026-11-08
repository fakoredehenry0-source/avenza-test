from __future__ import annotations

from dataclasses import dataclass

KOBO_PER_NAIRA = 100

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000_000, "trillion"), (1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def naira_to_kobo(naira: int | float | str) -> int:
    return int(round(float(naira) * KOBO_PER_NAIRA))


def kobo_to_naira(kobo: int) -> float:
    return kobo / KOBO_PER_NAIRA


def format_naira(kobo: int, symbol: str = "₦") -> str:
    negative = kobo < 0
    kobo = abs(int(kobo))
    naira, rem = divmod(kobo, 100)
    body = f"{naira:,}.{rem:02d}"
    return f"{'-' if negative else ''}{symbol}{body}"


def _under_thousand(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] if ones == 0 else f"{_TENS[tens]}-{_ONES[ones]}"
    hundreds, rest = divmod(n, 100)
    text = f"{_ONES[hundreds]} hundred"
    if rest:
        text += f" and {_under_thousand(rest)}"
    return text


def int_to_words(n: int) -> str:
    if n < 0:
        return f"minus {int_to_words(-n)}"
    if n == 0:
        return "zero"
    if n < 1000:
        return _under_thousand(n)
    for scale, name in _SCALES:
        if n >= scale:
            head, rest = divmod(n, scale)
            text = f"{int_to_words(head)} {name}"
            if rest:
                text += f" {int_to_words(rest)}"
            return text
    raise ValueError("unreachable")


def amount_to_words(kobo: int) -> str:
    """Speakable amount. Naira is invariant in Nigerian English."""
    kobo = int(kobo)
    naira, rem = divmod(kobo, 100)
    if naira == 0 and rem > 0:
        return f"{int_to_words(rem)} kobo"
    text = f"{int_to_words(naira)} naira"
    if rem:
        text += f" and {int_to_words(rem)} kobo"
    return text


@dataclass(frozen=True)
class Amount:
    kobo: int

    @property
    def naira(self) -> float:
        return kobo_to_naira(self.kobo)

    def display(self) -> str:
        return format_naira(self.kobo)

    def words(self) -> str:
        return amount_to_words(self.kobo)
