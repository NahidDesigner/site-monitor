"""The scheduling expression parser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from site_monitor.cron import CronError, describe, next_run, parse


def at(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "expression, after, expected",
    [
        ("0 */6 * * *", at(2026, 8, 29, 1, 50), at(2026, 8, 29, 6, 0)),
        ("*/15 * * * *", at(2026, 8, 29, 1, 50), at(2026, 8, 29, 2, 0)),
        ("0 3 * * *", at(2026, 8, 29, 4, 0), at(2026, 8, 30, 3, 0)),
        ("@daily", at(2026, 8, 29, 1, 0), at(2026, 8, 30, 0, 0)),
        ("0 0 1 * *", at(2026, 8, 29, 1, 0), at(2026, 9, 1, 0, 0)),
        # 2026-08-29 is a Saturday, so the next weekday run is Monday.
        ("0 3 * * 1-5", at(2026, 8, 29, 1, 0), at(2026, 8, 31, 3, 0)),
        ("30 9 * * mon", at(2026, 8, 29, 1, 0), at(2026, 8, 31, 9, 30)),
    ],
)
def test_next_run(expression, after, expected):
    assert next_run(expression, after=after) == expected


def test_next_run_is_strictly_after_the_given_moment():
    """A schedule that just fired must not immediately fire again."""
    exactly_on = at(2026, 8, 29, 6, 0)

    assert next_run("0 */6 * * *", after=exactly_on) == at(2026, 8, 29, 12, 0)


def test_timezone_is_honoured():
    """3am in New York is 07:00 UTC during daylight saving."""
    result = next_run("0 3 * * *", tz="America/New_York", after=at(2026, 8, 29, 1, 0))

    assert result == at(2026, 8, 29, 7, 0)


def test_unknown_timezone_falls_back_to_utc_rather_than_failing():
    assert next_run("0 3 * * *", tz="Mars/Olympus", after=at(2026, 8, 29, 1, 0)) == at(
        2026, 8, 29, 3, 0
    )


def test_sunday_accepted_as_both_0_and_7():
    assert parse("0 0 * * 0").weekdays == parse("0 0 * * 7").weekdays


def test_day_of_month_and_weekday_are_ored_like_real_cron():
    """With both day fields restricted, cron fires when EITHER matches."""
    expression = parse("0 0 1 * 1")  # the 1st, or any Monday

    assert expression.matches(at(2026, 9, 1, 0, 0))   # a Tuesday, but the 1st
    assert expression.matches(at(2026, 8, 31, 0, 0))  # a Monday, not the 1st
    assert not expression.matches(at(2026, 9, 2, 0, 0))


def test_lists_and_ranges():
    expression = parse("0,30 9-11 * * *")

    assert expression.minutes == {0, 30}
    assert expression.hours == {9, 10, 11}


@pytest.mark.parametrize(
    "bad, message",
    [
        ("", "Enter a schedule"),
        ("0 0 * *", "needs 5 fields"),
        ("99 * * * *", "out of range"),
        ("0 0 * * 9", "out of range"),
        ("a * * * *", "not a number"),
        ("0 0 * * 5-1", "backwards"),
        ("*/0 * * * *", "not a valid step"),
    ],
)
def test_invalid_expressions_explain_themselves(bad, message):
    with pytest.raises(CronError) as info:
        parse(bad)

    assert message in str(info.value)


def test_describe_gives_plain_english_for_common_schedules():
    assert describe("0 */6 * * *") == "Every 6 hours"
    assert describe("@daily") == "Every day at midnight"
    # Anything unusual falls back to the raw expression rather than guessing.
    assert describe("7 4 * * 3") == "7 4 * * 3"
