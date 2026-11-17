"""Synchronous, deterministic marketing scorecard — go-to-market composite, inline.

Reassembles three *reused* deterministic engines into one synchronous,
fail-closed call over a single page fetch:

* the CRO conversion scanner (``CROScanner`` + ``compute_cro_score``) → the
  ``conversion`` dimension, and its hero/value-prop sub-scores → ``content``;
* the SEO audit scanner (``SEOScanner.scan_audit`` + ``compute_seo_audit_score``)
  → the ``seo`` dimension (HTML-only; the live robots/llms.txt checks of the
  dedicated ``/api/seo/audit`` are intentionally skipped to keep this a single
  fast fetch);
* the light marketing scanner (``MarketingScanner``) → the ``competitive`` /
  ``brand`` / ``growth`` dimensions.

No LLM, no DB, no job row on the default path. Fetching (URL input) is a small
self-contained httpx GET mirroring the cro-score / seo-audit fetch, so this
module is fully self-contained and trivially mockable in tests. When
``include_llm`` is set, the existing CRO micro-judge annotates a few extracted
copy snippets — advisory only, and **fail-soft**: the deterministic composite is
computed first and returned unchanged regardless of the LLM outcome.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.domains.critique.services.report_generator import (
    _get_severity_weights,
    deduplicate_findings,
)
from src.domains.critique.services.scanners.cro import CROScanner, compute_cro_score
from src.domains.critique.services.scanners.marketing import (
    DIMENSION_WEIGHTS,
    MarketingScanner,
    compute_marketing_score,
    dimension_of,
)
from src.domains.critique.services.scanners.marketing.checks import (
    MARKETING_CHECKS,
    NOT_RUN_REASONS,
)
from src.domains.critique.services.scanners.marketing.facts import (
    MarketingFactsDict,
    build_marketing_facts,
    check_measurements,
)
from src.domains.critique.services.scanners.seo import SEOScanner, compute_seo_audit_score
from src.domains.scorecards.ai_status import AiStatusCollector, ai_requested
from src.domains.scorecards.claim_grounding import maybe_ground_claims
from src.domains.scorecards.errors import ScorecardError
from src.domains.scorecards.fetch import fetch_html_capped, parse_soup
from src.domains.scorecards.findings import (
    action_buckets,
    build_checks,
    build_findings,
    summarize_buckets,
)
from src.domains.scorecards.priority import apply_enrichment, with_projection
from src.domains.scorecards.recommendation_writer import maybe_write_recommendations
from src.domains.scorecards.report_critic import maybe_critique_report
from src.domains.scorecards.report_narrative import maybe_narrate_report

if TYPE_CHECKING:
    from bs4 import BeautifulSoup
    from w2v_common.clients import WMClient

logger = logging.getLogger(__name__)

_CRO_SCANNER = CROScanner()
_SEO_SCANNER = SEOScanner()
_MKT_SCANNER = MarketingScanner()

# The scorecard's OWN dimensions (design 2026-10-17 §2.3). content / conversion /
# seo are borrowed from the CRO + SEO engines and are reported as one reference
# row each — their cro-/seo- findings belong to those reports, never this one.
OWN_DIMENSIONS = frozenset({"competitive", "brand", "growth"})


def _is_own_dimension(dimension: str | None) -> bool:
    return dimension in OWN_DIMENSIONS


class MarketingScoreError(ScorecardError):
    """Marketing fetch/parse/scan failure — surfaced as a fail-closed 502."""


async def _default_fetch_html(url: str) -> str:
    """Fetch page HTML (capped httpx GET) — mirrors cro / seo_audit."""
    return await fetch_html_capped(url, user_agent="MarketingScorecardService/1.0")


def _scan_all(html: str, url: str, soup: BeautifulSoup | None) -> list[dict]:
    """Run the three deterministic scanners over the shared soup (in a thread)."""
    findings: list[dict] = []
    findings.extend(_CRO_SCANNER.scan(html, url, soup=soup))
    # HTML-only SEO audit (no live robots/llms.txt fetch) for a single fast pass.
    findings.extend(_SEO_SCANNER.scan_audit(html, url, soup=soup, include_llm=False))
    findings.extend(_MKT_SCANNER.scan(html, url, soup=soup))
    return findings


# capability -> the check whose FIRING means the capability is absent on the page.
_PRECONDITION_CHECKS: dict[str, str] = {
    "analytics": "mkt-growth-no-analytics",
    "og_image": "mkt-brand-no-og-image",
}


def _marketing_checks(
    raw_findings: list[dict], facts: MarketingFactsDict
) -> list[dict[str, object]]:
    """The ``mkt-*`` check inventory (own dimensions only — never cro-/seo- rows).

    Built from the marketing scanner's findings BEFORE cross-engine
    de-duplication: ``deduplicate_findings`` topic-merges e.g.
    ``mkt-brand-no-contact-info`` into ``seo-eeat-no-contact``, and reading the
    merged list would report a page with no contact details as a ``pass``.
    ``mkt-growth-no-consent`` only runs when analytics was detected and
    ``mkt-brand-incomplete-social-card`` only when an og:image is set — the
    absence finding of the precondition check is what says it did not run.
    """
    mkt = [f for f in raw_findings if str(f.get("check_id") or "").startswith("mkt-")]
    fired = {f.get("check_id") for f in mkt}
    available = [
        capability
        for capability, precondition in _PRECONDITION_CHECKS.items()
        if precondition not in fired
    ]
    return build_checks(
        MARKETING_CHECKS,
        mkt,
        bucket_key="dimension",
        available=available,
        not_run_reasons=NOT_RUN_REASONS,
        measured=check_measurements(facts),
        bucket_of=dimension_of,
        keep=_is_own_dimension,
    )


def _marketing_overall(findings: list[dict]) -> float:
    """Recompute the whole marketing composite over ``findings`` → overall float.

    Used as the black-box scorer for :func:`enrich_refs`'s projection. It rebuilds
    the CRO + SEO sub-scores from the same finding set, so removing *any* finding
    (cro-/seo-/mkt-) yields the correct composite delta — the marketing score
    borrows the CRO/SEO overalls, so they must be recomputed too, not held fixed.
    """
    cro = compute_cro_score(findings)
    seo = compute_seo_audit_score(findings)
    return compute_marketing_score(
        cro_overall=cro["overall_score"],
        seo_overall=seo["overall_score"],
        cro_subsection_scores=cro["subsection_scores"],
        marketing_findings=findings,
    )["overall_score"]


async def score_marketing(
    *,
    html: str | None,
    url: str | None,
    include_llm: bool = False,
    include_priority: bool = False,
    include_projection: bool = False,
    include_evidence: bool = False,
    include_unbucketed: bool = False,
    include_recommendations: bool = False,
    include_grounding: bool = False,
    include_exec_summary: bool = False,
    include_critic: bool = False,
    bucket_depth_cap: int | None = None,
    intent: str | None = None,
    sources: list[str] | None = None,
    fetch_html: Callable[[str], Awaitable[str]] | None = None,
    wm_client: WMClient | None = None,
) -> dict[str, Any]:
    """Score one page as a marketing asset synchronously. Raises :class:`MarketingScoreError`.

    Exactly one of ``html`` / ``url`` is expected (the schema enforces it). When
    ``include_llm`` is set, the CRO micro-judge annotates a few extracted copy
    snippets (headline / value-prop / testimonial) — advisory only and fail-soft:
    the deterministic composite is computed first and returned unchanged
    regardless of whether the LLM call succeeds. ``wm_client`` is injectable for
    tests; production passes ``app.state.wm_client``.
    """
    fetcher = fetch_html or _default_fetch_html
    if html is None:
        if url is None:
            raise MarketingScoreError("no page source provided")
        try:
            html = await fetcher(url)
        except Exception as e:
            raise MarketingScoreError(f"Failed to fetch URL: {e}") from e
    if not html or not html.strip():
        raise MarketingScoreError("empty page content")

    try:
        soup = parse_soup(html)
        findings = await asyncio.to_thread(_scan_all, html, url or "", soup)
    except Exception as e:
        raise MarketingScoreError(f"Failed to scan page: {e}") from e

    raw_findings = list(findings)
    findings = deduplicate_findings(findings)
    ai = AiStatusCollector(
        requested=ai_requested(
            include_llm=include_llm,
            include_recommendations=include_recommendations,
            include_grounding=include_grounding,
            include_exec_summary=include_exec_summary,
            include_critic=include_critic,
        )
    )

    cro = compute_cro_score(findings)
    seo = compute_seo_audit_score(findings)
    scorecard = compute_marketing_score(
        cro_overall=cro["overall_score"],
        seo_overall=seo["overall_score"],
        cro_subsection_scores=cro["subsection_scores"],
        marketing_findings=findings,
    )
    quick_wins, strategic = action_buckets(
        findings,
        bucket_of=dimension_of,
        bucket_key="dimension",
        drop_unbucketed=True,
        include_unbucketed=include_unbucketed,
    )

    # Tier-1 deterministic enrichment (opt-in). apply_enrichment is a no-op that
    # returns the untouched refs + ``None`` when no flag is set → byte-identical.
    quick_wins, strategic, projection = apply_enrichment(
        quick_wins,
        strategic,
        findings,
        weights=DIMENSION_WEIGHTS,
        bucket_of=dimension_of,
        bucket_key="dimension",
        severity_weights=_get_severity_weights(),
        score_fn=_marketing_overall,
        include_priority=include_priority,
        include_projection=include_projection,
        include_evidence=include_evidence,
        depth_cap=bucket_depth_cap,
    )

    # Tier-3 recommendation-writer: weave evidence-grounded fix prose onto the
    # actionable refs (opt-in via include_recommendations). Advisory + fail-soft.
    audit_refs = (*quick_wins, *strategic)
    await maybe_write_recommendations(
        audit_refs,
        findings,
        include_recommendations=include_recommendations,
        wm_client=wm_client,
        intent=intent,
        ai=ai,
    )

    # Tier-2 claim-grounding (A18): fact-check each written recommendation against
    # the page text; unsupported advice is dropped in place (SOFT). Advisory +
    # fail-soft; a no-op returning None when off or nothing was written.
    page_text = soup.get_text(" ", strip=True) if (include_grounding and soup is not None) else ""
    grounding = await maybe_ground_claims(
        audit_refs,
        include_grounding=include_grounding,
        wm_client=wm_client,
        page_text=page_text,
        sources=sources,
        ai=ai,
    )

    # Tier-3 enrichment: advisory annotation only — never an input to the score.
    # TODO(MKT-G4, owner decision §7.4): this still reuses the CRO micro-judge on
    # headline / value-prop / testimonial. A marketing-specific judge (positioning,
    # audience clarity, differentiation, proof strength) needs a NEW worker_management
    # worker that does not exist yet — not built here on purpose.
    from src.domains.cro.llm_judge import maybe_judge_snippets
    from src.domains.cro.service import _extract_llm_snippets

    judged = await maybe_judge_snippets(
        _extract_llm_snippets(soup) if include_llm else {},
        include_llm=include_llm,
        wm_client=wm_client,
        ai=ai,
    )
    llm_assessments: list[dict[str, Any]] = [{**j, "dimension": "content"} for j in (judged or [])]
    llm_enriched = judged is not None

    result: dict[str, object] = {
        "overall_score": scorecard["overall_score"],
        "grade": scorecard["grade"],
        "dimension_scores": scorecard["dimension_scores"],
        "dimensions": summarize_buckets(
            findings,
            scorecard["dimension_scores"],
            weights=DIMENSION_WEIGHTS,
            bucket_of=dimension_of,
            bucket_key="dimension",
        ),
        "quick_wins": quick_wins,
        "strategic": strategic,
        "source": url or "inline-html",
        "llm_enriched": llm_enriched,
        "llm_assessments": llm_assessments,
        "scored_at": datetime.now(UTC).isoformat(),
    }
    result = with_projection(result, projection)
    if grounding is not None:
        result["grounding"] = grounding

    # Tier-3 exec-summary (A2) + report-critic (A11): both advisory + fail-soft and
    # merged only when produced, so the default response stays byte-identical. The
    # critic re-runs the writer once over its flagged ids (bounded, no recursion).
    await maybe_narrate_report(
        result,
        refs=audit_refs,
        findings=findings,
        scores=scorecard,
        grounding=grounding,
        include_exec_summary=include_exec_summary,
        wm_client=wm_client,
        intent=intent,
        ai=ai,
    )
    await maybe_critique_report(
        result,
        refs=audit_refs,
        findings=findings,
        scores=scorecard,
        include_critic=include_critic,
        wm_client=wm_client,
        intent=intent,
        ai=ai,
    )
    # Design 2026-10-17 contract (additive). ``findings`` lists only the
    # scorecard's own mkt-* dimensions; borrowed cro-/seo- findings stay in the
    # CRO / SEO reports. Built LAST so rows carry any writer/critic prose.
    result["findings"] = build_findings(
        findings,
        bucket_of=dimension_of,
        bucket_key="dimension",
        weights=DIMENSION_WEIGHTS,
        severity_weights=_get_severity_weights(),
        quick_wins=quick_wins,
        strategic=strategic,
        keep=_is_own_dimension,
    )
    facts = build_marketing_facts(soup, dimension_scores=scorecard["dimension_scores"])
    result["checks"] = _marketing_checks(raw_findings, facts)
    result["facts"] = facts
    result["ai"] = ai.to_dict()
    return result
