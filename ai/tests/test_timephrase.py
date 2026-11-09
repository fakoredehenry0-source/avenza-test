from datetime import datetime

from avenza_ai.parsing import LAGOS, TimeStatus, parse_time_phrase

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=LAGOS)   # Sunday


def rng(text):
    r = parse_time_phrase(text, NOW)
    assert r.ok, r
    return r.range


def test_today_yesterday():
    assert rng("show my transactions today").describe() == "Sunday 8 November 2026"
    assert rng("what did I send yesterday").describe() == "Saturday 7 November 2026"
    assert rng("the day before yesterday").describe() == "Friday 6 November 2026"


def test_weeks_and_months():
    assert rng("last week").describe() == "from Monday 26 October 2026 to Sunday 1 November 2026"
    assert rng("this week").describe() == "from Monday 2 November 2026 to Sunday 8 November 2026"
    assert rng("last month").describe() == "from Thursday 1 October 2026 to Saturday 31 October 2026"
    assert rng("this month").start.day == 1 and rng("this month").start.month == 11
    assert rng("last year").start.year == 2025


def test_rolling_windows():
    r = rng("past 7 days")
    assert (r.end - r.start).days == 7 and r.end.day == 9
    assert (rng("last two weeks").end - rng("last two weeks").start).days == 14
    assert rng("last 3 months").start.month == 8 and rng("last 3 months").start.day == 9


def test_weekdays_and_months_by_name():
    assert rng("on Friday").describe() == "Friday 6 November 2026"
    assert rng("last Sunday").describe() == "Sunday 1 November 2026"   # never "today"
    assert rng("in October").start.month == 10 and rng("in October").start.year == 2026
    assert rng("in December").start.year == 2025                       # not yet started this year
    assert rng("3 days ago").describe() == "Thursday 5 November 2026"


def test_ambiguity_is_rejected():
    assert parse_time_phrase("yesterday and last week", NOW).status is TimeStatus.AMBIGUOUS
    assert parse_time_phrase("two weeks ago", NOW).status is TimeStatus.AMBIGUOUS
    assert parse_time_phrase("last 1000 days", NOW).status is TimeStatus.AMBIGUOUS
    assert parse_time_phrase("last 0 days", NOW).status is TimeStatus.AMBIGUOUS


def test_no_phrase():
    assert parse_time_phrase("show my transactions", NOW).status is TimeStatus.NONE
    assert parse_time_phrase("", NOW).status is TimeStatus.NONE


def test_naive_datetime_is_treated_as_lagos():
    r = parse_time_phrase("today", datetime(2026, 11, 8, 23, 59))
    assert r.range.start.hour == 0 and r.range.start.utcoffset().total_seconds() == 3600


def test_month_end_rolling_does_not_crash():
    r = parse_time_phrase("last month", datetime(2026, 3, 31, 9, 0, tzinfo=LAGOS))
    assert r.range.start.month == 2 and r.range.end.month == 3
