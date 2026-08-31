"""Comparing one check of a site against the one before it.

The workflow this exists for: a page breaks, it gets fixed, the site is
checked again. Two reports side by side do not answer "did my fix work" --
someone has to diff two lists of URLs by eye. So the diff is computed here
instead, and every check carries what changed since the last one.

Nothing new is collected for this. It is entirely derived from the
broken_assets rows already stored against each site_run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

# A breakage is identified by the page it was found on plus the stylesheet
# URL. The ?ver= timestamp is part of the asset URL and deliberately kept:
# when Elementor regenerates a stylesheet the reference changes, and a
# different reference genuinely is a different breakage, not the same one.
BreakageKey = tuple[str, str]


def _key(row) -> BreakageKey:
    return (row["page_url"], row["asset_url"])


@dataclass(frozen=True)
class SiteDelta:
    """What changed between two checks of one site."""

    new: tuple = ()
    still: tuple = ()
    fixed: tuple = ()
    compared: bool = False  # False when there was no earlier check to compare

    @property
    def new_count(self) -> int:
        return len(self.new)

    @property
    def still_count(self) -> int:
        return len(self.still)

    @property
    def fixed_count(self) -> int:
        return len(self.fixed)

    @property
    def new_keys(self) -> frozenset:
        """Page+asset pairs that are newly broken, for labelling a list.

        Keyed on both halves, never the stylesheet alone: the same global
        stylesheet is referenced by every page on a site, so matching on the
        asset URL would mark it new everywhere the moment it broke anywhere.
        """
        return frozenset(_key(row) for row in self.new)

    @property
    def is_quiet(self) -> bool:
        """Nothing changed either way."""
        return not self.new and not self.fixed

    @property
    def summary(self) -> str:
        """One line for a list row -- the headline, not the detail."""
        if not self.compared:
            return "first check"
        parts = []
        if self.fixed:
            parts.append(f"{len(self.fixed)} fixed")
        if self.new:
            parts.append(f"{len(self.new)} new")
        if self.still:
            parts.append(f"{len(self.still)} still broken")
        return ", ".join(parts) if parts else "no change"


def compare(current: Sequence, previous: Sequence | None) -> SiteDelta:
    """Label this check's breakages against the previous check of the site.

    `previous` of None means there is nothing to compare against -- a first
    check, which is reported as such rather than as "all new". Everything
    looks new on a first check, and calling it that would read as a sudden
    regression when it is only the starting point.
    """
    current_by_key = {_key(row): row for row in current}

    if previous is None:
        return SiteDelta(new=(), still=tuple(current_by_key.values()), compared=False)

    previous_by_key = {_key(row): row for row in previous}

    new = tuple(
        row for key, row in current_by_key.items() if key not in previous_by_key
    )
    still = tuple(
        row for key, row in current_by_key.items() if key in previous_by_key
    )
    fixed = tuple(
        row for key, row in previous_by_key.items() if key not in current_by_key
    )
    return SiteDelta(new=new, still=still, fixed=fixed, compared=True)


def label_of(row, delta: SiteDelta) -> str:
    """Which bucket one breakage row falls into, for display."""
    if not delta.compared:
        return ""
    key = _key(row)
    if any(_key(other) == key for other in delta.new):
        return "new"
    return "still"


@dataclass
class TimelineEntry:
    """One check of a site, with what changed since the check before it."""

    site_run: object
    delta: SiteDelta
    broken: tuple = ()

    @property
    def state(self) -> str:
        """healthy | broken | unverified | failed -- one word for a badge."""
        row = self.site_run
        if row["error"]:
            return "failed"
        if row["broken_assets"]:
            return "broken"
        if row["warning"]:
            return "unverified"
        return "healthy"


def build_timeline(
    history: Iterable,
    broken_by_site_run: dict[int, Sequence],
) -> list[TimelineEntry]:
    """Turn a site's checks (newest first) into entries carrying their deltas.

    Each check is compared with the one immediately older than it. The oldest
    check in the window has nothing behind it, so it is marked as a first
    check rather than compared against nothing.
    """
    rows = list(history)
    entries: list[TimelineEntry] = []

    for index, row in enumerate(rows):
        current = broken_by_site_run.get(row["id"], ())
        older = rows[index + 1] if index + 1 < len(rows) else None
        previous = broken_by_site_run.get(older["id"], ()) if older else None
        entries.append(
            TimelineEntry(
                site_run=row,
                delta=compare(current, previous),
                broken=tuple(current),
            )
        )
    return entries
