"""Comparing a check against the one before it.

The question these answer: "I fixed it and re-checked -- did it work?"
"""

from __future__ import annotations

from site_monitor.history import build_timeline, compare


def breakage(page: str, asset: str) -> dict:
    return {"page_url": page, "asset_url": asset, "reason": "HTTP 404"}


PAGE_A = "https://x.test/a/"
PAGE_B = "https://x.test/b/"
OLD_CSS = "https://x.test/elementor/css/post-1.css?ver=111"
NEW_CSS = "https://x.test/elementor/css/post-1.css?ver=222"
OTHER = "https://x.test/elementor/css/post-2.css?ver=333"


# -- the fix-and-recheck loop -------------------------------------------------


def test_a_breakage_that_is_gone_is_reported_as_fixed():
    previous = [breakage(PAGE_A, OLD_CSS)]

    delta = compare([], previous)

    assert delta.fixed_count == 1
    assert delta.new_count == 0
    assert delta.still_count == 0
    assert delta.summary == "1 fixed"


def test_a_breakage_that_persists_is_still_not_new():
    row = breakage(PAGE_A, OLD_CSS)

    delta = compare([row], [row])

    assert delta.still_count == 1
    assert delta.new_count == 0
    assert delta.fixed_count == 0
    assert delta.summary == "1 still broken"
    assert delta.is_quiet


def test_a_breakage_that_appeared_since_last_time_is_new():
    delta = compare([breakage(PAGE_A, OLD_CSS)], [])

    assert delta.new_count == 1
    assert delta.summary == "1 new"
    assert not delta.is_quiet


def test_one_fixed_one_new_reports_both():
    delta = compare(
        [breakage(PAGE_B, OTHER)],
        [breakage(PAGE_A, OLD_CSS)],
    )

    assert delta.summary == "1 fixed, 1 new"


def test_a_regenerated_stylesheet_is_a_different_breakage():
    """A new ?ver= is genuinely a new reference, not the same one persisting."""
    delta = compare([breakage(PAGE_A, NEW_CSS)], [breakage(PAGE_A, OLD_CSS)])

    assert delta.new_count == 1
    assert delta.fixed_count == 1
    assert delta.still_count == 0


def test_the_same_stylesheet_on_two_pages_is_two_breakages():
    """A global stylesheet is referenced everywhere; each page is its own row."""
    delta = compare(
        [breakage(PAGE_A, OTHER), breakage(PAGE_B, OTHER)],
        [breakage(PAGE_A, OTHER)],
    )

    assert delta.still_count == 1
    assert delta.new_count == 1
    assert (PAGE_B, OTHER) in delta.new_keys
    assert (PAGE_A, OTHER) not in delta.new_keys


# -- a first check is not a regression ----------------------------------------


def test_a_first_check_is_not_reported_as_all_new():
    """Everything looks new when there is no history; that is not a regression."""
    delta = compare([breakage(PAGE_A, OLD_CSS)], None)

    assert not delta.compared
    assert delta.new_count == 0
    assert delta.summary == "first check"


def test_a_clean_check_after_a_clean_check_says_no_change():
    delta = compare([], [])

    assert delta.summary == "no change"
    assert delta.is_quiet


# -- timelines ----------------------------------------------------------------


def site_run(run_id: int, broken: int = 0, warning: str = "", error=None) -> dict:
    return {
        "id": run_id,
        "run_id": run_id,
        "broken_assets": broken,
        "warning": warning,
        "error": error,
    }


def test_a_timeline_compares_each_check_with_the_one_before_it():
    # Newest first, as the database returns them.
    history = [site_run(3), site_run(2, broken=1), site_run(1, broken=1)]
    broken_by_site_run = {
        3: [],
        2: [breakage(PAGE_A, OLD_CSS)],
        1: [breakage(PAGE_A, OLD_CSS)],
    }

    entries = build_timeline(history, broken_by_site_run)

    assert [e.delta.summary for e in entries] == [
        "1 fixed",        # newest: the breakage is gone
        "1 still broken",  # middle: it was there last time too
        "first check",     # oldest in the window has nothing behind it
    ]


def test_timeline_entries_carry_a_state_for_a_badge():
    history = [
        site_run(4, error="sitemap unreachable"),
        site_run(3, warning="no Elementor stylesheets found"),
        site_run(2, broken=2),
        site_run(1),
    ]

    states = [entry.state for entry in build_timeline(history, {})]

    assert states == ["failed", "unverified", "broken", "healthy"]


def test_an_empty_history_produces_no_entries():
    assert build_timeline([], {}) == []
