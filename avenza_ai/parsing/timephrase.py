"""Time-phrase parser for the transaction-history addendum.

Resolves phrases such as "yesterday", "last week", "past 7 days", "on Friday",
"in October" to a half-open range [start, end) in Lagos time (WAT, UTC+1, no DST).
Ambiguity is rejected so the customer is asked again. The resolved range is
always read back with describe(), so a wrong reading is caught by the customer.

Definitions (kept deliberately simple and spoken back every time):
- "this week" / "last week": calendar weeks, Monday to Sunday.
- "past/last N days|weeks|months": rolling, ending with today.
- A bare weekday ("on Friday", "last Friday") is the most recent one before today.
- "in <month>": the most recent such month that has started.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, List, Optional, Tuple

LAGOS = timezone(timedelta(hours=1), "WAT")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "fifteen": 15,
          "twenty": 20, "thirty": 30}
MAX_DAYS = 366


class TimeStatus(str, Enum):
    OK = "ok"
    NONE = "none"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime  # exclusive
    label: str

    def describe(self) -> str:
        last = self.end - timedelta(days=1)
        if last.date() == self.start.date():
            return _day(self.start)
        return f"from {_day(self.start)} to {_day(last)}"


@dataclass(frozen=True)
class TimeRangeResult:
    status: TimeStatus
    range: Optional[TimeRange] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status is TimeStatus.OK


def _day(d: datetime) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {MONTHS[d.month - 1].capitalize()} {d.year}"


def _sod(d: datetime) -> datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def _add_months(d: datetime, months: int) -> datetime:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return d.replace(year=year, month=month, day=min(d.day, calendar.monthrange(year, month)[1]))


def _num(tok: str) -> Optional[int]:
    return int(tok) if tok.isdigit() else _SMALL.get(tok)


_N = r"(\d{1,4}|" + "|".join(_SMALL) + r")"


def parse_time_phrase(text: str, now: Optional[datetime] = None) -> TimeRangeResult:
    now = now or datetime.now(LAGOS)
    now = now.replace(tzinfo=LAGOS) if now.tzinfo is None else now.astimezone(LAGOS)
    today = _sod(now)
    tomorrow = today + timedelta(days=1)
    work = " " + re.sub(r"[^a-z0-9 ]", " ", text.lower()) + " "
    found: List[Tuple[Optional[TimeRange], Optional[str]]] = []

    def take(pattern: str, build: Callable[[re.Match], Tuple[Optional[TimeRange], Optional[str]]]):
        nonlocal work
        for m in list(re.finditer(pattern, work)):
            found.append(build(m))
        work = re.sub(pattern, " ", work)

    def rolling(m):
        n, unit = _num(m.group(2)), m.group(3)
        if not n:
            return None, "bad_number"
        if unit == "day":
            start = today - timedelta(days=n - 1)
        elif unit == "week":
            start = today - timedelta(days=7 * n - 1)
        else:
            start = _add_months(today, -n) + timedelta(days=1)
        if (tomorrow - start).days > MAX_DAYS:
            return None, "range_too_long"
        return TimeRange(start, tomorrow, f"last {n} {unit}s"), None

    take(r"\b(last|past|previous)\s+" + _N + r"\s+(day|week|month)s?\b", rolling)

    def ago(m):
        n, unit = _num(m.group(1)), m.group(2)
        if unit != "day" or not n:
            return None, "ambiguous_ago"
        d = today - timedelta(days=n)
        return TimeRange(d, d + timedelta(days=1), f"{n} days ago"), None

    take(r"\b" + _N + r"\s+(day|week|month)s?\s+ago\b", ago)

    take(r"\bday before yesterday\b",
         lambda m: (TimeRange(today - timedelta(days=2), today - timedelta(days=1), "day before yesterday"), None))
    take(r"\byesterday\b", lambda m: (TimeRange(today - timedelta(days=1), today, "yesterday"), None))
    take(r"\btoday\b", lambda m: (TimeRange(today, tomorrow, "today"), None))

    monday = today - timedelta(days=today.weekday())
    take(r"\bthis week\b", lambda m: (TimeRange(monday, monday + timedelta(days=7), "this week"), None))
    take(r"\blast week\b", lambda m: (TimeRange(monday - timedelta(days=7), monday, "last week"), None))
    first = today.replace(day=1)
    take(r"\bthis month\b", lambda m: (TimeRange(first, _add_months(first, 1), "this month"), None))
    take(r"\blast month\b", lambda m: (TimeRange(_add_months(first, -1), first, "last month"), None))
    jan1 = today.replace(month=1, day=1)
    take(r"\bthis year\b", lambda m: (TimeRange(jan1, jan1.replace(year=jan1.year + 1), "this year"), None))
    take(r"\blast year\b", lambda m: (TimeRange(jan1.replace(year=jan1.year - 1), jan1, "last year"), None))

    def weekday(m):
        idx = WEEKDAYS.index(m.group(2))
        back = (today.weekday() - idx) % 7 or 7
        d = today - timedelta(days=back)
        return TimeRange(d, d + timedelta(days=1), m.group(2)), None

    take(r"\b(last|on|since)?\s*(" + "|".join(WEEKDAYS) + r")\b", weekday)

    def month(m):
        idx = MONTHS.index(m.group(1)) + 1
        year = today.year if idx <= today.month else today.year - 1
        start = today.replace(year=year, month=idx, day=1)
        return TimeRange(start, _add_months(start, 1), m.group(1)), None

    take(r"\bin\s+(" + "|".join(MONTHS) + r")\b", month)

    if not found:
        return TimeRangeResult(TimeStatus.NONE, reason="no_time_phrase")
    errors = [e for r, e in found if e]
    if errors:
        return TimeRangeResult(TimeStatus.AMBIGUOUS, reason=errors[0])
    ranges = {(r.start, r.end) for r, _ in found if r}
    if len(ranges) > 1:
        return TimeRangeResult(TimeStatus.AMBIGUOUS, reason="multiple_time_phrases")
    return TimeRangeResult(TimeStatus.OK, range=found[0][0])
