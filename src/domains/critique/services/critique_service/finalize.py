"""Score / summarize / finalize phases for ``CritiqueService.process_job``."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.models import CritiqueJob
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.critique_service.scoring_result import (
    _background_tasks,
    _ScoringResult,
)
from src.domains.critique.services.metrics import (
    findings_total,
    jobs_active,
    jobs_total,
)
from src.domains.critique.services.metrics import (
    scan_duration as scan_duration_metric,
)
from src.domains.critique.services.report_generator import (
    compute_confidence_scores,
    compute_overall_score,
    compute_scores,
    deduplicate_findings,
    generate_markdown_report,
    overall_score_for_complete_scan,
)
from src.domains.critique.services.scanners.tool_adapters.registry import AdapterStatus

logger = logging.getLogger(__name__)


def _pkg() -> Any:
    """Return the package module — used to resolve patchable names at call time."""
    from src.domains.critique.services import critique_service as _p

    return _p


class CritiqueFinalizeMixin:
    """Mixin: scoring + summary + completion / failure finalization phases."""

    # Cross-mixin methods/attributes initialized in ``CritiqueService``.
    _ux: Any
    _apply_suppressions: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _save_findings: Callable[..., Coroutine[Any, Any, None]]
    _record_usage: Callable[..., Coroutine[Any, Any, None]]
    _deliver_webhook: Callable[..., Coroutine[Any, Any, None]]
    _update_progress: Callable[..., Coroutine[Any, Any, None]]

    async def _phase_score_and_summarize(
        self,
        db: AsyncSession,
        job: CritiqueJob,
        tenant_id: str,
        all_findings: list[dict[str, Any]],
        *,
        start_time: float,
        github_repo: str | None,
        llm_config: LLMConfigOverride | None,
        is_static: bool = False,
        incomplete_scanners: list[str] | None = None,
        unmeasured_adapters: Mapping[str, AdapterStatus] | None = None,
    ) -> _ScoringResult:
        """Phase: dedupe, suppress, score, summarize, build report.

        ``incomplete_scanners`` names the scanners that measured nothing. It
        decides how an EMPTY score set is scored -- see below. It defaults to
        ``None`` meaning "completeness unknown", which is treated as incomplete:
        a caller that does not pass it gets the conservative answer rather than a
        silent perfect score.

        ``unmeasured_adapters`` (external tools that failed / timed out / were
        skipped) is disclosed in the report, so "0 accessibility violations"
        cannot be read off a scan that never ran.
        """
        findings = deduplicate_findings(all_findings)
        findings = await self._apply_suppressions(
            db, findings, tenant_id=tenant_id, domain=job.domain
        )
        active_findings = [f for f in findings if not f.get("is_suppressed")]

        # The suppression lookup above leaves a read transaction open. Release
        # the connection before the summary LLM call so we don't sit
        # idle-in-transaction across it — the same reap-the-backend hazard that
        # bit the redesign generation path. Scoring/report building below are
        # in-memory; the next DB write (finalize) checks out a fresh connection.
        await db.commit()

        scores = compute_scores(active_findings)
        # A CLEAN page must not be delivered as 0.0/grade F.
        #
        # ``compute_scores`` derives its categories FROM the findings, so an
        # empty ``scores`` is produced both by a flawless page (and by a
        # category-scoped suppression that removes the last finding) and by a
        # run where every scanner crashed. ``compute_overall_score({})`` is
        # 0.0, so today BOTH ship the worst possible score -- persisted,
        # cached for an hour, and webhooked.
        #
        # ``incomplete_scanners`` is what separates the two. When it is empty
        # every scanner ran and found nothing, which is exactly the condition
        # ``compute_category_score`` already scores as 100.0; match it via the
        # shared helper the artifact scorer uses. When it is NOT empty the
        # score is unmeasured, and we must NOT fail open to 100.0 -- keep the
        # conservative 0.0 and say so loudly.
        # ``None`` means the caller did not establish completeness at all, which
        # is treated exactly like a known-incomplete scan: unknown never buys a
        # perfect score.
        scan_was_complete = incomplete_scanners is not None and not incomplete_scanners
        if scores or scan_was_complete:
            overall_score = overall_score_for_complete_scan(scores)
        else:
            overall_score = compute_overall_score(scores)
            logger.error(
                "Job %s scored with NO findings, but the scan is INCOMPLETE (%s), "
                "so it is scored %.1f rather than as a clean page.",
                job.id,
                ", ".join(incomplete_scanners)
                if incomplete_scanners
                else "completeness not reported",
                overall_score,
            )
        confidence = compute_confidence_scores(
            active_findings, pages_scanned=1, max_pages=job.max_pages
        )
        summary, summary_token_usage = await _pkg().generate_summary(
            active_findings, job.url, scores, llm_config=llm_config
        )

        token_usage = self._aggregate_token_usage(summary_token_usage)

        report = generate_markdown_report(
            url=job.url,
            findings=findings,
            scores=scores,
            summary=summary,
            scan_duration=time.monotonic() - start_time,
            github_repo=github_repo,
        )
        if unmeasured_adapters:
            report += _unmeasured_adapters_note(unmeasured_adapters)
        if is_static:
            report += (
                "\n\n> _Note: scored from a pre-rendered artifact with no live "
                "origin. Live-only checks (seo_live, live-security headers/TLS/"
                "network, and external adapters) were skipped — see the job's "
                "`progress.skipped_categories`._\n"
            )
        return _ScoringResult(
            findings=findings,
            scores=scores,
            overall_score=overall_score,
            confidence=confidence,
            summary=summary,
            report=report,
            token_usage=token_usage,
        )

    def _aggregate_token_usage(
        self, summary_token_usage: dict[str, int] | None
    ) -> dict[str, int] | None:
        """Combine UX + summary token usage into a single dict."""
        accum: dict[str, int] = {}
        ux_token_usage = self._ux._last_token_usage
        for tu in (ux_token_usage, summary_token_usage):
            if tu:
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    accum[k] = accum.get(k, 0) + tu.get(k, 0)
        return accum or None

    async def _phase_finalize_completed(
        self,
        db: AsyncSession,
        job: CritiqueJob,
        job_id: str,
        tenant_id: str,
        *,
        scoring: _ScoringResult,
        scanner_timings: dict[str, float],
        start_time: float,
        run_meta: dict[str, Any] | None = None,
        cache_result: bool = True,
        llm_config: LLMConfigOverride | None = None,
        site_analysis: dict | None = None,
    ) -> None:
        """Phase: persist completion state, metrics, cache result, dispatch webhook.

        ``run_meta`` (input_mode / scored_categories / skipped_categories) is
        merged into ``job.progress`` so a static scoring's reduced coverage is
        surfaced, never silent. ``cache_result=False`` (static input) skips the
        URL-keyed result cache write.
        """
        from src.domains.critique.services.scan_cache import set_cached_result

        job.status = "completed"
        job.findings = scoring.findings
        job.category_scores = scoring.scores
        job.overall_score = scoring.overall_score
        job.confidence_scores = scoring.confidence
        job.summary = scoring.summary
        job.report_markdown = scoring.report
        scan_duration_value = round(time.monotonic() - start_time, 2)
        job.scan_duration = scan_duration_value
        job.pages_scanned = 1
        job.completed_at = datetime.now(UTC)

        jobs_active.dec()
        jobs_total.inc(status="completed")
        scan_duration_metric.observe(scan_duration_value)
        findings_total.inc(len(scoring.findings), category="all")

        if scanner_timings or scoring.token_usage or run_meta:
            progress_data: dict[str, object] = {"scanner_timings": scanner_timings}
            if scoring.token_usage:
                progress_data["token_usage"] = scoring.token_usage
            if run_meta:
                progress_data.update(run_meta)
            job.progress = progress_data

        # Stamp findings with the PARENT job's tenant (not the phase argument) —
        # the job row is the authority for ownership under the findings RLS policy.
        await self._save_findings(db, job.job_id, scoring.findings, tenant_id=job.tenant_id)

        await self._record_usage(
            db,
            tenant_id=tenant_id,
            pages_scanned=1,
            findings_count=len(scoring.findings),
            is_error=False,
        )

        if cache_result:
            await set_cached_result(
                job.tenant_id,
                job.url,
                job.categories,
                {
                    "findings": scoring.findings,
                    "category_scores": scoring.scores,
                    "overall_score": scoring.overall_score,
                    "confidence_scores": scoring.confidence,
                    "summary": scoring.summary,
                    "report_markdown": scoring.report,
                    "pages_scanned": 1,
                },
                instructions=job.instructions,
                # The WRITE must carry the same steering inputs as the read in
                # `_phase_check_cache`, or this entry is stored under a
                # steering-agnostic key and later served to a request that
                # selected a different (premium) model and is billed for it.
                llm_config=llm_config,
                site_analysis=site_analysis,
            )

        if job.callback_url:
            task = asyncio.create_task(self._deliver_webhook(job))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        await self._update_progress(job_id, status="completed")

        await _pkg().log_event(
            db,
            event_type="job_completed",
            tenant_id=tenant_id,
            job_id=job_id,
            details={
                "findings_count": len(scoring.findings),
                "overall_score": scoring.overall_score,
                "scan_duration": job.scan_duration,
            },
        )

    async def _phase_finalize_failed(
        self,
        db: AsyncSession,
        job: CritiqueJob,
        job_id: str,
        tenant_id: str,
        exc: BaseException,
        *,
        start_time: float,
    ) -> None:
        """Phase: persist failure state and metrics."""
        logger.error("Critique job %s failed: %s", job_id, exc, exc_info=True)
        job.status = "failed"
        job.error_message = str(exc)
        job.completed_at = datetime.now(UTC)
        scan_duration_value = round(time.monotonic() - start_time, 2)
        job.scan_duration = scan_duration_value
        jobs_active.dec()
        jobs_total.inc(status="failed")
        scan_duration_metric.observe(scan_duration_value)

        await self._record_usage(
            db, tenant_id=tenant_id, pages_scanned=0, findings_count=0, is_error=True
        )
        await _pkg().log_event(
            db,
            event_type="job_failed",
            tenant_id=tenant_id,
            job_id=job_id,
            details={"error": str(exc)},
        )


def _unmeasured_adapters_note(unmeasured: Mapping[str, AdapterStatus]) -> str:
    """Report footnote naming each external tool that measured nothing, and why."""
    lines = [
        f"- `{name}` ({status.category}): **{status.status}**"
        + (f" — {status.error}" if status.error else "")
        for name, status in sorted(unmeasured.items())
    ]
    return (
        "\n\n> _Note: these external tools did NOT complete, so their categories "
        "are only partially measured — a missing finding from them is not a pass "
        "(see the job's `progress.external_tools`):_\n>\n"
        + "\n".join(f"> {line}" for line in lines)
        + "\n"
    )
