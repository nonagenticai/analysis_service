"""Critique scheduled-scan endpoints (WS-8)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from w2v_common.auth import get_tenant_source_from_context

from src.domains.critique.schemas import (
    CreateScheduleRequest,
    ScheduleListResponse,
    ScheduleResponse,
    UpdateScheduleRequest,
)
from src.shared.database.session import get_db
from src.shared.security.url_validator import validate_callback_url

from ._helpers import _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/critique/schedules",
    status_code=201,
    responses={
        400: {"description": "Invalid scan URL (SSRF guard)"},
        422: {"description": "Validation error"},
        501: {"description": "Scheduled scans feature is not enabled"},
    },
    operation_id="create_critique_schedule",
)
async def create_schedule(
    body: CreateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Create a scheduled recurring scan (feature-flagged)."""
    import uuid

    from src.config import get_settings
    from src.domains.critique.models import CritiqueSchedule
    from src.domains.critique.services.scheduler import next_run_from_cron

    settings = get_settings()
    if not settings.features.scheduled_scans:
        raise HTTPException(status_code=501, detail="Scheduled scans feature is not enabled")

    tenant_id = _get_tenant_id(request)
    logger.info(
        "Schedule creation requested: url=%s cron=%s tenant=%s",
        body.url,
        body.cron_expression,
        tenant_id,
    )

    # Validate the SCAN target for SSRF before persisting the schedule, with the
    # same validator and the same error shape ``jobs.py``/``batch.py`` use. The
    # scheduler turns this row into an ordinary critique job on a timer, so the
    # row reaches exactly the fetchers and scanners ``POST /api/critique``
    # guards -- and until this check existed, a tenant refused there could
    # simply resubmit the identical internal URL here and have the platform
    # dereference it every hour instead of once.
    #
    # Deliberately NOT a pydantic field validator on ``CreateScheduleRequest``:
    # the guard does DNS I/O, and that schema is also deserialized by S2S
    # callers and in tests, where a blocking resolver call inside model
    # construction would be both a latency and a correctness hazard.
    try:
        validate_callback_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid URL: {exc}") from exc

    schedule_id = str(uuid.uuid7())
    next_run = next_run_from_cron(body.cron_expression)

    # Capture HOW this request established its tenant. THIS is the only moment
    # the provenance exists: the scheduler tick that later fires this row runs
    # off a 60s cron poll with no request scope at all, so unless the answer is
    # persisted here it is gone, and the tick can only claim an unvouched tenant
    # -- which the outbound hook refuses at the first metered hop.
    #
    # ``None`` is stored as NULL verbatim. A request that genuinely had no
    # recorded provenance (a trusted-network lane, a test, an S2S caller that
    # never stated one) must NOT have one invented for it here: substituting
    # ``VOUCHED_ONBEHALF`` or any other value would forge a vouch and let a bare
    # header spend an arbitrary tenant's credentials forever, on a timer. The
    # tick fails closed on NULL instead.
    tenant_source = get_tenant_source_from_context()

    schedule = CritiqueSchedule(
        schedule_id=schedule_id,
        tenant_id=tenant_id,
        tenant_source=tenant_source.value if tenant_source is not None else None,
        url=body.url,
        categories=body.categories,
        cron_expression=body.cron_expression,
        enabled=True,
        next_run_at=next_run,
    )
    db.add(schedule)
    await db.commit()

    logger.info("Schedule created: schedule_id=%s url=%s", schedule_id, body.url)
    return ScheduleResponse(
        schedule_id=schedule_id,
        tenant_id=tenant_id,
        url=body.url,
        categories=body.categories,
        cron_expression=body.cron_expression,
        enabled=True,
        next_run_at=next_run.isoformat(),
        run_count=0,
        created_at=schedule.created_at.isoformat() if schedule.created_at else None,
    )


