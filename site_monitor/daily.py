"""One day's checks, assembled into something worth reading.

The question this answers: "what happened to the sites today?" -- which is a
sequence, not a snapshot. At 01:00 these pages were found broken; by 05:00
some had healed and others had appeared. A flat list of every broken URL
loses exactly the part that matters.

So a day is built as an ordered list of checks, each carrying a per-site
breakdown of what was found, what was fixed, and what is still outstanding,
with the URLs nested under the site they belong to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, time, timedelta, timezone

from .cron import get_zone
from .history import SiteDelta, compare


def day_bounds(day: date_cls, tz_name: str) -> tuple[str, str]:
    """The UTC window covering one calendar day in the given timezone.

    A calendar day, not a rolling 24 hours: a rolling window returns
    different content every time it is opened, so two people downloading
    "the last day" get two different documents and neither can be cited.
    """
    zone = get_zone(tz_name)
    start_local = datetime.combine(day, time.min, tzinfo=zone)
    end_local = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    # Seconds precision, matching db.utcnow() exactly: the window is compared
    # against stored timestamps as text, so the two formats must not drift.
    def to_utc(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat(timespec="seconds")

    return to_utc(start_local), to_utc(end_local)


def today_in(tz_name: str) -> date_cls:
    return datetime.now(get_zone(tz_name)).date()


@dataclass
class SiteEntry:
    """One site's outcome within one check."""

    domain: str
    delta: SiteDelta
    warning: str = ""
    error: str = ""
    pages_checked: int = 0
    assets_checked: int = 0

    @property
    def found(self) -> tuple:
        return self.delta.new

    @property
    def fixed(self) -> tuple:
        return self.delta.fixed

    @property
    def still(self) -> tuple:
        return self.delta.still

    @staticmethod
    def _pages(rows) -> list[str]:
        """Distinct page URLs, in the order first seen.

        A page can reference several broken stylesheets, so counting rows
        overstates how many pages are actually affected -- and "how many
        pages are broken" is the number anyone asks for first.
        """
        seen: list[str] = []
        for row in rows:
            if row["page_url"] not in seen:
                seen.append(row["page_url"])
        return seen

    @property
    def found_pages(self) -> list[str]:
        return self._pages(self.delta.new)

    @property
    def fixed_pages(self) -> list[str]:
        return self._pages(self.delta.fixed)

    @property
    def still_pages(self) -> list[str]:
        return self._pages(self.delta.still)

    @property
    def broken_pages(self) -> list[str]:
        """Every page of this site broken at this check: new plus carried over."""
        return self._pages(tuple(self.delta.new) + tuple(self.delta.still))

    @property
    def is_noteworthy(self) -> bool:
        """Whether this site earned a place in the report.

        A site that was checked and was fine both times is not news, and
        printing fifty of those buries the handful that matter.
        """
        return bool(
            self.delta.new
            or self.delta.fixed
            or self.delta.still
            or self.warning
            or self.error
        )


@dataclass
class CheckEntry:
    """One run, with the sites in it that had something to report."""

    run: object
    sites: list[SiteEntry] = field(default_factory=list)

    @property
    def found_count(self) -> int:
        return sum(len(site.found) for site in self.sites)

    @property
    def fixed_count(self) -> int:
        return sum(len(site.fixed) for site in self.sites)

    @property
    def still_count(self) -> int:
        return sum(len(site.still) for site in self.sites)

    @property
    def noteworthy(self) -> list[SiteEntry]:
        return [site for site in self.sites if site.is_noteworthy]

    # Counts shown to a reader are pages, not stylesheet rows. One page can
    # reference several broken stylesheets, so rows overstate the damage --
    # and "how many pages are broken" is the question people actually ask.
    def _pages(self, attribute: str) -> int:
        return len({page for site in self.sites for page in getattr(site, attribute)})

    @property
    def found_pages_count(self) -> int:
        return self._pages("found_pages")

    @property
    def fixed_pages_count(self) -> int:
        return self._pages("fixed_pages")

    @property
    def still_pages_count(self) -> int:
        return self._pages("still_pages")

    @property
    def headline(self) -> str:
        parts = []
        if self.found_pages_count:
            parts.append(f"{self.found_pages_count} found")
        if self.fixed_pages_count:
            parts.append(f"{self.fixed_pages_count} fixed")
        if self.still_pages_count:
            parts.append(f"{self.still_pages_count} still broken")
        return ", ".join(parts) if parts else "nothing broken"


@dataclass
class DayReport:
    day: date_cls
    tz_name: str
    checks: list[CheckEntry] = field(default_factory=list)

    @property
    def check_count(self) -> int:
        return len(self.checks)

    @property
    def found_count(self) -> int:
        return sum(check.found_count for check in self.checks)

    @property
    def fixed_count(self) -> int:
        return sum(check.fixed_count for check in self.checks)

    @property
    def fixed_pages_count(self) -> int:
        return len(
            {
                page
                for check in self.checks
                for site in check.sites
                for page in site.fixed_pages
            }
        )

    @property
    def found_pages_count(self) -> int:
        """Distinct pages newly broken across the day, counted once each."""
        return len(
            {
                page
                for check in self.checks
                for site in check.sites
                for page in site.found_pages
            }
        )

    @property
    def sites_touched(self) -> int:
        """Distinct sites that appear anywhere in the day."""
        return len({site.domain for check in self.checks for site in check.sites})

    @property
    def sites_affected(self) -> int:
        return len(
            {
                site.domain
                for check in self.checks
                for site in check.noteworthy
                if site.found or site.still or site.fixed
            }
        )

    @property
    def outstanding(self) -> list[SiteEntry]:
        """Where each affected site stood at its last check of the day.

        The running total is what someone acts on tomorrow morning; the
        per-check breakdown above it is how it got there.
        """
        latest: dict[str, SiteEntry] = {}
        for check in self.checks:
            for site in check.sites:
                latest[site.domain] = site
        return [
            site
            for site in latest.values()
            if site.found or site.still or site.warning or site.error
        ]

    @property
    def is_empty(self) -> bool:
        return not self.checks


def build_day_report(database, day: date_cls, tz_name: str) -> DayReport:
    """Assemble one day from stored runs. Reads only; changes nothing."""
    start, end = day_bounds(day, tz_name)
    report = DayReport(day=day, tz_name=tz_name)

    for run in database.runs_between(start, end):
        entry = CheckEntry(run=run)
        for site_run in database.site_runs_for_run(run["id"]):
            current = database.broken_for_site_run(site_run["id"])
            earlier = database.previous_site_run(site_run["domain"], site_run["id"])
            # An empty list, not None, when the site has never been checked
            # before. On a site's own timeline a first check is reported as
            # such, because calling it all-new would read as a regression at
            # the moment the site was added. A day report is the opposite
            # case: it exists to say what was discovered, and those
            # breakages genuinely were discovered at this check.
            previous = database.broken_for_site_run(earlier["id"]) if earlier else []
            entry.sites.append(
                SiteEntry(
                    domain=site_run["domain"],
                    delta=compare(current, previous),
                    warning=site_run["warning"] or "",
                    error=site_run["error"] or "",
                    pages_checked=site_run["pages_checked"],
                    assets_checked=site_run["assets_checked"],
                )
            )
        report.checks.append(entry)

    return report
