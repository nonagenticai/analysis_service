"""Synchronous, deterministic artifact scoring — the pure critique core, inline.

There is no reusable ``run_critique(url)`` in the codebase: the only whole-
pipeline entry (``CritiqueService.process_job``) requires a persisted job row,
returns ``None``, commits repeatedly under RLS, and runs two LLM calls plus
live-network probes — none of which fit a synchronous request. So this module
reassembles the **pure, persistence-free** building blocks the async pipeline
also uses:

- the static, HTML-in scanners (``SEOScanner`` / ``A11yScanner`` /
  ``PerformanceScanner`` — each ``scan(html, url, *, soup) -> list[dict]``),
- the pure aggregators in :mod:`src.domains.critique.services.report_generator`
  (``deduplicate_findings`` / ``compute_scores`` / ``compute_overall_score`` /
  ``compute_confidence_scores``).

No LLM, no DB, no job row, no Redis. Fetching (URL input) and HTML parsing use
the shared :mod:`src.domains.scorecards.fetch` helpers (``fetch_html_capped`` /
``parse_soup``) also used by the cro / seo_audit / marketing scorecards, so this
module stays self-contained and trivially mockable in tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.domains.artifact_score.schemas import STATIC_CATEGORIES
from src.domains.critique.services.report_generator import (
    compare_findings,
    compute_confidence_scores,
    compute_score_deltas,
    compute_scores,
    deduplicate_findings,
    overall_score_for_complete_scan,
)
from src.domains.critique.services.scanners.a11y import A11yScanner
from src.domains.critique.services.scanners.perf import PerformanceScanner
from src.domains.critique.services.scanners.protocol import Scanner
from src.domains.critique.services.scanners.seo import SEOScanner
from src.domains.scorecards.errors import ScorecardError
from src.domains.scorecards.fetch import fetch_html_capped, parse_soup
from src.domains.scorecards.findings import worse_severity

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Stateless scanners, built once. The category string each emits matches the
# aggregator's expectations: seo→"seo", a11y→"accessibility", perf→"performance".
# All three satisfy the sync ``Scanner`` protocol (``scan(html, url, *, soup)``).
_SCANNERS: dict[str, Scanner] = {
    "seo": SEOScanner(),
    "accessibility": A11yScanner(),
    "performance": PerformanceScanner(),
}


class ArtifactScoreError(ScorecardError):
    """Artifact fetch/parse/scan failure — surfaced as a fail-closed 502."""


def resolve_categories(requested: list[str] | None) -> list[str]:
    """Return the ordered, de-duplicated static categories to score.

    ``None`` → all static categories. Otherwise keep only supported categories
    (dropping ux/security/seo_live), preserving request order. May return ``[]``
    when nothing valid was requested — the route turns that into a ``422``.
    """
    if not requested:
        return list(STATIC_CATEGORIES)
    return [c for c in dict.fromkeys(requested) if c in STATIC_CATEGORIES]


async def _default_fetch_html(url: str) -> str:
    """Fetch page HTML via the shared capped GET (httpx 30s, redirects, 10MB cap).

    Kept as a module-level function (not inlined) because the route tests
    monkeypatch ``artifact_score.service._default_fetch_html`` by that path.
    """
    return await fetch_html_capped(url, user_agent="ArtifactScoreService/1.0")


async def _scan(category: str, html: str, url: str, soup: BeautifulSoup | None) -> list[dict]:
    scanner = _SCANNERS[category]
    return await asyncio.to_thread(scanner.scan, html, url or "", soup=soup)


async def gather_findings(
    html: str, url: str | None, categories: list[str], soup: BeautifulSoup | None
) -> list[dict]:
    """Run the requested static scanners concurrently and flatten their findings."""
    results = await asyncio.gather(*(_scan(c, html, url or "", soup) for c in categories))
    findings: list[dict] = []
    for r in results:
        findings.extend(r)
    return findings


def _summarize(findings: list[dict]) -> list[dict]:
    """Roll findings up to one ``{category, count, top_severity}`` row per category."""
    by_cat: dict[str, dict[str, Any]] = {}
    for f in findings:
        cat = f.get("category", "")
        if not cat:
            continue
        entry = by_cat.setdefault(cat, {"count": 0, "top": "info"})
        entry["count"] += 1
        entry["top"] = worse_severity(entry["top"], f.get("severity", "info"))
    return [
        {"category": cat, "count": e["count"], "top_severity": e["top"]}
        for cat, e in sorted(by_cat.items())
    ]


async def score_artifact_with_findings(
    *,
    html: str | None,
    url: str | None,
    categories: list[str],
    fetch_html: Callable[[str], Awaitable[str]] | None = None,
) -> tuple[dict[str, Any], list[dict]]:
    """Score one artifact and return ``(payload, deduplicated_findings)``.

    The payload is the ``/api/brand/score-artifact`` wire body; the raw findings
    are returned alongside so the delta path can diff them with
    ``compare_findings``. Raises :class:`ArtifactScoreError` on failure. Exactly
    one of ``html`` / ``url`` is expected (the schema enforces it).
    """
    fetcher = fetch_html or _default_fetch_html
    if html is None:
        if url is None:
            raise ArtifactScoreError("no artifact source provided")
        try:
            html = await fetcher(url)
        except Exception as e:
            raise ArtifactScoreError(f"Failed to fetch URL: {e}") from e
    if not html or not html.strip():
        raise ArtifactScoreError("empty artifact content")

    try:
        soup = parse_soup(html)
        findings = await gather_findings(html, url, categories, soup)
    except Exception as e:
        raise ArtifactScoreError(f"Failed to scan artifact: {e}") from e

    findings = deduplicate_findings(findings)
    category_scores = compute_scores(findings)
    # A CLEAN artifact must not be scored as an F.
    #
    # ``compute_scores`` derives its category set FROM the findings, so a clean
    # scan yields ``{}`` — and ``compute_overall_score({})`` returns ``0.0``,
    # the worst possible score. That default is only defensible for the async
    # critique pipeline, where ``gather(return_exceptions=True)`` means "no
    # findings" can also mean "every scanner crashed and nothing was measured".
    #
    # It is NOT ambiguous here: ``gather_findings`` gathers WITHOUT
    # ``return_exceptions``, so any scanner failure propagates and this function
    # has already raised ``ArtifactScoreError`` (fail-closed 502) above. Reaching
    # this line with zero findings means every requested scanner ran and found
    # nothing — which is exactly the condition ``compute_category_score`` scores
    # as ``100.0``. Match it rather than shipping 0.0/grade F for a clean page.
    overall_score = overall_score_for_complete_scan(category_scores)
    payload = {
        "overall_score": overall_score,
        "category_scores": category_scores,
        "confidence": compute_confidence_scores(findings),
        "findings_summary": _summarize(findings),
        "source": url or "inline-html",
        "categories": list(categories),
        "scored_at": datetime.now(UTC).isoformat(),
    }
    return payload, findings


async def score_artifact(
    *,
    html: str | None,
    url: str | None,
    categories: list[str],
    fetch_html: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Score one artifact synchronously. Raises :class:`ArtifactScoreError` on failure.

    Exactly one of ``html`` / ``url`` is expected (the schema enforces it).
    """
    payload, _ = await score_artifact_with_findings(
        html=html, url=url, categories=categories, fetch_html=fetch_html
    )
    return payload


