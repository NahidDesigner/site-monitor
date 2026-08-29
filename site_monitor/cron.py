"""A small 5-field cron parser, so schedules live in the app.

Only what a schedule UI actually needs: `*`, numbers, ranges, comma lists and
steps. Evaluated in a configurable timezone, because "run at 3am" should mean
3am where the person setting it lives, not 3am UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "hour", "day of month", "month", "day of week")

ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}

MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split()
    )
}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}


class CronError(ValueError):
    """Raised for an expression a person needs to fix."""


def get_zone(name: str):
    try:
        return ZoneInfo(name) if name and name.upper() != "UTC" else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _parse_field(raw: str, index: int) -> set[int]:
    low, high = FIELD_RANGES[index]
    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty value in the {FIELD_NAMES[index]} field")

        step = 1
        if "/" in part:
            part, _, step_raw = part.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise CronError(f"'{step_raw}' is not a valid step")
            step = int(step_raw)
            part = part or "*"

        if index == 3:
            for name, number in MONTHS.items():
                part = part.replace(name, str(number))
        if index == 4:
            for name, number in DAYS.items():
                part = part.replace(name, str(number))

        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_raw, _, end_raw = part.partition("-")
            start, end = _number(start_raw, index), _number(end_raw, index)
            if start > end:
                raise CronError(
                    f"range {start}-{end} is backwards in the {FIELD_NAMES[index]} field"
                )
        else:
            start = end = _number(part, index)

        values.update(range(start, end + 1, step))

    return values


def _number(raw: str, index: int) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise CronError(f"'{raw}' is not a number in the {FIELD_NAMES[index]} field")
    value = int(raw)
    # Cron traditionally accepts 7 for Sunday as well as 0.
    if index == 4 and value == 7:
        value = 0
    low, high = FIELD_RANGES[index]
    if not low <= value <= high:
        raise CronError(
            f"{value} is out of range for {FIELD_NAMES[index]} ({low}-{high})"
        )
    return value


@dataclass(frozen=True)
class CronExpression:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool
    raw: str

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        dom_ok = moment.day in self.days
        # Python: Monday=0. Cron: Sunday=0.
        dow_ok = ((moment.weekday() + 1) % 7) in self.weekdays

        # Classic cron: when both day fields are restricted, either may match.
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime, *, horizon_days: int = 400) -> datetime | None:
        """The first matching minute strictly after `after`."""
        moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = after + timedelta(days=horizon_days)

        while moment <= limit:
            if moment.month not in self.months:
                moment = _start_of_next_month(moment)
                continue
            if not self._day_matches(moment):
                moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if moment.hour not in self.hours:
                moment = _advance_hour(moment)
                continue
            if moment.minute not in self.minutes:
                moment += timedelta(minutes=1)
                continue
            return moment
        return None

    def _day_matches(self, moment: datetime) -> bool:
        dom_ok = moment.day in self.days
        dow_ok = ((moment.weekday() + 1) % 7) in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def _start_of_next_month(moment: datetime) -> datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return moment.replace(year=year, month=month, day=1, hour=0, minute=0)


def _advance_hour(moment: datetime) -> datetime:
    nxt = moment.replace(minute=0) + timedelta(hours=1)
    return nxt


def parse(expression: str) -> CronExpression:
    """Parse a 5-field cron expression, or one of the @aliases."""
    raw = (expression or "").strip()
    if not raw:
        raise CronError("Enter a schedule, for example 0 */6 * * *")

    normalized = ALIASES.get(raw.lower(), raw)
    fields = normalized.split()
    if len(fields) != 5:
        raise CronError(
            f"A schedule needs 5 fields (minute hour day month weekday); got {len(fields)}"
        )

    parsed = [_parse_field(field, index) for index, field in enumerate(fields)]
    return CronExpression(
        minutes=frozenset(parsed[0]),
        hours=frozenset(parsed[1]),
        days=frozenset(parsed[2]),
        months=frozenset(parsed[3]),
        weekdays=frozenset(parsed[4]),
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
        raw=raw,
    )


def next_run(expression: str, *, tz: str = "UTC", after: datetime | None = None) -> datetime | None:
    """Next fire time as an aware UTC datetime."""
    zone = get_zone(tz)
    moment = (after or datetime.now(timezone.utc)).astimezone(zone)
    result = parse(expression).next_after(moment)
    return result.astimezone(timezone.utc) if result else None


def describe(expression: str) -> str:
    """A plain-English gloss for the schedule list."""
    common = {
        "* * * * *": "Every minute",
        "0 * * * *": "Every hour",
        "0 */2 * * *": "Every 2 hours",
        "0 */3 * * *": "Every 3 hours",
        "0 */4 * * *": "Every 4 hours",
        "0 */6 * * *": "Every 6 hours",
        "0 */12 * * *": "Every 12 hours",
        "0 0 * * *": "Every day at midnight",
        "0 3 * * *": "Every day at 3:00am",
        "0 9 * * *": "Every day at 9:00am",
        "0 0 * * 0": "Every Sunday at midnight",
        "0 0 * * 1": "Every Monday at midnight",
        "0 0 1 * *": "The 1st of every month",
    }
    raw = (expression or "").strip()
    return common.get(ALIASES.get(raw.lower(), raw), raw)
