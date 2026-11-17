"""Synchronous, deterministic CRO scoring — pure conversion audit, inline.

Reassembles the pure, persistence-free building blocks (the ``CROScanner`` +
``compute_cro_score`` + ``deduplicate_findings``) into a single synchronous,
fail-closed call. No LLM, no DB, no job row on the default path. Fetching (URL
input) is a small self-contained httpx GET mirroring the artifact-score fetch,
so this module is fully self-contained and trivially mockable in tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from src.domains.critique.services.report_generator import (
    _get_severity_weights,
    deduplicate_findings,
)
from src.domains.critique.services.scanners.cro import CROScanner, compute_cro_score
from src.domains.critique.services.scanners.cro.base import _TESTIMONIAL_CLASS_RE
from src.domains.critique.services.scanners.cro.checks import (
    CRO_CHECKS,
    FOOTER_REQUIREMENT,
    FORM_REQUIREMENT,
    NO_FOOTER_REASON,
    NO_FORM_REASON,
    measured_from_facts,
)
from src.domains.critique.services.scanners.cro.experiments import experiment_for
from src.domains.critique.services.scanners.cro.facts import CroFactsPayload, build_cro_facts
from src.domains.critique.services.scanners.cro.footer import has_bottom_region
from src.domains.critique.services.scanners.cro.form_friction import has_conversion_form
from src.domains.critique.services.scanners.cro.scoring import SECTION_WEIGHTS, _section_of
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
    from collections.abc import Collection

    from bs4 import BeautifulSoup
    from w2v_common.clients import WMClient

    from src.domains.cro.llm_judge import CroJudgment

logger = logging.getLogger(__name__)

_SNIPPET_MAX = 300  # per-snippet char cap for the (tiny) micro-judge prompt

_SCANNER = CROScanner()


class CroScoreError(ScorecardError):
    """CRO fetch/parse/scan failure — surfaced as a fail-closed 502."""


async def _default_fetch_html(url: str) -> str:
    """Fetch page HTML (capped httpx GET) — mirrors artifact_score."""
    return await fetch_html_capped(url, user_agent="CROScoreService/1.0")


def _extract_llm_snippets(soup: BeautifulSoup | None) -> dict[str, str]:
    """Pull ≤3 short copy snippets for the Tier-3 micro-judge (no mutation, no re-parse).

    Reuses the already-parsed soup: the first ``<h1>`` (headline), the first
    ``<h2>``/lead ``<p>`` (value prop), and the first testimonial-classed block or
    ``<blockquote>`` (testimonial). Returns only found, non-empty, length-capped
    snippets; an empty dict means "nothing to judge — skip the LLM entirely".
    """
    if soup is None:
        return {}
    snippets: dict[str, str] = {}
    h1 = soup.find("h1")
    if h1 and (text := h1.get_text(" ", strip=True)):
        snippets["headline"] = text[:_SNIPPET_MAX]
    value_prop = soup.find("h2") or soup.find("p")
    if value_prop and (text := value_prop.get_text(" ", strip=True)):
        snippets["value_prop"] = text[:_SNIPPET_MAX]
    testimonial = soup.find(class_=_TESTIMONIAL_CLASS_RE) or soup.find("blockquote")
    if testimonial and (text := testimonial.get_text(" ", strip=True)):
        snippets["testimonial"] = text[:_SNIPPET_MAX]
    return snippets


def _unmeasured_note(section: str) -> str:
    if section == "form":
        return f"{NO_FORM_REASON}; form friction was not measured"
    if section == "footer":
        return f"{NO_FOOTER_REASON}; the closing CTA and contact checks did not run"
    return "Not measured on this page"


def _unmeasured_sections(*, form_present: bool, footer_present: bool) -> frozenset[str]:
    """Sections whose checks could not run on this page (excluded from the overall)."""
    missing = set()
    if not form_present:
        missing.add("form")
    if not footer_present:
        missing.add("footer")
    return frozenset(missing)


def _available_requirements(*, form_present: bool, footer_present: bool) -> set[str]:
    """Check preconditions this page satisfies (see ``CheckSpec.requires``)."""
    out: set[str] = set()
    if form_present:
        out.add(FORM_REQUIREMENT)
    if footer_present:
        out.add(FOOTER_REQUIREMENT)
    return out


class _SectionRowBase(TypedDict):
    section: str
    score: float | None
    weight: float
    count: int
    top_severity: str


class SectionRow(_SectionRowBase, total=False):
    """One ``subsections[]`` row; ``measured``/``note`` only on unmeasured rows."""

    measured: bool
    note: str


def _summarize_sections(
    findings: list[dict],
    section_scores: dict[str, float | None],
    unmeasured: Collection[str],
) -> list[SectionRow]:
    """Per-section rollup where an UNMEASURED section is not dressed up as clean.

    ``summarize_buckets`` (shared with seo/marketing) defaults a missing bucket to
    100, so it gets only the measured scores; unmeasured rows are then rewritten
    to ``score=None`` + ``measured=False`` + a note (the SEO ``_summarize_groups``
    pattern).
    """
    # Rebuilt key-by-key (same keys, same order) so the row shape is checked.
    rows: list[SectionRow] = [
        {
            "section": str(raw["section"]),
            "score": raw["score"],
            "weight": raw["weight"],
            "count": raw["count"],
            "top_severity": str(raw["top_severity"]),
        }
        for raw in summarize_buckets(
            findings,
            {s: v for s, v in section_scores.items() if v is not None},
            weights=SECTION_WEIGHTS,
            bucket_of=_section_of,
            bucket_key="section",
        )
    ]
    for row in rows:
        if row["section"] in unmeasured:
            row["score"] = None
            row["measured"] = False
            row["note"] = _unmeasured_note(row["section"])
    return rows


def _attach_experiments(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Add the authored ``experiment`` to each finding row that has one (CRO-G4).

    A check with no catalog entry gets no ``experiment`` key at all, which the
    report renders as "hypothesis: not authored".
    """
    for row in rows:
        if (experiment := experiment_for(str(row.get("check_id") or ""))) is not None:
            row["experiment"] = experiment
    return rows


