"""A FAILED scan must never be reported as a CLEAN one.

`_run_cross_page_linking` swallowed every crawl error and returned `[]`. An empty
finding slice scores 100.0 in the composite kernel, so a crawl that timed out
produced `internal_linking: score=100, count=0, top_severity="info"` — byte-identical
to a site whose internal linking is genuinely perfect — and that fabricated 100 was
folded into the 0.07-weighted mean, LIFTING the overall score.

The three axes below are the whole contract:

1. a group whose scan FAILED is distinguishable in the response from one that
   scanned clean (`score=None`, `measured=False`, a note, and a name in
   `unmeasured_groups`);
2. a genuinely empty-but-scanned group still scores 100.0 — the regression guard
   that matters most, because over-correcting here would report every clean site
   as broken;
3. the overall score does not silently benefit from the unmeasured group.
"""

from __future__ import annotations

import pytest

from src.domains.critique.services.scanners.composite import (
    apply_unmeasured,
    measured_coverage,
    score_buckets,
    weighted_overall,
)
from src.domains.critique.services.scanners.seo import (
    GROUP_WEIGHTS,
    compute_seo_audit_score,
    group_of,
)
from src.domains.seo_audit.service import audit_seo

# A page with real internal links and enough body text that the SINGLE-PAGE
# internal_linking checks (`seo-linking-generic-anchors` / `-no-internal` /
# `-insufficient-content`) stay quiet — so the group's only remaining input is
# the cross-page crawl, and the ambiguity under test is isolated.
_PAGE = """<html><head>
<title>Merqi Analytics Platform for Growing Teams</title>
<meta name="description" content="Merqi gives growing product teams one shared
analytics workspace: funnels, retention and revenue in a single view, with no
SQL required and no data engineer needed to get started today.">
<link rel="canonical" href="https://example.test/">
<meta name="viewport" content="width=device-width, initial-scale=1">
</head><body>
<h1>Analytics for growing product teams</h1>
<p>Merqi brings funnels, retention and revenue reporting into one workspace so
product teams can answer their own questions without waiting on a data engineer.
Every metric is defined once and reused everywhere, which keeps the numbers in a
board deck and the numbers in a dashboard the same numbers.</p>
<h2>How teams use Merqi</h2>
<p>Teams start by connecting a warehouse, then build funnels over the events
they already collect. Retention cohorts and revenue breakdowns come from the
same definitions, so nothing drifts between reports over the course of a quarter.
A product manager can open a funnel, split it by plan tier, and see where trial
users stall without asking anyone to write a query. An analyst can review those
same definitions, correct one of them, and know that every dashboard built on it
updates at the same moment rather than drifting quietly apart over months.</p>
<h2>Definitions live in one place</h2>
<p>Every metric in Merqi is written down once. Activation, weekly active teams,
expansion revenue and churn all resolve against the same event contracts, which
means a number quoted in a board deck and a number shown on a dashboard are the
same number. When a definition changes, the change is versioned and the history
stays readable, so a quarter-over-quarter comparison never silently changes its
own meaning underneath the people reading it.</p>
<h2>Getting started</h2>
<p>Connect a warehouse, map a handful of events, and open the first funnel in an
afternoon. Most teams have retention cohorts running the same week, and revenue
reporting shortly after that, without adding a single data engineering ticket to
anyone else's backlog or waiting on a quarterly planning cycle to begin.</p>
<a href="/pricing">Merqi pricing plans</a>
<a href="/product/funnels">Funnel analysis</a>
<a href="/product/retention">Retention cohorts</a>
<img src="/hero.png" alt="Merqi analytics dashboard">
</body></html>"""


def _group(result: dict, name: str) -> dict:
    """The one `groups` row for `name` — KeyError-loud if the rollup dropped it."""
    return next(row for row in result["groups"] if row["group"] == name)


