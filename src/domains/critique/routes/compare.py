"""Critique comparison endpoints: pair compare, historical compare."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    ComparisonResponse,
    FindingChangeEvent,
    HistoricalComparisonResponse,
)
from src.shared.database.session import get_db

from ._helpers import _get_critique_service, _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/critique/compare",
    responses={
        404: {"description": "Job not found"},
        409: {"description": "Both jobs must be completed"},
    },
    operation_id="compare_critiques",
)
async def compare_critiques(
    request: Request,
    job_id_1: str = Query(..., description="First job ID"),
    job_id_2: str = Query(..., description="Second job ID"),
    db: AsyncSession = Depends(get_db),
) -> ComparisonResponse:
    """Compare findings between two critique jobs.

    Supports before/after deltas across mixed input modes (e.g. a live-URL
    "before" vs a static html/artifact "after"). ``compute_score_deltas``
    treats a category present on only one side as ``0.0`` on the other, so the
    call never errors — but the result is **semantically asymmetric**: a static
    job runs a strict subset of checks (seo base only, security HTML-subset
    only; no seo_live / live-security / adapters), so ``seo``/``security``
    deltas are not apples-to-apples and categories present on one side only
    yield full-value deltas. Each job's ``progress.input_mode`` /
    ``progress.skipped_categories`` disclose which side was static.
    """
    from src.domains.critique.services.report_generator import (
        compare_findings,
        compute_score_deltas,
    )

    logger.info("Compare requested: job_id_1=%s job_id_2=%s", job_id_1, job_id_2)
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)

    # BOTH sides are scoped. Scoping only one leaves the route a leak: the
    # caller supplies both ids, so an unscoped second slot reads any job in the
    # estate (and the 409/score deltas below would disclose its contents).
    job1 = await service.get_job(db, job_id_1, tenant_id=tenant_id)
    job2 = await service.get_job(db, job_id_2, tenant_id=tenant_id)

    if not job1:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id_1}")
    if not job2:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id_2}")
    if job1.status != "completed" or job2.status != "completed":
        raise HTTPException(status_code=409, detail="Both jobs must be completed for comparison")

    findings_1 = job1.findings or []
    findings_2 = job2.findings or []

    new_findings, resolved_findings, persistent = compare_findings(findings_1, findings_2)

    score_deltas = compute_score_deltas(job1.category_scores or {}, job2.category_scores or {})

    overall_delta = None
    if job1.overall_score is not None and job2.overall_score is not None:
        overall_delta = round(float(job2.overall_score) - float(job1.overall_score), 1)

    logger.info(
        "Compare completed: new=%d resolved=%d persistent=%d",
        len(new_findings),
        len(resolved_findings),
        persistent,
    )
    return ComparisonResponse(
        job_id_1=job_id_1,
        job_id_2=job_id_2,
        url_1=job1.url,
        url_2=job2.url,
        score_deltas=score_deltas,
        overall_delta=overall_delta,
        new_findings=len(new_findings),
        resolved_findings=len(resolved_findings),
        persistent_findings=persistent,
    )


@router.get(
    "/critique/compare/history",
    responses={
        409: {"description": "Need at least 2 completed jobs for domain"},
    },
    operation_id="list_critique_compare_history",
)
async def compare_history(
    request: Request,
    domain: str = Query(..., description="Domain to compare"),
    limit: int = Query(default=10, ge=2, le=50),
    db: AsyncSession = Depends(get_db),
) -> HistoricalComparisonResponse:
    """Compare all jobs for a domain over time, tracking finding changes."""
    from src.domains.critique.services.report_generator import compare_findings

    logger.info("Historical comparison requested: domain=%s limit=%d", domain, limit)
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)

    jobs, _ = await service.list_jobs(db, tenant_id, page=1, per_page=limit, status="completed")
    domain_jobs = [j for j in jobs if j.domain == domain]

    if len(domain_jobs) < 2:
        raise HTTPException(status_code=409, detail="Need at least 2 completed jobs for domain")

    latest = domain_jobs[0]
    previous = domain_jobs[1]

    new_f, resolved_f, _ = compare_findings(previous.findings or [], latest.findings or [])

    def _to_event(findings: list[dict], change: str) -> list[FindingChangeEvent]:
        return [
            FindingChangeEvent(
                check_id=f.get("check_id"),
                title=f.get("title", ""),
                category=f.get("category", ""),
                severity=f.get("severity", "info"),
                change=change,
            )
            for f in findings
        ]

    return HistoricalComparisonResponse(
        domain=domain,
        jobs_compared=len(domain_jobs),
        new_findings=_to_event(new_f, "new"),
        resolved_findings=_to_event(resolved_f, "resolved"),
    )
