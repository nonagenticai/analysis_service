"""Critique routes package.

Composes the critique APIRouter from focused submodules and re-exports the
public symbols (route handlers, helpers, background tasks) at the package
level so that ``from src.domains.critique.routes import X`` continues to work
for tests and other code that originally imported from the monolithic
``routes.py`` module.

Sub-routers are registered in the same sequence as the original monolithic
declaration order so that FastAPI's first-match-wins behavior for overlapping
paths (e.g. ``/critique/compare`` vs ``/critique/{job_id}``) is preserved
bit-identically.
"""

from __future__ import annotations

import asyncio  # re-exported for tests that patch ``critique.routes.asyncio``
import logging

from fastapi import APIRouter
from w2v_common.auth import LICENSE_GATE

from . import (
    analytics,
    batch,
    compare,
    exports,
    findings,
    jobs,
    schedules,
    scoring,
    suppressions,
)
from ._helpers import (
    _background_tasks,
    _get_critique_service,
    _get_tenant_id,
    _process_job_background,
)
from .analytics import (
    get_analytics_trends,
    get_domain_analytics,
    get_tenant_summary,
    get_top_findings,
)
from .batch import create_batch_critique, get_batch_status
from .compare import compare_critiques, compare_history
from .exports import (
    export_csv,
    export_sarif,
    get_critique_report,
    get_critique_report_pdf,
)
from .findings import acknowledge_finding, get_critique_findings, get_unified_findings
from .jobs import (
    create_critique,
    delete_critique,
    get_critique,
    list_critiques,
    rerun_critique,
    stream_critique_progress,
    update_job_tags,
)
from .schedules import (
    create_schedule,
    delete_schedule,
    get_schedule,
    list_schedules,
    update_schedule,
)
from .scoring import (
    delete_config_override,
    get_config_overrides,
    get_scoring_weights_endpoint,
    update_scoring_weights,
)
from .suppressions import create_suppression, delete_suppression, list_suppressions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["critique"], dependencies=list(LICENSE_GATE))

# Order mirrors the original monolithic file:
#   POST /critique, GET /critique/compare, GET /critique/compare/history,
#   analytics, suppressions, schedules, scoring/config,
#   /critique/{job_id}/export/*, /critique/{job_id}/findings/...,
#   PATCH /critique/{job_id}/tags, POST /critique/{job_id}/findings/{fid}/acknowledge,
#   POST /critique/{job_id}/rerun, GET /critique/{job_id}/progress,
#   GET /critique/{job_id}, GET/DELETE /critique[/...], batch.
#
# The literal-prefix routers must come BEFORE ``read_router`` so that
# ``/critique/compare`` and friends win over ``GET /critique/{job_id}``.
router.include_router(jobs.write_router)  # POST /critique, PATCH tags, POST rerun, GET progress
router.include_router(compare.router)
router.include_router(analytics.router)
router.include_router(suppressions.router)
router.include_router(schedules.router)
router.include_router(scoring.router)
router.include_router(exports.router)
router.include_router(findings.router)
router.include_router(jobs.read_router)  # GET /critique/{job_id}, GET /critique, DELETE
router.include_router(batch.router)


__all__ = [
    "_background_tasks",
    "_get_critique_service",
    "_get_tenant_id",
    "_process_job_background",
    "acknowledge_finding",
    "asyncio",
    "compare_critiques",
    "compare_history",
    "create_batch_critique",
    "create_critique",
    "create_schedule",
    "create_suppression",
    "delete_config_override",
    "delete_critique",
    "delete_schedule",
    "delete_suppression",
    "export_csv",
    "export_sarif",
    "get_analytics_trends",
    "get_batch_status",
    "get_config_overrides",
    "get_critique",
    "get_critique_findings",
    "get_critique_report",
    "get_critique_report_pdf",
    "get_domain_analytics",
    "get_schedule",
    "get_scoring_weights_endpoint",
    "get_tenant_summary",
    "get_top_findings",
    "get_unified_findings",
    "list_critiques",
    "list_schedules",
    "list_suppressions",
    "logger",
    "rerun_critique",
    "router",
    "stream_critique_progress",
    "update_job_tags",
    "update_schedule",
    "update_scoring_weights",
]
