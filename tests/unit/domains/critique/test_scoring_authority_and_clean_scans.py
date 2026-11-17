"""Regressions for three ways a WRONG NUMBER reached a delivered report.

1. A clean scan was scored 0.0 / grade F (``compute_overall_score({}) == 0.0``)
   in the async critique pipeline, while its sibling artifact scorer guarded
   against exactly that. Fixed WITHOUT fail-opening: an empty finding set only
   scores 100.0 when no scanner reported measuring nothing.
2. A transport failure fetching robots.txt / sitemap.xml was emitted as a SITE
   defect ("Missing robots.txt", medium), costing real SEO points and putting a
   false remediation in the customer's PDF.
3. The Markdown report computed an UNWEIGHTED mean while the API and PDF used
   the weighted scorer, so one job carried two authoritative overall scores.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.domains.artifact_score.service import (
    resolve_categories,
    score_artifact_with_findings,
)
from src.domains.critique.models import CritiqueJob
from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.report_generator import (
    _md_scores_section,
    compute_overall_score,
    overall_score_for_complete_scan,
)
from src.domains.critique.services.scanners.seo.technical import scan_live

_TENANT = "7eed672d-a7ff-5f16-87d7-b6a493fee523"


async def _fake_summary(
    _active: object, _url: object, _scores: object, *, llm_config: object = None
) -> tuple[str, None]:
    return "summary", None


async def _score(db_session, findings: list[dict], incomplete: list[str] | None):
    svc = CritiqueService()
    job = CritiqueJob(url="https://example.com", domain="example.com", max_pages=1)
    with patch(
        "src.domains.critique.services.critique_service.generate_summary",
        side_effect=_fake_summary,
    ):
        return await svc._phase_score_and_summarize(
            db_session,
            job,
            _TENANT,
            findings,
            start_time=0.0,
            github_repo=None,
            llm_config=None,
            incomplete_scanners=incomplete,
        )


# --------------------------------------------------------------------------
# 1. a clean scan must not be delivered as 0.0 / grade F
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_complete_scan_scores_100_not_zero(db_session) -> None:
    """Every scanner ran and found nothing -> a PERFECT page, not the worst one."""
    result = await _score(db_session, [], [])

    # The guard is only exercised if `scores` really is empty. Assert that, so a
    # future fixture that smuggles in a category cannot make this test vacuous.
    assert result.scores == {}, "fixture produced categories; the guard is untested"
    assert result.overall_score == 100.0, (
        f"a flawless page was scored {result.overall_score}, not 100.0"
    )


@pytest.mark.asyncio
async def test_scan_emptied_by_suppression_scores_100(db_session) -> None:
    """A category-scoped suppression removing the last finding leaves a CLEAN scan."""
    findings = [
        {
            "id": "f1",
            "category": "seo",
            "severity": "high",
            "title": "t",
            "is_suppressed": True,
        }
    ]
    result = await _score(db_session, findings, [])

    assert result.scores == {}, "fixture produced categories; the guard is untested"
    assert result.overall_score == 100.0


@pytest.mark.asyncio
async def test_incomplete_scan_does_not_fail_open_to_100(db_session) -> None:
    """A scan where a scanner measured NOTHING must not be sold as a clean page.

    This is the other half of the fix. The sibling artifact scorer may map an
    empty finding set to 100.0 because it gathers WITHOUT
    ``return_exceptions``; the critique pipeline gathers WITH it, so an empty
    finding set is genuinely ambiguous and must stay conservative.
    """
    result = await _score(db_session, [], ["seo", "a11y"])

    assert result.scores == {}
    assert result.overall_score != 100.0, (
        "an UNMEASURED scan was reported as a perfect score -- fail-open on a measurement gap"
    )
    assert result.overall_score == 0.0


@pytest.mark.asyncio
async def test_completeness_unknown_is_treated_as_incomplete(db_session) -> None:
    """Omitting the signal must give the conservative answer, not a silent 100."""
    result = await _score(db_session, [], None)
    assert result.overall_score == 0.0


# --------------------------------------------------------------------------
# 2. a transport failure is not a site defect
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_id", "errored_id", "path"),
    [
        ("seo-robots-txt-missing", "seo-robots-txt-errored", "/robots.txt"),
        ("seo-sitemap-missing", "seo-sitemap-errored", "/sitemap.xml"),
    ],
)
async def test_transport_failure_is_not_reported_as_a_missing_file(
    missing_id: str, errored_id: str, path: str
) -> None:
    """A timeout on the customer's origin must not claim the file is absent."""

    async def _boom(self: object, url: str, *a: object, **k: object) -> httpx.Response:
        if url.endswith(path):
            raise httpx.ConnectTimeout("origin timed out")
        return httpx.Response(200, request=httpx.Request("GET", url))

    with (
        patch.object(httpx.AsyncClient, "get", _boom),
        patch(
            "src.domains.critique.services.scanners.seo.technical._is_ssrf_safe_resolved",
            new=AsyncMock(return_value=True),
        ),
    ):
        findings = await scan_live("https://example.com/page")

    ids = [f.get("check_id") for f in findings]
    assert missing_id not in ids, (
        f"a TRANSPORT failure was emitted as {missing_id} -- a false remediation "
        "telling the customer to create a file they already have"
    )
    errored = [f for f in findings if f.get("check_id") == errored_id]
    assert errored, f"the unmeasured check was dropped silently; got {ids}"
    assert errored[0]["severity"] == "info", (
        f"'could not check' was scored as a defect ({errored[0]['severity']})"
    )