def _safe_facts(
    soup: BeautifulSoup | None, url: str, section_scores: dict[str, float | None]
) -> tuple[CroFactsPayload, dict[str, str]]:
    """Facts + the per-check measured values; a facts bug never fails the score."""
    if soup is None:
        return {}, {}
    try:
        facts = build_cro_facts(soup, url, subsection_scores=section_scores)
        return facts, measured_from_facts(facts)
    except Exception:
        logger.warning(
            "CRO facts extraction failed; returning the score without facts", exc_info=True
        )
        return {}, {}


async def score_cro(
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
    """Score one page for CRO synchronously. Raises :class:`CroScoreError` on failure.

    Exactly one of ``html`` / ``url`` is expected (the schema enforces it). When
    ``include_llm`` is set, a tiny Tier-3 micro-judge annotates a few extracted
    copy snippets (headline / value-prop / testimonial) — but this is advisory
    only and **fail-soft**: the deterministic score is computed first and returned
    unchanged regardless of whether the LLM call succeeds. ``wm_client`` is
    injectable for tests; production passes ``app.state.wm_client``.
    """
    fetcher = fetch_html or _default_fetch_html
    if html is None:
        if url is None:
            raise CroScoreError("no page source provided")
        try:
            html = await fetcher(url)
        except Exception as e:
            raise CroScoreError(f"Failed to fetch URL: {e}") from e
    if not html or not html.strip():
        raise CroScoreError("empty page content")

    try:
        soup = parse_soup(html)
        findings = await asyncio.to_thread(_SCANNER.scan, html, url or "", soup=soup)
        form_present = soup is not None and has_conversion_form(soup)
        footer_present = soup is not None and has_bottom_region(soup)
    except Exception as e:
        raise CroScoreError(f"Failed to scan page: {e}") from e

    findings = deduplicate_findings(findings)
    # CRO-G5: with no conversion form every form check is skipped, so the section
    # is NOT measured — never a clean 100 that lifts the overall. Same for a page
    # with no footer / closing section: the footer checks never ran.
    unmeasured = _unmeasured_sections(form_present=form_present, footer_present=footer_present)
    score = compute_cro_score(findings, unmeasured=unmeasured)
    ai = AiStatusCollector(
        requested=ai_requested(
            include_llm=include_llm,
            include_recommendations=include_recommendations,
            include_grounding=include_grounding,
            include_exec_summary=include_exec_summary,
            include_critic=include_critic,
        )
    )
    quick_wins, strategic = action_buckets(
        findings, bucket_of=_section_of, bucket_key="section", include_unbucketed=include_unbucketed
    )

    # Tier-1 deterministic enrichment (opt-in). apply_enrichment is a no-op that
    # returns the untouched refs + ``None`` when no flag is set, so the default
    # response is byte-identical.
    quick_wins, strategic, projection = apply_enrichment(
        quick_wins,
        strategic,
        findings,
        weights=SECTION_WEIGHTS,
        bucket_of=_section_of,
        bucket_key="section",
        severity_weights=_get_severity_weights(),
        score_fn=lambda fs: compute_cro_score(fs, unmeasured=unmeasured)["overall_score"],
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
    # The outcome (ok / failed / skipped / empty) is recorded into ``ai``.
    from src.domains.cro.llm_judge import maybe_judge_snippets

    judged = await maybe_judge_snippets(
        _extract_llm_snippets(soup) if include_llm else {},
        include_llm=include_llm,
        wm_client=wm_client,
        ai=ai,
    )
    llm_judgments: list[CroJudgment] = judged or []
    llm_enriched = judged is not None

    result: dict[str, object] = {
        "overall_score": score["overall_score"],
        "grade": score["grade"],
        "subsection_scores": score["subsection_scores"],
        "subsections": _summarize_sections(findings, score["subsection_scores"], unmeasured),
        "unmeasured_sections": score.get("unmeasured", []),
        "coverage": score.get("coverage", 1.0),
        "quick_wins": quick_wins,
        "strategic": strategic,
        "source": url or "inline-html",
        "llm_enriched": llm_enriched,
        "llm_judgments": llm_judgments,
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
        scores=score,
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
        scores=score,
        include_critic=include_critic,
        wm_client=wm_client,
        intent=intent,
        ai=ai,
    )
    # Design 2026-10-17 contract (additive): full findings, check inventory,
    # measured facts, and the machine-readable AI status. Built LAST so the
    # findings carry any prose the writer/critic attached to the refs.
    result["findings"] = _attach_experiments(
        build_findings(
            findings,
            bucket_of=_section_of,
            bucket_key="section",
            weights=SECTION_WEIGHTS,
            severity_weights=_get_severity_weights(),
            quick_wins=quick_wins,
            strategic=strategic,
        )
    )
    facts, measured = _safe_facts(soup, url or "", score["subsection_scores"])
    result["checks"] = build_checks(
        CRO_CHECKS,
        findings,
        bucket_key="section",
        available=_available_requirements(form_present=form_present, footer_present=footer_present),
        not_run_reasons={FORM_REQUIREMENT: NO_FORM_REASON, FOOTER_REQUIREMENT: NO_FOOTER_REASON},
        measured=measured,
        bucket_of=_section_of,
    )
    result["facts"] = facts
    result["ai"] = ai.to_dict()
    return result