async def _audit(monkeypatch: pytest.MonkeyPatch, *, crawl: list[dict] | Exception) -> dict:
    """Run the audit against a URL with `scan_internal_linking_live` stubbed.

    `crawl` is either the findings the crawl returns or the exception it raises.
    Patched at its DEFINITION site, because `_run_cross_page_linking` imports the
    symbol lazily inside the function body — patching the caller's module would
    silently miss.
    """

    async def _fake(
        url: str, home_soup: object = None, *, observe: dict | None = None, **kwargs: object
    ) -> list[dict]:
        if isinstance(crawl, Exception):
            raise crawl
        if observe is not None:  # a crawl that RAN (reached pages) and found `crawl`
            observe.update({"ran": True, "reason": None, "pages_crawled": 3, "pages_reached": 3})
        return crawl

    monkeypatch.setattr(
        "src.domains.critique.services.scanners.seo.internal_linking_live."
        "scan_internal_linking_live",
        _fake,
    )
    return await audit_seo(
        html=None,
        url="https://example.test/",
        fetch_html=_fetch,
        cross_page_linking=True,
    )


async def _fetch(url: str) -> str:
    return _PAGE


# ── 1. a FAILED scan is distinguishable from a CLEAN one ──────────────────────


async def test_failed_crawl_is_distinguishable_from_a_clean_scan(monkeypatch):
    """The core assertion: the two responses must not be byte-identical.

    Same page, same stub signature — the ONLY difference is whether the crawl
    raised. Before the fix both produced `score=100.0, count=0, measured absent`.
    """
    failed = await _audit(monkeypatch, crawl=TimeoutError("crawl timed out"))
    clean = await _audit(monkeypatch, crawl=[])

    failed_row = _group(failed, "internal_linking")
    clean_row = _group(clean, "internal_linking")

    # The clean scan keeps the historical shape exactly.
    assert clean_row["score"] == 100.0
    assert clean_row.get("measured", True) is True
    assert clean_row.get("note") is None
    # `performance` is ALWAYS unmeasured on this path (SEO-G1) and weight-0, so
    # it is listed without touching coverage.
    assert clean["unmeasured_groups"] == ["performance"]
    assert clean["coverage"] == 1.0

    # The failed scan is UNMEASURED, not clean — and says so four ways.
    assert failed_row["score"] is None
    assert failed_row["measured"] is False
    assert "UNMEASURED" in failed_row["note"]
    assert failed["unmeasured_groups"] == ["internal_linking", "performance"]
    assert failed["group_scores"]["internal_linking"] is None
    assert failed["coverage"] < 1.0

    # And the two responses genuinely differ (the property the defect violated).
    assert failed_row != clean_row


async def test_unmeasured_note_names_what_was_skipped(monkeypatch):
    """A generic 'scan failed' is a worse diagnostic than none — name the signals."""
    failed = await _audit(monkeypatch, crawl=RuntimeError("dns failure"))
    note = _group(failed, "internal_linking")["note"]
    assert "cross-page" in note
    assert "internal_linking" in note
    # It must be readable as neither a pass nor a finding.
    assert "not clean" in note


# ── 2. REGRESSION GUARD: an empty-but-scanned group still scores 100 ──────────


async def test_genuinely_empty_scanned_group_still_scores_100(monkeypatch):
    """Over-correcting would report a perfect site as broken. It must not."""
    clean = await _audit(monkeypatch, crawl=[])
    assert _group(clean, "internal_linking")["score"] == 100.0
    assert clean["group_scores"]["internal_linking"] == 100.0
    assert clean["unmeasured_groups"] == ["performance"]


async def test_no_crawl_requested_is_unchanged():
    """`cross_page_linking=False` is the default contract — nothing is unmeasured."""
    result = await audit_seo(html=_PAGE, url=None)
    assert result["unmeasured_groups"] == ["performance"]
    assert result["coverage"] == 1.0
    assert all(
        row.get("measured", True) is True
        for row in result["groups"]
        if row["group"] != "performance"
    )
    assert all(score is not None for score in result["group_scores"].values())