@pytest.mark.asyncio
async def test_transport_failure_costs_no_score() -> None:
    """The errored finding must carry ZERO scoring penalty (info weight is 0)."""
    from src.domains.critique.services.report_generator import compute_scores

    async def _boom(self: object, url: str, *a: object, **k: object) -> httpx.Response:
        raise httpx.ConnectTimeout("origin timed out")

    with (
        patch.object(httpx.AsyncClient, "get", _boom),
        patch(
            "src.domains.critique.services.scanners.seo.technical._is_ssrf_safe_resolved",
            new=AsyncMock(return_value=True),
        ),
    ):
        findings = await scan_live("https://example.com/page")

    assert findings, "expected the unmeasured checks to be recorded"
    assert compute_scores(findings) == {"seo": 100.0}, (
        "an unreachable origin cost the customer SEO points"
    )


# --------------------------------------------------------------------------
# 3. exactly one scorer  (+ structural sibling agreement)
# --------------------------------------------------------------------------


def test_markdown_overall_matches_the_weighted_api_score() -> None:
    """The Markdown report must not publish a second, disagreeing number."""
    scores = {"accessibility": 60.0, "seo": 90.0}
    expected = compute_overall_score(scores)

    section = "\n".join(_md_scores_section(scores))

    assert f"**Overall Score: {expected:.1f}/100**" in section, (
        f"Markdown disagrees with the API scorer ({expected}); section:\n{section}"
    )
    # Pin the specific drift that shipped: the unweighted mean of this input.
    unweighted = sum(scores.values()) / len(scores)
    assert unweighted != expected, "fixture no longer discriminates the two formulas"
    assert f"{unweighted:.1f}/100" not in section


@pytest.mark.parametrize(
    "scores",
    [
        {},
        {"seo": 90.0},
        {"accessibility": 60.0, "seo": 90.0},
        {"security": 10.0, "performance": 80.0, "ux": 55.0},
    ],
)
def test_both_scorers_agree_for_the_same_input(scores: dict[str, float]) -> None:
    """Structural guard: the clean-scan rule has exactly ONE definition.

    ``artifact_score`` and the critique pipeline both route an
    established-complete scan through ``overall_score_for_complete_scan``. If
    someone re-inlines the rule in one of them, this drifts and fails.
    """
    shared = overall_score_for_complete_scan(scores)
    if scores:
        assert shared == compute_overall_score(scores)
    else:
        assert shared == 100.0


@pytest.mark.asyncio
async def test_artifact_scorer_reports_100_for_a_clean_page() -> None:
    """The sibling still honours the rule through the shared helper."""
    payload, _ = await score_artifact_with_findings(
        html="<html><head><title>t</title></head><body><h1>h</h1></body></html>",
        url=None,
        categories=resolve_categories(None),
    )
    if not payload["category_scores"]:
        assert payload["overall_score"] == 100.0


def test_errored_checks_group_with_the_check_they_stand_in_for() -> None:
    """An unmeasured check must not be reassigned to the default audit bucket."""
    from src.domains.critique.services.scanners.seo import group_of

    for errored, missing in (
        ("seo-robots-txt-errored", "seo-robots-txt-missing"),
        ("seo-sitemap-errored", "seo-sitemap-missing"),
    ):
        assert group_of(errored) == group_of(missing) == "technical", (
            f"{errored} landed in {group_of(errored)}, not with {missing}"
        )
