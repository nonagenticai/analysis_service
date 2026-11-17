"""Synchronous, deterministic SEO discoverability audit — inline.

Reassembles the pure, persistence-free building blocks (``SEOScanner.scan_audit``
+ ``compute_seo_audit_score`` + ``deduplicate_findings``) into a single
synchronous, fail-closed call. No LLM, no DB, no job row on the default path.
With a URL it also runs the best-effort robots.txt / sitemap.xml / llms.txt
live probes (design 2026-10-17 SEO-G2).
Fetching (URL input) is a small self-contained httpx GET mirroring the
artifact-score / cro-score fetch, so this module is fully self-contained and
trivially mockable in tests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from src.domains.critique.services.report_generator import (
    _get_severity_weights,
    deduplicate_findings,
)
from src.domains.critique.services.scanners.seo import (
    GROUP_WEIGHTS,
    SEOScanner,
    compute_seo_audit_score,
    group_of,
)
from src.domains.critique.services.scanners.seo.catalog import (
    LIVE_CHECK_PROBES,
    SEO_CHECK_CATALOG,
    STATUS_MARKER_IDS,
)
from src.domains.critique.services.scanners.seo.facts import (
    NO_URL_REASON,
    PERFORMANCE_NOT_MEASURED,
    SeoFacts,
    build_seo_facts,
)
from src.domains.critique.services.scanners.seo.observations import (
    CrawlObservation,
    LiveObservation,
    live_probe,
    probe_part,
)
from src.domains.critique.services.scanners.seo.seo_llm import (
    SeoContentGapAnalyzer,
    SeoEeatDepthAnalyzer,
    SeoIntentClassifier,
    SeoLlmAnnotation,
    SeoLlmOutcome,
    enrich_seo_audit,
)
from src.domains.scorecards.ai_status import AiLayerName, AiStatusCollector, ai_requested
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
    from w2v_common.clients.wm_client import WMClient

logger = logging.getLogger(__name__)

# Bound the opt-in competitor content-gap fan-out (cost + the single WM call).
_MAX_COMPETITORS = 3

_SCANNER = SEOScanner()

# ⛔ A group whose scanner never ran gets its OWN note, worded so it can never be
# read as either "passed" (no note) or "failed" (a finding). Same convention as
# `w2v_orchestrator`'s `mastering_gate._UNMEASURED_NOTE`: an absent measurement
# is UNMEASURED, never a clean one. Named per group because only the group knows
# what was skipped.
_UNMEASURED_NOTES: dict[str, str] = {
    "internal_linking": (
        "internal_linking: the cross-page crawl did not complete, so the site-wide "
        "orphan / broken-link / hub signals were never measured — this group is "
        "UNMEASURED, not clean, and is excluded from the overall score"
    ),
}


def _unmeasured_note(group: str, detail: str | None = None) -> str:
    """The note for an unmeasured group — never a generic 'scan failed'.

    ``detail`` (e.g. "the start URL was rejected by the SSRF guard") names WHY,
    so the report can say more than "did not complete".
    """
    note = _UNMEASURED_NOTES.get(
        group,
        f"{group}: this group's scanner did not run — UNMEASURED, not clean, and "
        "excluded from the overall score",
    )
    return f"{note} (reason: {detail})" if detail else note


PERFORMANCE_GROUP = "performance"


class SeoAuditError(ScorecardError):
    """SEO-audit fetch/parse/scan failure — surfaced as a fail-closed 502."""


async def _default_fetch_html(url: str) -> str:
    """Fetch page HTML (capped httpx GET) — mirrors artifact_score."""
    return await fetch_html_capped(url, user_agent="SeoAuditService/1.0")


async def _run_cross_page_linking(
    url: str, soup: BeautifulSoup | None, max_pages: int
) -> tuple[list[dict], dict[str, str]]:
    """Best-effort cross-page internal-linking crawl — never raises into the audit.

    Returns ``(findings, unmeasured_notes)`` — ``{group: note}`` for each group
    the crawl could not measure. ⛔ The second element is the whole point: a crawl
    that raised, was SSRF-blocked, could not reach the home page, or reached no
    page returns ``([], {"internal_linking": <note with reason>})``, which is NOT
    the same fact as a crawl that ran and found nothing (``([], {})``). Collapsing
    both to a bare ``[]`` is what let a failed crawl score ``internal_linking`` a
    perfect 100 — indistinguishable from a clean site.
    """
    from src.domains.critique.services.scanners.seo.internal_linking_live import (
        scan_internal_linking_live,
    )

    observe: dict = {}
    try:
        found = await scan_internal_linking_live(
            url, home_soup=soup, max_pages=max_pages, observe=observe
        )
    except Exception as e:  # noqa: BLE001 - cross-page linking is best-effort, never fatal
        logger.warning("SEO cross-page linking failed, continuing without it: %s", e)
        # The caller asked for the cross-page signals and did not get them. The gap
        # is RECORDED, never swallowed into an empty finding list.
        detail = f"the crawl raised {type(e).__name__}"
        return [], {"internal_linking": _unmeasured_note("internal_linking", detail)}
    if not observe.get("ran"):
        detail = str(observe.get("reason") or "the crawl did not run")
        logger.warning("SEO cross-page linking did not measure anything: %s", detail)
        return found, {"internal_linking": _unmeasured_note("internal_linking", detail)}
    return found, {}


async def _run_live_checks(url: str) -> tuple[list[dict], LiveObservation]:
    """robots.txt / sitemap.xml (technical) + AI-crawler / llms.txt (GEO) — best-effort.

    Returns ``(findings, live)`` where ``live`` holds what each probe actually
    observed (``{"technical": {...}, "geo": {...}}``). A probe that raised is
    recorded with its error, so its checks become ``not_run`` — a live-fetch
    failure must never fail the audit, and must never read as a pass.
    """
    findings: list[dict] = []
    live: LiveObservation = {"technical": {}, "geo": {}}
    probes: tuple[tuple[str, Callable[..., Awaitable[list[dict]]]], ...] = (
        ("technical", _SCANNER.scan_live),
        ("geo", _SCANNER.scan_audit_live),
    )
    for name, probe in probes:
        try:
            findings.extend(await probe(url, observe=live_probe(live, name)))
        except Exception as e:  # noqa: BLE001 - live checks are best-effort, never fatal
            logger.warning("SEO live %s checks failed, continuing without them: %s", name, e)
            observed = live_probe(live, name)
            observed["ran"] = False
            observed["error"] = f"{type(e).__name__}: {e}"[:240]
    return findings, live


async def _competitor_keyword_sets(
    competitor_urls: list[str], fetcher: Callable[[str], Awaitable[str]]
) -> list[tuple[str, list[str]]]:
    """Fetch capped competitors and extract their keyword sets; dead ones are skipped."""
    from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe_resolved
    from src.domains.critique.services.scanners.seo.keyword import top_keywords

    async def _fetch_competitor(comp_url: str) -> tuple[str, list[str]] | None:
        # competitor_urls are caller-supplied — never fetch an SSRF-unsafe host.
        # Resolving: a "competitor" is a bare tenant-supplied hostname, so
        # pointing its A record at the estate is the cheapest attack there is.
        if not comp_url or not await _is_ssrf_safe_resolved(comp_url):
            logger.warning("content-gap: skipping SSRF-unsafe competitor URL: %s", comp_url)
            return None
        try:
            comp_html = await fetcher(comp_url)
        except Exception as exc:  # noqa: BLE001 - a dead competitor is skipped, never fatal
            logger.warning("content-gap: competitor fetch failed for %s: %s", comp_url, exc)
            return None
        if not comp_html or not comp_html.strip():
            return None
        comp_soup = parse_soup(comp_html)
        if comp_soup is None:
            return None
        comp_kw = await asyncio.to_thread(top_keywords, comp_soup)
        return (comp_url, comp_kw) if comp_kw else None

    fetched = await asyncio.gather(
        *(_fetch_competitor(u) for u in competitor_urls[:_MAX_COMPETITORS]),
        return_exceptions=True,
    )
    return [item for item in fetched if isinstance(item, tuple)]


def _content_gap_annotation(gaps: list[dict], competitors: list[str]) -> SeoLlmAnnotation:
    topics = [gap["topic"] for gap in gaps]
    if not topics:
        title, summary = (
            "Content gaps vs competitors: none found",
            ("The analysed competitor pages cover no topic this page misses."),
        )
    else:
        title = f"Content gaps vs competitors: {len(topics)} missing topic(s)"
        summary = "Competitor pages cover topics this page does not: " + "; ".join(topics) + "."
    return {
        "kind": "content_gap",
        "title": title,
        "summary": summary,
        "data": {"gaps": gaps, "competitors": competitors},
    }


async def _run_content_gap(
    *,
    soup: BeautifulSoup,
    competitor_urls: list[str],
    fetcher: Callable[[str], Awaitable[str]],
    wm_client: WMClient,
    model_override: str | None,
) -> SeoLlmOutcome:
    """Competitor content-gap analysis as one reported outcome (layer ``content_gap``).

    Score-neutral (an annotation, never a finding). Fully fail-soft: every path
    returns an outcome naming what happened instead of a silent ``[]``.
    """
    from bs4 import Tag

    from src.domains.critique.services.scanners.seo.keyword import top_keywords

    worker = SeoContentGapAnalyzer.WORKER_NAME
    try:
        target_kw = await asyncio.to_thread(top_keywords, soup)
        if not target_kw:
            return SeoLlmOutcome(
                "content_gap", worker, "skipped", "target page too thin to extract keywords"
            )
        comp_sets = await _competitor_keyword_sets(competitor_urls, fetcher)
        if not comp_sets:
            return SeoLlmOutcome(
                "content_gap",
                worker,
                "skipped",
                "no competitor page could be fetched and analysed",
                by_design=False,
            )
        title_tag = soup.find("title")
        title = title_tag.get_text(" ", strip=True) if isinstance(title_tag, Tag) else ""
        gaps = await SeoContentGapAnalyzer(wm_client, model_override).analyze(
            target_kw, comp_sets, title
        )
    except Exception as e:  # noqa: BLE001 - content-gap is optional, never fatal
        logger.warning("SEO content-gap analysis failed, continuing: %s", e)
        return SeoLlmOutcome("content_gap", worker, "failed", error=e)
    if gaps is None:
        return SeoLlmOutcome("content_gap", worker, "empty", "the model returned no valid gaps")
    annotation = _content_gap_annotation(gaps["gaps"], [u for u, _ in comp_sets])
    return SeoLlmOutcome("content_gap", worker, "ok", annotation=annotation)


def _record_llm_outcome(ai: AiStatusCollector, outcome: SeoLlmOutcome) -> None:
    """Report one analyzer outcome into the AI-status collector (wire: ``ai``)."""
    layer, worker = outcome.layer, outcome.worker
    if outcome.status == "ok":
        ai.ok(layer, worker, outcome.reason)
        if outcome.annotation is not None:
            note = outcome.annotation
            ai.annotate(
                layer,
                note["kind"],
                title=note["title"],
                summary=note["summary"],
                data=dict(note["data"]),
            )
    elif outcome.status == "failed":
        ai.failed(layer, worker, outcome.error or outcome.reason or "failed")
    elif outcome.status == "empty":
        ai.empty(layer, worker, outcome.reason or "the model returned nothing")
    else:
        ai.skipped(layer, worker, outcome.reason or "skipped", by_design=outcome.by_design)


async def _run_llm_layers(
    *,
    soup: BeautifulSoup | None,
    include_llm: bool,
    wm_client: WMClient | None,
    model_override: str | None,
    competitor_urls: list[str] | None,
    fetcher: Callable[[str], Awaitable[str]],
    ai: AiStatusCollector,
) -> bool:
    """Run the opt-in SEO analyzers and REPORT each into ``ai``; return ``llm_enriched``.

    Outputs land in ``ai.annotations[]`` — never in the scored/bucketed findings
    (design SEO-G7), so the deterministic score cannot move. ``include_llm``
    without a WM client is recorded as unavailable, never a silent skip (F9).
    """
    if not include_llm:
        return False
    if wm_client is None or soup is None:
        workers: list[tuple[AiLayerName, str]] = [
            ("seo_intent", SeoIntentClassifier.WORKER_NAME),
            ("seo_eeat", SeoEeatDepthAnalyzer.WORKER_NAME),
        ]
        if competitor_urls:
            workers.append(("content_gap", SeoContentGapAnalyzer.WORKER_NAME))
        for layer, worker in workers:
            if wm_client is None:
                ai.wm_unavailable(layer, worker)
            else:
                ai.skipped(layer, worker, "page could not be parsed", by_design=False)
        return False

    outcomes = await enrich_seo_audit(soup, wm_client=wm_client, model_override=model_override)
    if competitor_urls:
        outcomes.append(
            await _run_content_gap(
                soup=soup,
                competitor_urls=competitor_urls,
                fetcher=fetcher,
                wm_client=wm_client,
                model_override=model_override,
            )
        )
    for outcome in outcomes:
        _record_llm_outcome(ai, outcome)
    return any(o.status == "ok" and o.annotation is not None for o in outcomes)


class GroupSummaryRow(TypedDict):
    """One ``groups`` rollup row — the dict shape behind :class:`SeoAuditGroupSummary`.

    ``measured`` / ``note`` are present ONLY on an unmeasured group, so a measured
    row stays byte-identical to what ``summarize_buckets`` has always returned.
    """

    group: str
    score: float | None
    weight: float
    count: int
    top_severity: str
    measured: NotRequired[bool]
    note: NotRequired[str]


def _summarize_groups(
    findings: list[dict],
    group_scores: dict[str, float | None],
    unmeasured: set[str],
    notes: dict[str, str] | None = None,
) -> list[GroupSummaryRow]:
    """Per-group rollup where an UNMEASURED group is not dressed up as a clean one.

    ``summarize_buckets`` is shared with cro/marketing and defaults a missing
    bucket to ``100.0``, so it is fed only the MEASURED scores; the unmeasured rows
    are then overwritten with ``score=None`` plus ``measured=False`` and a note.
    A consumer reading only ``score`` therefore gets ``null``, not a fake 100.
    """
    # Rebuilt key-by-key (same insertion order, same values) rather than cast, so
    # the declared row shape is checked rather than asserted.
    rows: list[GroupSummaryRow] = [
        {
            "group": raw["group"],
            "score": raw["score"],
            "weight": raw["weight"],
            "count": raw["count"],
            "top_severity": raw["top_severity"],
        }
        for raw in summarize_buckets(
            findings,
            {g: s for g, s in group_scores.items() if s is not None},
            weights=GROUP_WEIGHTS,
            bucket_of=group_of,
            bucket_key="group",
        )
    ]
    for row in rows:
        if row["group"] in unmeasured:
            row["score"] = None
            row["measured"] = False
            row["note"] = (notes or {}).get(row["group"]) or _unmeasured_note(row["group"])
    # SEO-G1 (owner decision on Lighthouse pending): performance is REPORTED as a
    # group so it can never render as a clean 100, but it carries weight 0 and is
    # NOT in GROUP_WEIGHTS — overall_score and coverage are untouched.
    rows.append(
        {
            "group": PERFORMANCE_GROUP,
            "score": None,
            "weight": 0.0,
            "count": 0,
            "top_severity": "info",
            "measured": False,
            "note": PERFORMANCE_NOT_MEASURED,
        }
    )
    return rows


def _live_not_run_reason(check_id: str, live: LiveObservation) -> str | None:
    """Why a live check did not run although a URL was given (``None`` = it ran)."""
    probe_name, part_name = LIVE_CHECK_PROBES[check_id]
    probe = live_probe(live, probe_name)
    part = probe_part(probe, part_name)
    if part.get("status") is not None:
        return None
    detail = part.get("error") or probe.get("error") or probe.get("reason")
    return f"live check did not complete: {detail}" if detail else "live check did not complete"


def _build_seo_checks(
    findings: list[dict],
    *,
    url: str | None,
    live: LiveObservation | None,
    cross_page_linking: bool,
    unmeasured: set[str],
    notes: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """The check inventory (pass / fail / not_run) over :data:`SEO_CHECK_CATALOG`.

    "Could not check" markers (``seo-*-errored``) are not checks: they are kept
    out of the inventory and turn their parent check ``not_run``. A live check
    whose probe got no HTTP response is ``not_run``, never ``pass``.
    """
    available = {"url", "page_url"} if url else set()
    if url and cross_page_linking and "internal_linking" not in unmeasured:
        available.add("crawl")
    crawl_reason = (
        "site-wide linking not measured (cross-page crawl off)"
        if url
        else "no URL — inline HTML cannot be crawled"
    )
    rows = build_checks(
        SEO_CHECK_CATALOG,
        [f for f in findings if f.get("check_id") not in STATUS_MARKER_IDS],
        bucket_key="group",
        available=available,
        not_run_reasons={
            "url": NO_URL_REASON,
            "page_url": "no URL — inline HTML has no page address to check",
            "crawl": crawl_reason,
        },
        not_run_buckets={g: (notes or {}).get(g) or _unmeasured_note(g) for g in unmeasured},
        bucket_of=group_of,
    )
    if url and live is not None:
        for row in rows:
            check_id = str(row["check_id"])
            if row["status"] == "pass" and check_id in LIVE_CHECK_PROBES:
                reason = _live_not_run_reason(check_id, live)
                if reason is not None:
                    row["status"] = "not_run"
                    row["reason"] = reason
    return rows


async def _load_html(
    html: str | None, url: str | None, fetcher: Callable[[str], Awaitable[str]]
) -> str:
    """The page source: inline ``html`` or a fetch of ``url``; fail-closed."""
    if html is None:
        if url is None:
            raise SeoAuditError("no page source provided")
        try:
            html = await fetcher(url)
        except Exception as e:
            raise SeoAuditError(f"Failed to fetch URL: {e}") from e
    if not html or not html.strip():
        raise SeoAuditError("empty page content")
    return html


async def _scan_page(html: str, url: str | None) -> tuple[BeautifulSoup | None, list[dict]]:
    """Parse once and run the deterministic audit scanner; fail-closed."""
    try:
        soup = parse_soup(html)
        findings = await asyncio.to_thread(
            _SCANNER.scan_audit, html, url or "", soup=soup, include_llm=False
        )
    except Exception as e:
        raise SeoAuditError(f"Failed to scan page: {e}") from e
    return soup, findings


async def audit_seo(
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
    model_override: str | None = None,
    competitor_urls: list[str] | None = None,
    cross_page_linking: bool = False,
    max_pages: int = 20,
) -> dict[str, Any]:
    """Audit one page for SEO discoverability synchronously.

    Raises :class:`SeoAuditError` on failure. Exactly one of ``html`` / ``url``
    is expected (the schema enforces it). With ``include_llm`` the opt-in Tier-3
    analyzers (search intent, E-E-A-T depth, competitor content gap) add
    ``ai.annotations[]`` and report their status into ``ai`` — fail-soft, and
    never findings, so they cannot move the deterministic score.
    """
    fetcher = fetch_html or _default_fetch_html
    html = await _load_html(html, url, fetcher)
    soup, findings = await _scan_page(html, url)

    # Live discoverability signals (SEO-G2): robots.txt + sitemap.xml (technical)
    # and AI-crawler access + llms.txt (GEO). Only meaningful for a real URL, and
    # best-effort: a live-fetch failure never fails the audit, and its checks are
    # reported not_run rather than pass.
    live: LiveObservation | None = None
    if url:
        live_findings, live = await _run_live_checks(url)
        findings.extend(live_findings)

    # Cross-page internal-linking analysis (opt-in, network, page-capped). Off by
    # default — the fast single-fetch deterministic contract stays the norm. The
    # already-parsed home `soup` is passed read-only (never mutated) to skip a
    # re-fetch. Best-effort: a crawl failure must never fail the audit.
    unmeasured_notes: dict[str, str] = {}
    crawl_findings: list[dict] = []
    if url and cross_page_linking:
        crawl_findings, unmeasured_notes = await _run_cross_page_linking(url, soup, max_pages)
        findings.extend(crawl_findings)
    unmeasured: set[str] = set(unmeasured_notes)

    ai = AiStatusCollector(
        requested=ai_requested(
            include_llm=include_llm,
            include_recommendations=include_recommendations,
            include_grounding=include_grounding,
            include_exec_summary=include_exec_summary,
            include_critic=include_critic,
        )
    )
    # Opt-in Tier-3 SEO analyzers (search intent / E-E-A-T depth / competitor
    # content gap). Reported into `ai` (annotations + per-layer status), never
    # into the scored findings → score-neutral by construction.
    llm_enriched = await _run_llm_layers(
        soup=soup,
        include_llm=include_llm,
        wm_client=wm_client,
        model_override=model_override,
        competitor_urls=competitor_urls,
        fetcher=fetcher,
        ai=ai,
    )

    findings = deduplicate_findings(findings)
    score = compute_seo_audit_score(findings, unmeasured=unmeasured)
    quick_wins, strategic = action_buckets(
        findings, bucket_of=group_of, bucket_key="group", include_unbucketed=include_unbucketed
    )

    # Tier-1 deterministic enrichment (opt-in). apply_enrichment is a no-op that
    # returns the untouched refs + ``None`` when no flag is set → byte-identical.
    quick_wins, strategic, projection = apply_enrichment(
        quick_wins,
        strategic,
        findings,
        weights=GROUP_WEIGHTS,
        bucket_of=group_of,
        bucket_key="group",
        severity_weights=_get_severity_weights(),
        score_fn=lambda fs: compute_seo_audit_score(fs, unmeasured=unmeasured)["overall_score"],
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

    result: dict[str, object] = {
        "overall_score": score["overall_score"],
        "grade": score["grade"],
        "group_scores": score["group_scores"],
        "groups": _summarize_groups(findings, score["group_scores"], unmeasured, unmeasured_notes),
        "quick_wins": quick_wins,
        "strategic": strategic,
        "source": url or "inline-html",
        "llm_enriched": llm_enriched,
        # `performance` is always listed: it is never measured on this path (SEO-G1).
        "unmeasured_groups": sorted(unmeasured | {PERFORMANCE_GROUP}),
        "coverage": score.get("coverage", 1.0),
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
    # measured facts, AI status. Built LAST so rows carry writer/critic prose.
    result["findings"] = build_findings(
        findings,
        bucket_of=group_of,
        bucket_key="group",
        weights=GROUP_WEIGHTS,
        severity_weights=_get_severity_weights(),
        quick_wins=quick_wins,
        strategic=strategic,
    )
    result["checks"] = _build_seo_checks(
        findings,
        url=url,
        live=live,
        cross_page_linking=cross_page_linking,
        unmeasured=unmeasured,
        notes=unmeasured_notes,
    )
    result["facts"] = _build_facts(
        soup,
        url,
        live=live,
        crawl={
            "requested": bool(url and cross_page_linking),
            "ran": "internal_linking" not in unmeasured,
            "findings": crawl_findings,
            "note": unmeasured_notes.get("internal_linking"),
        },
    )
    result["ai"] = ai.to_dict()
    return result


def _build_facts(
    soup: BeautifulSoup | None,
    url: str | None,
    *,
    live: LiveObservation | None,
    crawl: CrawlObservation,
) -> SeoFacts:
    """facts{} — exception-safe end to end: a facts bug never fails the audit."""
    try:
        return build_seo_facts(soup, url or "", live=live, crawl=crawl)
    except Exception as e:  # noqa: BLE001 - facts are advisory, never fatal
        logger.warning("SEO facts extraction failed, returning none: %s", e)
        return {
            "measured": False,
            "reason": f"fact extraction failed: {type(e).__name__}",
            "performance": {"measured": False, "reason": PERFORMANCE_NOT_MEASURED},
        }