def test_default_scoring_path_is_byte_identical():
    """The kernel change is opt-in: no `unmeasured` ⇒ the old dict, key for key."""
    findings = [
        {"category": "seo", "check_id": "seo-missing-h1", "severity": "high"},
        {"category": "seo", "check_id": "seo-linking-orphan", "severity": "medium"},
    ]
    assert compute_seo_audit_score(findings) == compute_seo_audit_score(findings, unmeasured=())
    assert "unmeasured" not in compute_seo_audit_score(findings)
    assert "coverage" not in compute_seo_audit_score(findings)


# ── 3. the overall score does not benefit from the gap ────────────────────────


async def test_overall_score_does_not_benefit_from_an_unmeasured_group():
    """A fabricated 100 in a 0.07-weight group used to pull the mean UP.

    The page is scored twice against the SAME findings; the only difference is
    whether `internal_linking` is credited a 100 it did not earn. Renormalising
    over the measured weight must leave the mean strictly below the credited one
    (the other groups average below 100 on this page).
    """
    findings = [
        {"category": "seo", "check_id": "seo-missing-h1", "severity": "high"},
        {"category": "seo", "check_id": "seo-canonical-missing", "severity": "medium"},
    ]
    scored = score_buckets(findings, category="seo", weights=GROUP_WEIGHTS, bucket_of=group_of)
    assert scored["internal_linking"] == 100.0, "the fabricated 100 under test"

    credited = weighted_overall(scored, GROUP_WEIGHTS)
    honest = weighted_overall({**scored, "internal_linking": None}, GROUP_WEIGHTS)
    zeroed = weighted_overall({**scored, "internal_linking": 0.0}, GROUP_WEIGHTS)

    assert honest < credited, "an unmeasured group must not lift the overall score"
    # And it is not the other lie either — the group is EXCLUDED, never zeroed.
    assert honest > zeroed
    # The service-level result agrees with the kernel.
    assert (
        compute_seo_audit_score(findings, unmeasured={"internal_linking"})["overall_score"]
        == honest
    )
    assert compute_seo_audit_score(findings)["overall_score"] == credited


def test_weighted_overall_renormalises_over_measured_weight():
    """Exclusion, not substitution: the mean is over the surviving weight only."""
    weights = {"a": 0.5, "b": 0.3, "c": 0.2}
    # With c unmeasured the mean is (80*0.5 + 60*0.3) / 0.8 = 72.5, NOT 58.0 (c=0)
    # and NOT 78.0 (c=100).
    assert weighted_overall({"a": 80.0, "b": 60.0, "c": None}, weights) == 72.5
    assert weighted_overall({"a": 80.0, "b": 60.0, "c": 0.0}, weights) == 58.0
    assert weighted_overall({"a": 80.0, "b": 60.0, "c": 100.0}, weights) == 78.0


def test_all_unmeasured_raises_rather_than_inventing_a_number():
    """There is no honest score for a scorecard whose every bucket failed."""
    weights = {"a": 0.5, "b": 0.5}
    with pytest.raises(ValueError, match="unmeasured"):
        weighted_overall({"a": None, "b": None}, weights)


def test_apply_unmeasured_default_is_a_plain_copy():
    scores = {"a": 100.0, "b": 42.0}
    assert apply_unmeasured(scores, ()) == scores
    assert apply_unmeasured(scores, {"b"}) == {"a": 100.0, "b": None}


def test_measured_coverage_reports_the_measured_weight_share():
    assert measured_coverage({b: 100.0 for b in GROUP_WEIGHTS}, GROUP_WEIGHTS) == 1.0
    partial = {**{b: 100.0 for b in GROUP_WEIGHTS}, "internal_linking": None}
    assert measured_coverage(partial, GROUP_WEIGHTS) == pytest.approx(0.93, abs=0.005)
