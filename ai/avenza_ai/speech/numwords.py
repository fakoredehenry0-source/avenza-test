"""Number-to-words for read-backs (English; Pidgin uses the same number words).

Yoruba, Hausa and Igbo number-to-words are intentionally NOT provided: they need
a native speaker. Until then, read-backs in those languages use English numbers
(common code-switching) and the language stays unclaimed.
"""
from __future__ import annotations

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_DIGITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (" " + _ONES[o] if o else "")


def _below_1000(n: int) -> str:
    h, r = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " hundred")
    if r:
        if h:
            parts.append("and")
        parts.append(_below_100(r))
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """0 .. 999,999,999,999 in plain English, e.g. 15000 -> 'fifteen thousand'."""
    if not isinstance(n, int) or n < 0 or n >= 10**12:
        raise ValueError("number out of range")
    if n == 0:
        return "zero"
    groups = []
    for name, size in (("billion", 10**9), ("million", 10**6), ("thousand", 10**3)):
        g, n = divmod(n, size)
        if g:
            groups.append(_below_1000(g) + " " + name)
    parts = list(groups)
    if n:
        last = _below_1000(n)
        if groups and n < 100:
            parts.append("and " + last)
        else:
            parts.append(last)
    return " ".join(parts)


def naira_to_words(kobo: int) -> str:
    """Integer kobo to spoken English: 1500000 -> 'fifteen thousand naira'."""
    if not isinstance(kobo, int) or kobo <= 0:
        raise ValueError("amount must be a positive integer number of kobo")
    naira, k = divmod(kobo, 100)
    out = []
    if naira:
        out.append(number_to_words(naira) + (" naira"))
    if k:
        out.append(number_to_words(k) + " kobo")
    return " and ".join(out)


def digits_to_words(n: int) -> str:
    """15000 -> 'one five, zero zero zero' (groups of three keep a natural pause)."""
    s = str(n)
    groups = []
    while s:
        groups.insert(0, s[-3:])
        s = s[:-3]
    return ", ".join(" ".join(_DIGITS[int(c)] for c in g) for g in groups)


def has_teen_ty_risk(naira: int) -> bool:
    """True if a spoken group could be mistaken: thirteen/thirty ... nineteen/ninety.

    Speech recognition often confuses these pairs (the doc's 'fifteen versus fifty'),
    so the read-back adds the digits when this is true.
    """
    s = str(naira)
    while s:
        g = int(s[-3:]) % 100
        if 13 <= g <= 19 or (g >= 30 and g % 10 == 0):
            return True
        s = s[:-3]
    return False
