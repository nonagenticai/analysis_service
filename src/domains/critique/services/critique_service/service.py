"""Top-level ``CritiqueService`` coordinator class."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.critique_service.finalize import CritiqueFinalizeMixin
from src.domains.critique.services.critique_service.helpers import CritiqueHelpersMixin
from src.domains.critique.services.critique_service.lifecycle import JobLifecycleMixin
from src.domains.critique.services.critique_service.phases import CritiquePhasesMixin
from src.domains.critique.services.critique_service.scoring_result import ALL_CATEGORIES
from src.domains.critique.services.scanners.a11y import A11yScanner
from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner
from src.domains.critique.services.scanners.cro import CROScanner
from src.domains.critique.services.scanners.perf import PerformanceScanner
from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.critique.services.scanners.seo import SEOScanner
from src.domains.critique.services.scanners.tool_adapters.registry import (
    AdapterStatus,
    ToolAdapterRegistry,
)
from src.domains.critique.services.scanners.ux_reviewer import UXReviewer, UXReviewerProtocol

logger = logging.getLogger(__name__)


class CritiqueService(
    JobLifecycleMixin,
    CritiquePhasesMixin,
    CritiqueFinalizeMixin,
    CritiqueHelpersMixin,
):
    """Orchestrates website critique scanning."""

    def __init__(
        self,
        *,
        adapter_registry: ToolAdapterRegistry | None = None,
    ) -> None:
        self._seo = SEOScanner()
        self._security = SecurityScanner()
        self._a11y = A11yScanner()
        self._performance = PerformanceScanner()
        self._ux: UXReviewerProtocol = UXReviewer()
        self._code_quality = CodeQualityScanner()
        self._cro = CROScanner()

        # External tool adapter registry — DI seam: callers (and tests) may
        # inject a registry; otherwise we build the default one from settings.
        # An explicit ``adapter_registry`` argument is honored as-is, including
        # ``None`` is *not* treated specially here — to disable adapters,
        # callers should pass an empty registry or rely on settings.
        if adapter_registry is not None:
            self._adapter_registry: ToolAdapterRegistry | None = adapter_registry
        else:
            self._adapter_registry = self._init_adapters()

    def set_adapter_registry(self, registry: ToolAdapterRegistry | None) -> None:
        """Replace the external-tool adapter registry.

        Public DI hook used by composition roots (and tests) to swap in a
        different registry without subclassing or poking private state.
        Pass ``None`` to disable external-tool adapters.
        """
        self._adapter_registry = registry

    def _init_adapters(self) -> ToolAdapterRegistry | None:
        """Initialize external tool adapter registry if enabled."""
        from src.domains.critique.services import critique_service as _pkg

        settings = _pkg.get_settings()
        if not settings.external_tools.enabled:
            return None

        registry = ToolAdapterRegistry()

        if settings.external_tools.axe_enabled:
            from src.domains.critique.services.scanners.tool_adapters.axe_adapter import AxeAdapter

            registry.register(AxeAdapter())

        if settings.external_tools.lighthouse_enabled:
            from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import (
                LighthouseAdapter,
            )

            registry.register(LighthouseAdapter())

        if settings.external_tools.zap_enabled:
            from src.domains.critique.services.scanners.tool_adapters.zap_adapter import ZapAdapter

            registry.register(ZapAdapter())

        if settings.external_tools.nuclei_enabled:
            from src.domains.critique.services.scanners.tool_adapters.nuclei_adapter import (
                NucleiAdapter,
            )

            registry.register(NucleiAdapter())

        if registry.adapter_names:
            logger.info("External tool adapters registered: %s", registry.adapter_names)

        return registry

    async def process_job(
        self,
        db: AsyncSession,
        job_id: str,
        tenant_id: str,
        *,
        force_rescan: bool = False,
        site_analysis: dict | None = None,
        llm_config: LLMConfigOverride | None = None,
        html_input: str | None = None,
        artifact_ref: str | None = None,
    ) -> None:
        """Process a pending critique job (run all scanners).

        Thin orchestrator. Phase methods are independently testable.

        When ``html_input`` or ``artifact_ref`` is supplied the job scores a
        pre-rendered artifact with no live origin (``is_static``): the live-only
        work (``seo_live``, live-security checks, external adapters) is skipped
        and the URL-keyed cache is bypassed. What was scored vs skipped is
        surfaced in ``job.progress`` (never silently dropped).

        Raises:
            ValueError: If the job is not found.
        """
        job = await self._get_job(db, job_id, tenant_id=tenant_id)
        if not job:
            raise ValueError(f"Job not found: {job_id}")

        is_static = html_input is not None or artifact_ref is not None

        if (
            not force_rescan
            and not is_static
            and await self._phase_check_cache(
                db,
                job,
                job_id,
                tenant_id,
                llm_config=llm_config,
                site_analysis=site_analysis,
            )
        ):
            return

        from src.domains.critique.services.metrics import cache_misses

        if not force_rescan and not is_static:
            cache_misses.inc()
        await self._phase_mark_running(db, job, job_id, tenant_id)

        start_time = time.monotonic()
        from src.domains.critique.services import critique_service as _pkg

        settings = _pkg.get_settings()

        try:
            html = await self._resolve_html(job, html_input=html_input, artifact_ref=artifact_ref)
            github_repo = self._phase_resolve_repo(job, html)

            soup = self._parse_soup(html)
            categories = set(job.categories) if job.categories else ALL_CATEGORIES

            (
                scanner_findings,
                scanner_timings,
                incomplete_scanners,
            ) = await self._phase_run_scanners(
                job_id=job_id,
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

            adapter_run = await self._phase_run_adapters(job, html, settings, is_static=is_static)
            all_findings = scanner_findings + adapter_run.findings
            # A failed/timed-out adapter measured NOTHING: its 0 findings must
            # not read as a clean result (live 2026-10-18: axe-core failed with
            # ``Page.evaluate: Event`` and the audit reported 0 a11y issues).
            unmeasured_adapters = adapter_run.unmeasured
            incomplete_scanners = [*incomplete_scanners, *unmeasured_adapters]

            await self._update_progress(job_id, status="scoring", scanner_timings=scanner_timings)

            scoring = await self._phase_score_and_summarize(
                db,
                job,
                tenant_id,
                all_findings,
                start_time=start_time,
                github_repo=github_repo,
                llm_config=llm_config,
                is_static=is_static,
                incomplete_scanners=incomplete_scanners,
                unmeasured_adapters={
                    name: adapter_run.statuses[name] for name in unmeasured_adapters
                },
            )

            await self._phase_finalize_completed(
                db,
                job,
                job_id,
                tenant_id,
                scoring=scoring,
                scanner_timings=scanner_timings,
                start_time=start_time,
                run_meta=self._build_run_meta(
                    categories=categories,
                    is_static=is_static,
                    html_input=html_input,
                    artifact_ref=artifact_ref,
                    adapter_statuses=adapter_run.statuses,
                    incomplete_scanners=incomplete_scanners,
                ),
                # An incomplete scan is not cached: the URL-keyed result cache
                # would re-serve the missing measurement for an hour, and the
                # cache entry does not carry ``progress`` to say so.
                cache_result=not is_static and not unmeasured_adapters,
                llm_config=llm_config,
                site_analysis=site_analysis,
            )

        except (OSError, ValueError, RuntimeError, TimeoutError) as e:
            await self._phase_finalize_failed(db, job, job_id, tenant_id, e, start_time=start_time)

        await db.commit()

    def _build_run_meta(
        self,
        *,
        categories: set[str],
        is_static: bool,
        html_input: str | None,
        artifact_ref: str | None,
        adapter_statuses: Mapping[str, AdapterStatus] | None = None,
        incomplete_scanners: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Structured record of the input mode + which categories were scored
        vs skipped-for-lack-of-a-live-origin. Surfaced via ``job.progress`` so
        static scoring never silently drops coverage.

        For URL input it also carries ``external_tools`` (one status per
        adapter: ``ok`` / ``failed`` / ``timeout`` / ``skipped`` + ``error``)
        and ``incomplete_scanners`` (every scanner/adapter that measured
        nothing), so a failed axe-core scan is visible as failed, not as 0
        violations."""
        if not is_static:
            meta: dict[str, Any] = {"input_mode": "url"}
            if adapter_statuses:
                meta["external_tools"] = {
                    name: status.as_dict() for name, status in sorted(adapter_statuses.items())
                }
            if incomplete_scanners:
                meta["incomplete_scanners"] = sorted(set(incomplete_scanners))
            return meta
        input_mode = "artifact" if artifact_ref else "html"
        skipped: dict[str, str] = {}
        if "seo" in categories:
            skipped["seo_live"] = "no_live_url"
        if "security" in categories:
            skipped["security_live"] = "no_live_url"
        if self._adapter_registry is not None:
            skipped["adapters"] = "no_live_url"
        return {
            "input_mode": input_mode,
            "scored_categories": sorted(categories),
            "skipped_categories": skipped,
        }
