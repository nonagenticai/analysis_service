"""Phase methods used by ``CritiqueService.process_job``."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from bs4 import BeautifulSoup

from src.domains.critique.models import CritiqueJob
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.metrics import (
    cache_hits,
    jobs_active,
    jobs_total,
)
from src.domains.critique.services.scanners.tool_adapters.registry import (
    AdapterRunResult,
    AdapterStatus,
)


def _pkg() -> Any:
    """Return the package module — used to resolve patchable names at call time."""
    from src.domains.critique.services import critique_service as _p

    return _p


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner
    from src.domains.critique.services.scanners.tool_adapters.registry import (
        ToolAdapterRegistry,
    )


class CritiquePhasesMixin:
    """Mixin providing the phase methods for ``CritiqueService.process_job``."""

    # Cross-mixin attributes initialized in ``CritiqueService`` (or by sibling mixins).
    _code_quality: CodeQualityScanner
    _adapter_registry: ToolAdapterRegistry | None
    # Sibling-mixin methods used here:
    _update_progress: Callable[..., Coroutine[Any, Any, None]]
    _run_scanner_with_timeout: Callable[
        ..., Coroutine[Any, Any, tuple[list[dict[str, Any]], float]]
    ]
    _run_seo: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_seo_live: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_a11y: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_cro: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_performance: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_security: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_security_static: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_ux: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]
    _run_code_quality: Callable[..., Coroutine[Any, Any, list[dict[str, Any]]]]

    async def _phase_check_cache(
        self,
        db: AsyncSession,
        job: CritiqueJob,
        job_id: str,
        tenant_id: str,
        *,
        llm_config: LLMConfigOverride | None = None,
        site_analysis: dict | None = None,
    ) -> bool:
        """Phase: short-circuit on cache hit. Returns True if cached path was taken.

        ``llm_config`` and ``site_analysis`` MUST reach the key: both steer the
        findings, so without them a request that selected a premium model was
        served — and billed for — an earlier default-model job's cached result.
        """
        from src.domains.critique.services.scan_cache import get_cached_result

        cached = await get_cached_result(
            job.tenant_id,
            job.url,
            job.categories,
            job.instructions,
            llm_config,
            site_analysis,
        )
        if not cached:
            return False
        cache_hits.inc()
        logger.info("Cache hit for job %s", job_id)
        jobs_total.inc(status="completed")
        await _pkg().log_event(
            db,
            event_type="job_cached",
            tenant_id=tenant_id,
            job_id=job_id,
            details={"url": job.url},
        )
        job.status = "completed"
        job.findings = cached.get("findings", [])
        job.category_scores = cached.get("category_scores", {})
        job.overall_score = cached.get("overall_score")
        job.confidence_scores = cached.get("confidence_scores")
        job.summary = cached.get("summary", "")
        job.report_markdown = cached.get("report_markdown", "")
        job.scan_duration = 0.0
        job.pages_scanned = cached.get("pages_scanned", 1)
        job.completed_at = datetime.now(UTC)
        await db.commit()
        return True

    async def _phase_mark_running(
        self, db: AsyncSession, job: CritiqueJob, job_id: str, tenant_id: str
    ) -> None:
        """Phase: transition job into running state."""
        job.status = "running"
        job.started_at = datetime.now(UTC)
        jobs_active.inc()
        await _pkg().log_event(
            db,
            event_type="job_started",
            tenant_id=tenant_id,
            job_id=job_id,
            details={"url": job.url},
        )
        await db.commit()

    @staticmethod
    def _phase_resolve_repo(job: CritiqueJob, html: str) -> str | None:
        """Phase: resolve github repo, auto-detecting from HTML if necessary."""
        github_repo = job.github_repo
        if not github_repo and html:
            detection = _pkg().detect_repo_url(html)
            if detection.url and detection.confidence in ("high", "medium"):
                github_repo = detection.url
                job.github_repo = github_repo
        return github_repo

    @staticmethod
    def _parse_soup(html: str) -> BeautifulSoup | None:
        """Parse HTML to BeautifulSoup once for all scanners; None if no HTML."""
        if not html:
            return None
        from bs4 import BeautifulSoup as _BeautifulSoup

        return _BeautifulSoup(html, "lxml")

    def _build_scan_tasks(
        self,
        *,
        job: CritiqueJob,
        html: str,
        soup: BeautifulSoup | None,
        categories: set[str],
        github_repo: str | None,
        settings: Any,
        site_analysis: dict | None,
        llm_config: LLMConfigOverride | None,
        is_static: bool = False,
    ) -> list[tuple[str, Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]], int]]:
        """Build the list of (name, factory, timeout) for scanners to run.

        When ``is_static`` (scoring a pre-rendered artifact with no live origin)
        the live-only tasks are omitted: ``seo_live`` (fetches origin
        robots/sitemap/llms.txt) is dropped, and ``security`` runs its HTML-only
        subset via ``_run_security_static`` instead of the live scan that opens
        its own request to headers/TLS/CORS/exposed-files.
        """
        scan_tasks: list[
            tuple[str, Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]], int]
        ] = []
        if "seo" in categories:
            scan_tasks.append(
                ("seo", lambda: self._run_seo(html, job.url, soup), settings.scanners.seo_timeout)
            )
        if "accessibility" in categories:
            scan_tasks.append(
                (
                    "a11y",
                    lambda: self._run_a11y(html, job.url, soup),
                    settings.scanners.a11y_timeout,
                )
            )
        if "performance" in categories:
            scan_tasks.append(
                (
                    "perf",
                    lambda: self._run_performance(html, job.url, soup),
                    settings.scanners.performance_timeout,
                )
            )
        if "security" in categories:
            if is_static:
                scan_tasks.append(
                    (
                        "sec",
                        lambda: self._run_security_static(
                            html, soup, job.url, site_analysis=site_analysis
                        ),
                        settings.scanners.security_timeout,
                    )
                )
            else:
                scan_tasks.append(
                    (
                        "sec",
                        lambda: self._run_security(
                            job.url, html, soup, site_analysis=site_analysis
                        ),
                        settings.scanners.security_timeout,
                    )
                )
        if "ux" in categories:
            scan_tasks.append(
                (
                    "ux",
                    lambda: self._run_ux(
                        html,
                        job.url,
                        job.instructions,
                        site_analysis=site_analysis,
                        llm_config=llm_config,
                        soup=soup,
                    ),
                    settings.scanners.ux_timeout,
                )
            )
        if "seo" in categories and not is_static:
            scan_tasks.append(
                ("seo_live", lambda: self._run_seo_live(job.url), settings.scanners.seo_timeout)
            )
        if "cro" in categories:
            scan_tasks.append(
                ("cro", lambda: self._run_cro(html, job.url, soup), settings.scanners.cro_timeout)
            )
        if "code_quality" in categories and github_repo and self._code_quality.is_configured:
            scan_tasks.append(
                (
                    "code_quality",
                    lambda: self._run_code_quality(github_repo),
                    settings.scanners.code_quality_timeout,
                )
            )
        return scan_tasks

    async def _phase_run_scanners(
        self,
        *,
        job_id: str,
        job: CritiqueJob,
        html: str,
        soup: BeautifulSoup | None,
        categories: set[str],
        github_repo: str | None,
        settings: Any,
        site_analysis: dict | None,
        llm_config: LLMConfigOverride | None,
        is_static: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, float], list[str]]:
        """Phase: run all configured scanners (with per-scanner caching).

        Returns ``(findings, timings, incomplete_scanners)``. ``incomplete_scanners``
        names every scanner that raised or returned an unusable outcome and so
        contributed ZERO findings. It is what makes "the page is clean" and "we
        measured nothing" distinguishable downstream: scoring an empty finding
        set as a perfect 100 is only defensible when that list is empty.

        For ``is_static`` input the URL-keyed per-scanner cache is bypassed
        (read and write) — the job's synthetic ``rendered://`` URL is unique per
        job so it could only ever produce dead keys.
        """
        all_findings: list[dict[str, Any]] = []
        scanner_timings: dict[str, float] = {}
        incomplete_scanners: list[str] = []

        if not html:
            # No HTML at all: nothing was measured. Name every requested category
            # so the empty finding set cannot be mistaken for a clean page.
            return all_findings, scanner_timings, sorted(categories)

        from src.domains.critique.services.scan_cache import (
            get_scanner_cached_result,
            set_scanner_cached_result,
        )

        scan_tasks = self._build_scan_tasks(
            job=job,
            html=html,
            soup=soup,
            categories=categories,
            github_repo=github_repo,
            settings=settings,
            site_analysis=site_analysis,
            llm_config=llm_config,
            is_static=is_static,
        )

        if is_static:
            cached_findings, tasks_to_run = [], scan_tasks
        else:
            cached_findings, tasks_to_run = await self._partition_scanner_cache(
                scan_tasks,
                job,
                get_scanner_cached_result,
                llm_config=llm_config,
                site_analysis=site_analysis,
            )
        all_findings.extend(cached_findings)

        total_scanners = len(scan_tasks)
        completed_scanners = total_scanners - len(tasks_to_run)
        await self._update_progress(
            job_id,
            status="scanning",
            completed=completed_scanners,
            total=total_scanners,
            scanners=[name for name, _, _ in scan_tasks],
        )

        if not tasks_to_run:
            return all_findings, scanner_timings, incomplete_scanners

        results = await asyncio.gather(
            *[
                self._run_scanner_with_timeout(name, factory, timeout)
                for name, factory, timeout in tasks_to_run
            ],
            return_exceptions=True,
        )

        for (name, _, _), result in zip(tasks_to_run, results, strict=False):
            if isinstance(result, BaseException):
                # Cancellation is not a scanner failure — it must propagate
                # untouched rather than be recorded as one scanner's error while
                # the job goes on to score over the survivors.
                if isinstance(result, asyncio.CancelledError):
                    raise result
                # Do NOT increment `completed_scanners`. A scanner that RAISED
                # contributed zero findings, exactly like the unusable-outcome
                # branch below — and that branch already refuses to count itself,
                # because a full `completed == total` on a short finding set is
                # indistinguishable from a clean scan to every progress consumer.
                # Counting a crash as completed does the thing that comment
                # forbids. The job still reaches `status="completed"` in
                # `finalize`, so nothing hangs on the shortfall.
                # `exc_info=result`: gather(return_exceptions=True) hands the
                # exception back as a VALUE, so we are NOT inside an `except`
                # block — `logger.exception()` here would log "NoneType: None"
                # and the real failure would land with no traceback at all.
                logger.error("Scanner %s failed: %s", name, result, exc_info=result)
                incomplete_scanners.append(name)
            elif isinstance(result, tuple):
                completed_scanners += 1
                findings_list, elapsed = result
                scanner_timings[name] = elapsed
                all_findings.extend(findings_list)
                if not is_static:
                    await set_scanner_cached_result(
                        job.tenant_id,
                        job.url,
                        name,
                        findings_list,
                        job.instructions,
                        llm_config,
                        site_analysis,
                    )
                logger.info(
                    "Scanner %s completed: %d findings in %.2fs",
                    name,
                    len(findings_list),
                    elapsed,
                )
            else:
                # Neither a valid (findings, elapsed) tuple nor an exception.
                # This branch previously did not exist: the scanner still counted
                # as COMPLETED, contributed ZERO findings, and the job finalized
                # and SCORED over a silently short finding set — reported clean.
                # Do NOT increment `completed_scanners`, so the shortfall is
                # visible in progress instead of being papered over.
                logger.error(
                    "Scanner %s returned an unusable outcome (%s), contributing no "
                    "findings: the scan is INCOMPLETE and must not be scored as clean",
                    name,
                    type(result).__name__,
                )
                incomplete_scanners.append(name)
            await self._update_progress(
                job_id,
                status="scanning",
                completed=completed_scanners,
                total=total_scanners,
                current_scanner=name,
            )

        return all_findings, scanner_timings, incomplete_scanners

    @staticmethod
    async def _partition_scanner_cache(
        scan_tasks: Sequence[
            tuple[str, Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]], int]
        ],
        job: CritiqueJob,
        get_scanner_cached_result: Callable[..., Coroutine[Any, Any, list[dict[str, Any]] | None]],
        *,
        llm_config: LLMConfigOverride | None = None,
        site_analysis: dict | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[tuple[str, Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]], int]],
    ]:
        """Split scan_tasks into (cached_findings, tasks_still_to_run).

        The per-scanner READ must be keyed on the same steering inputs as the
        write, or a premium-model run reuses the default model's findings.
        """
        cached_findings: list[dict[str, Any]] = []
        tasks_to_run: list[
            tuple[str, Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]], int]
        ] = []
        for name, factory, timeout in scan_tasks:
            scanner_cached = await get_scanner_cached_result(
                job.tenant_id, job.url, name, job.instructions, llm_config, site_analysis
            )
            if scanner_cached is not None:
                # Regenerate finding_ids so they don't collide with rows
                # already persisted by a previous job for the same URL.
                for f in scanner_cached:
                    f["finding_id"] = str(uuid.uuid7())
                cached_findings.extend(scanner_cached)
                logger.info("Scanner cache hit: scanner=%s", name)
            else:
                tasks_to_run.append((name, factory, timeout))
        return cached_findings, tasks_to_run

    async def _phase_run_adapters(
        self, job: CritiqueJob, html: str, settings: Any, *, is_static: bool = False
    ) -> AdapterRunResult:
        """Phase: run external tool adapters (axe, lighthouse, zap, nuclei) if enabled.

        Skipped entirely for ``is_static`` input — every adapter drives a live
        headless browser / scanner against ``job.url``, which does not exist for
        a pre-rendered artifact.

        Returns findings AND per-adapter statuses. A failed adapter's 0 findings
        are not "clean": the caller marks it incomplete and surfaces the status
        (``progress.external_tools``) instead of scoring it as a pass.
        """
        if is_static or self._adapter_registry is None:
            return AdapterRunResult()
        try:
            run = await self._adapter_registry.run_adapters_with_status(
                job.url, html, timeout=settings.scanners.overall_timeout
            )
            if run.findings:
                logger.info("External tools produced %d findings", len(run.findings))
            return run
        except (TimeoutError, ConnectionError, httpx.HTTPError) as e:
            logger.error("External tool adapters failed (retryable): %s", e)
            reason = f"{type(e).__name__}: {e}"
        except (ValueError, RuntimeError) as e:
            logger.warning("External tool adapters failed (unexpected): %s", e, exc_info=True)
            reason = f"{type(e).__name__}: {e}"
        # The whole adapter phase blew up: every registered adapter is unmeasured.
        return AdapterRunResult(
            statuses={
                name: AdapterStatus(status="failed", category="unknown", error=reason[:300])
                for name in self._adapter_registry.adapter_names
            }
        )