async def score_delta(
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    categories: list[str],
    fetch_html: Callable[[str], Awaitable[str]] | None = None,
) -> dict[str, Any]:
    """Score two artifacts and return their before/after delta in one call.

    ``before``/``after`` are each ``{"url": ..., "html": ...}`` (exactly one set,
    enforced by the schema). Raises :class:`ArtifactScoreError` if either side
    fails to fetch/scan — fail-closed, no partial delta.
    """
    before_payload, before_findings = await score_artifact_with_findings(
        html=before.get("html"), url=before.get("url"), categories=categories, fetch_html=fetch_html
    )
    after_payload, after_findings = await score_artifact_with_findings(
        html=after.get("html"), url=after.get("url"), categories=categories, fetch_html=fetch_html
    )
    new_findings, resolved_findings, persistent = compare_findings(before_findings, after_findings)
    score_deltas = compute_score_deltas(
        before_payload["category_scores"], after_payload["category_scores"]
    )
    overall_delta = round(
        float(after_payload["overall_score"]) - float(before_payload["overall_score"]), 1
    )
    return {
        "before": before_payload,
        "after": after_payload,
        "score_deltas": score_deltas,
        "overall_delta": overall_delta,
        "new_findings": _summarize(new_findings),
        "resolved_findings": _summarize(resolved_findings),
        "persistent_findings": persistent,
    }