@router.get(
    "/critique/schedules",
    responses={
        501: {"description": "Scheduled scans feature is not enabled"},
    },
    operation_id="list_critique_schedules",
)
async def list_schedules(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleListResponse:
    """List scheduled scans for the current tenant."""
    from sqlalchemy import select

    from src.config import get_settings
    from src.domains.critique.models import CritiqueSchedule

    settings = get_settings()
    if not settings.features.scheduled_scans:
        raise HTTPException(status_code=501, detail="Scheduled scans feature is not enabled")

    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        select(CritiqueSchedule).where(CritiqueSchedule.tenant_id == tenant_id)
    )
    schedules = list(result.scalars().all())
    items = [
        ScheduleResponse(
            schedule_id=s.schedule_id,
            tenant_id=s.tenant_id,
            url=s.url,
            categories=s.categories,
            cron_expression=s.cron_expression,
            enabled=s.enabled,
            last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
            next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
            last_job_id=s.last_job_id,
            run_count=s.run_count or 0,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in schedules
    ]
    return ScheduleListResponse(items=items, total=len(items))


@router.get(
    "/critique/schedules/{schedule_id}",
    responses={
        404: {"description": "Schedule not found"},
    },
    operation_id="get_critique_schedule",
)
async def get_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Get a scheduled scan by ID."""
    from sqlalchemy import select

    from src.domains.critique.models import CritiqueSchedule

    result = await db.execute(
        select(CritiqueSchedule).where(
            CritiqueSchedule.schedule_id == schedule_id,
            CritiqueSchedule.tenant_id == _get_tenant_id(request),
        )
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return ScheduleResponse(
        schedule_id=s.schedule_id,
        tenant_id=s.tenant_id,
        url=s.url,
        categories=s.categories,
        cron_expression=s.cron_expression,
        enabled=s.enabled,
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
        next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
        last_job_id=s.last_job_id,
        run_count=s.run_count or 0,
        created_at=s.created_at.isoformat() if s.created_at else None,
    )


@router.put(
    "/critique/schedules/{schedule_id}",
    responses={
        400: {"description": "Invalid scan URL (SSRF guard)"},
        404: {"description": "Schedule not found"},
        422: {"description": "Validation error"},
    },
    operation_id="update_critique_schedule",
)
async def update_schedule(
    schedule_id: str,
    body: UpdateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    """Update a scheduled scan."""
    from sqlalchemy import select

    from src.domains.critique.models import CritiqueSchedule
    from src.domains.critique.services.scheduler import next_run_from_cron

    result = await db.execute(
        select(CritiqueSchedule).where(
            CritiqueSchedule.schedule_id == schedule_id,
            CritiqueSchedule.tenant_id == _get_tenant_id(request),
        )
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if body.url is not None:
        # Same screen as create: PUT is a second write path to the same column,
        # and a guard on only one of them is not a guard. Validated BEFORE the
        # assignment so a rejected URL never touches the ORM object at all.
        try:
            validate_callback_url(body.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {exc}") from exc
        s.url = body.url
    if body.categories is not None:
        s.categories = body.categories
    if body.cron_expression is not None:
        s.cron_expression = body.cron_expression
        s.next_run_at = next_run_from_cron(body.cron_expression)
    if body.enabled is not None:
        s.enabled = body.enabled

    await db.commit()

    return ScheduleResponse(
        schedule_id=s.schedule_id,
        tenant_id=s.tenant_id,
        url=s.url,
        categories=s.categories,
        cron_expression=s.cron_expression,
        enabled=s.enabled,
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
        next_run_at=s.next_run_at.isoformat() if s.next_run_at else None,
        last_job_id=s.last_job_id,
        run_count=s.run_count or 0,
        created_at=s.created_at.isoformat() if s.created_at else None,
    )


@router.delete(
    "/critique/schedules/{schedule_id}",
    status_code=204,
    responses={
        404: {"description": "Schedule not found"},
    },
    operation_id="delete_critique_schedule",
)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a scheduled scan."""
    from sqlalchemy import select

    from src.domains.critique.models import CritiqueSchedule

    result = await db.execute(
        select(CritiqueSchedule).where(
            CritiqueSchedule.schedule_id == schedule_id,
            CritiqueSchedule.tenant_id == _get_tenant_id(request),
        )
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.delete(s)
    await db.commit()
