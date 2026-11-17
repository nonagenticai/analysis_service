"""Critique findings endpoints: paginated list, unified format, acknowledgement."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    AcknowledgeFindingRequest,
    FindingResponse,
    FindingsListResponse,
)
from src.shared.database.session import get_db

from ._helpers import _get_critique_service, _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/critique/{job_id}/findings/unified",
    responses={
        404: {"description": "Critique job not found"},
        409: {"description": "Job not completed"},
    },
    operation_id="get_critique_unified_findings",
)
async def get_unified_findings(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Get findings in unified cross-service format."""
    from src.domains.critique.services.finding_converters import critique_finding_to_unified

    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="Job not completed",
            headers={"Retry-After": "30"},
        )

    findings = job.findings or []
    return [critique_finding_to_unified(f).model_dump() for f in findings]


@router.post(
    "/critique/{job_id}/findings/{finding_id}/acknowledge",
    responses={
        404: {"description": "Finding not found"},
        422: {"description": "Validation error"},
    },
    operation_id="acknowledge_critique_finding",
)
async def acknowledge_finding(
    job_id: str,
    finding_id: str,
    body: AcknowledgeFindingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Acknowledge a specific finding (creates a suppression rule)."""
    import uuid

    from sqlalchemy import select

    from src.domains.critique.models import CritiqueFinding, FindingSuppression

    tenant_id = _get_tenant_id(request)

    # ``critique_findings`` now carries its own ``tenant_id`` (RLS-enrolled), so
    # ownership is a direct predicate — the join to ``critique_jobs`` that used to
    # derive it is gone. Both predicates are still required: ``tenant_id`` scopes
    # the tenant, and the ``{job_id}`` path-param match stops a foreign job id from
    # being used as the door (it was previously ignored entirely, making a finding
    # reachable through ANY job id). A mismatched pair is "not found", which also
    # closes the 200-vs-404 oracle on finding ids.
    result = await db.execute(
        select(CritiqueFinding).where(
            CritiqueFinding.finding_id == finding_id,
            CritiqueFinding.job_id == job_id,
            CritiqueFinding.tenant_id == tenant_id,
        )
    )
    finding = result.scalar_one_or_none()
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    suppression_id = str(uuid.uuid7())
    suppression = FindingSuppression(
        suppression_id=suppression_id,
        tenant_id=tenant_id,
        check_id=finding.check_id,
        reason=body.reason,
        status="acknowledged",
    )
    db.add(suppression)

    finding.is_suppressed = True
    finding.suppression_id = suppression_id
    await db.commit()

    return {"status": "acknowledged", "finding_id": finding_id, "suppression_id": suppression_id}


@router.get(
    "/critique/{job_id}/findings",
    responses={
        404: {"description": "Critique job not found"},
        409: {"description": "Job not completed"},
    },
    operation_id="get_critique_findings",
)
async def get_critique_findings(
    job_id: str,
    request: Request,
    category: str | None = Query(default=None, description="Filter by category"),
    severity: str | None = Query(default=None, description="Filter by severity"),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> FindingsListResponse:
    """Get paginated findings for a critique job with filtering."""
    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Critique not completed yet (status: {job.status})",
            headers={"Retry-After": "30"},
        )

    all_findings = job.findings or []

    filtered = all_findings
    if category:
        filtered = [f for f in filtered if f.get("category") == category]
    if severity:
        filtered = [f for f in filtered if f.get("severity") == severity]

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_findings = filtered[start:end]

    return FindingsListResponse(
        job_id=job_id,
        findings=[FindingResponse(**f) for f in page_findings],
        total=total,
        page=page,
        per_page=per_page,
    )
