"""Critique job lifecycle endpoints: create, get, list, delete, rerun, progress, tags."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    CritiqueJobResponse,
    CritiqueListResponse,
    CritiqueRequest,
    CritiqueResultResponse,
    FindingResponse,
    RerunRequest,
    TagsUpdateRequest,
)
from src.shared.database.session import get_db, get_db_session
from src.shared.security.url_validator import validate_callback_url

from ._helpers import (
    _background_tasks,
    _get_critique_service,
    _get_tenant_id,
    _process_job_background,
    validate_artifact_ref,
)

logger = logging.getLogger(__name__)

# Two routers are defined so the package __init__ can include them at
# different positions in the composite router, preserving the original
# declaration order (which FastAPI uses for path-overlap resolution).
#
# ``write_router`` holds POST/PATCH/DELETE-by-{job_id} routes whose paths or
# methods don't conflict with literal-prefix GETs. It is included EARLY.
#
# ``read_router`` holds the bare ``GET /critique/{job_id}`` and ``GET
# /critique`` and ``DELETE /critique/{job_id}`` routes. It is included AFTER
# the literal-prefix sub-routers (compare, analytics, suppressions, etc.) so
# the literal paths win the match.
write_router = APIRouter()
read_router = APIRouter()

# Backwards-compat: tests expect ``jobs.router`` to exist. Point it at the
# write_router so existing references keep working at the symbol level.
router = write_router


@write_router.post(
    "/critique",
    responses={
        400: {
            "description": (
                "Invalid callback URL or scan URL (SSRF guard), or an "
                "artifact_ref that is not a relative key inside the caller's "
                "own prefix (confinement guard)"
            )
        },
        422: {"description": "Validation error"},
    },
    operation_id="create_critique",
)
async def create_critique(
    body: CritiqueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CritiqueJobResponse:
    """Start a website critique (async, returns job_id)."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    logger.info("Critique requested: url=%s tenant=%s", body.url, tenant_id)

    # Validate callback URL for SSRF before persisting the job.
    if body.callback_url:
        try:
            validate_callback_url(body.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid callback URL: {exc}") from exc

    # Validate the SCAN target for SSRF before persisting the job. ``body.url``
    # is tenant-supplied and reaches three unguarded sinks downstream: the
    # headless-Chromium ``page.goto`` in the axe adapter, the ZAP spider target,
    # and the ``_fetch_html`` httpx GET. Rejecting here — pre-persistence — is
    # the single chokepoint where the request is still an HTTP request and can
    # be answered with a 4xx instead of a half-created job.
    #
    # Deliberately NOT a pydantic field validator on ``CritiqueRequest``: the
    # guard does DNS I/O (``_assert_resolves_public``), and that schema is also
    # deserialized by S2S callers and in tests, where a blocking resolver call
    # inside model construction would be both a latency and a correctness
    # hazard. Route-level keeps the I/O on the request path that owns it.
    if body.url:
        try:
            validate_callback_url(body.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {exc}") from exc

    # ...and the artifact key, for the same pre-persistence reason: a ref that
    # tries to escape the tenant prefix must be answered 400, not accepted as a
    # job that later fails. See ``_helpers.validate_artifact_ref``.
    validate_artifact_ref(body.artifact_ref, tenant_id)

    job_id = await service.create_job(
        db,
        tenant_id=tenant_id,
        url=body.url,
        categories=body.categories,
        max_pages=body.max_pages,
        instructions=body.instructions,
        github_repo=body.github_repo,
        callback_url=body.callback_url,
        tags=body.tags,
        webhook_config=body.webhook_config.model_dump() if body.webhook_config else None,
        auth_info=body.auth_info.model_dump(exclude_none=True) if body.auth_info else None,
    )

    task = asyncio.create_task(
        _process_job_background(
            service,
            job_id,
            tenant_id,
            force_rescan=body.force_rescan,
            site_analysis=body.site_analysis,
            llm_config=body.llm_config,
            html_input=body.html,
            artifact_ref=body.artifact_ref,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Critique job created: job_id=%s url=%s", job_id, body.url)
    return CritiqueJobResponse(
        job_id=job_id,
        status="pending",
        poll_url=f"/api/critique/{job_id}",
    )


@write_router.patch(
    "/critique/{job_id}/tags",
    responses={
        404: {"description": "Critique job not found"},
        422: {"description": "Validation error"},
    },
    operation_id="update_critique_job_tags",
)
async def update_job_tags(
    job_id: str,
    body: TagsUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CritiqueResultResponse:
    """Update tags on a critique job."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")

    job.tags = body.tags
    await db.commit()

    findings = None
    if job.findings:
        findings = [FindingResponse(**f) for f in job.findings]
    return CritiqueResultResponse(
        job_id=job.job_id,
        status=job.status,
        url=job.url,
        findings=findings,
        category_scores=job.category_scores,
        overall_score=job.overall_score,
    )


@write_router.post(
    "/critique/{job_id}/rerun",
    responses={
        400: {"description": "Invalid callback URL or scan URL (SSRF guard)"},
        404: {"description": "Critique job not found"},
        422: {"description": "Validation error"},
    },
    operation_id="rerun_critique",
)
async def rerun_critique(
    job_id: str,
    body: RerunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CritiqueJobResponse:
    """Create a new job with same URL/settings but optionally limited categories."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    logger.info("Rerun requested: original_job_id=%s", job_id)

    original = await service.get_job(db, job_id, tenant_id=tenant_id)
    if not original:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")

    # Defense-in-depth: re-validate the propagated callback URL for SSRF.
    if original.callback_url:
        try:
            validate_callback_url(original.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid callback URL: {exc}") from exc

    # ...and the propagated SCAN target, which is copied into the new job below
    # and dereferenced by the same fetchers. The original row may predate the
    # admission guard on ``POST /critique``, or have been loosened in the DB, so
    # "it was checked when it was created" is not a property this path can rely
    # on -- and a rerun re-dereferences it either way.
    if original.url:
        try:
            validate_callback_url(original.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid URL: {exc}") from exc

    categories = body.categories or original.categories
    new_job_id = await service.create_job(
        db,
        tenant_id=tenant_id,
        url=original.url,
        categories=categories,
        max_pages=original.max_pages or 1,
        instructions=original.instructions,
        github_repo=original.github_repo,
        callback_url=original.callback_url,
    )

    task = asyncio.create_task(
        _process_job_background(service, new_job_id, tenant_id, force_rescan=True)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Rerun job created: new_job_id=%s from=%s", new_job_id, job_id)
    return CritiqueJobResponse(
        job_id=new_job_id,
        status="pending",
        poll_url=f"/api/critique/{new_job_id}",
    )


@write_router.get(
    "/critique/{job_id}/progress",
    responses={
        404: {"description": "Critique job not found"},
        501: {"description": "SSE progress feature is not enabled"},
    },
    operation_id="stream_critique_progress",
)
async def stream_critique_progress(
    job_id: str,
    request: Request,
) -> StreamingResponse:
    """Stream scan progress via Server-Sent Events (feature-flagged).

    Deliberately NOT ``Depends(get_db)``. FastAPI closes a yield-dependency only
    after the response completes, and this response completes when the job
    reaches a terminal status -- which can be many minutes, or never if the
    client just leaves the tab open. A ``SessionDep`` here therefore pins one
    pgbouncer server connection ``idle in transaction`` (the RLS binding opens a
    ``SET LOCAL`` transaction) for the whole stream.

    The stream body reads progress from Redis and touches no table at all, so
    the only database work is the ownership pre-flight below -- which takes a
    short-lived session and gives it straight back.
    """
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.sse_progress:
        raise HTTPException(status_code=501, detail="SSE progress feature is not enabled")

    # Resolve the tenant HERE, inside the request scope. ``event_stream`` below
    # outlives the request and must never touch ``request.state``.
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    async with get_db_session() as db:
        job = await service.get_job(db, job_id, tenant_id=tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")

    async def event_stream() -> AsyncIterator[str]:
        import json
        import time as _time

        from src.shared.cache.redis_client import get_redis_client

        client = get_redis_client()
        last_heartbeat = _time.monotonic()
        heartbeat_interval = 15  # seconds
        while True:
            raw = await client.get(f"critique:progress:{job_id}")
            if raw:
                data = raw if isinstance(raw, str) else json.dumps(raw)
                yield f"data: {data}\n\n"
                last_heartbeat = _time.monotonic()
                progress = json.loads(data) if isinstance(data, str) else data
                if isinstance(progress, dict) and progress.get("status") in (
                    "completed",
                    "failed",
                ):
                    break
            else:
                yield f"data: {json.dumps({'status': 'waiting'})}\n\n"
                last_heartbeat = _time.monotonic()
            await asyncio.sleep(1)
            if _time.monotonic() - last_heartbeat >= heartbeat_interval:
                yield ": heartbeat\n\n"
                last_heartbeat = _time.monotonic()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@read_router.get(
    "/critique/{job_id}",
    responses={
        404: {"description": "Critique job not found"},
    },
    operation_id="get_critique",
)
async def get_critique(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CritiqueResultResponse:
    """Get critique job status and results."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")

    findings = None
    if job.findings:
        findings = [FindingResponse(**f) for f in job.findings]

    token_usage = (job.progress or {}).get("token_usage") if job.progress else None

    return CritiqueResultResponse(
        job_id=job.job_id,
        status=job.status,
        url=job.url,
        findings=findings,
        category_scores=job.category_scores,
        overall_score=job.overall_score,
        confidence_scores=job.confidence_scores,
        summary=job.summary,
        scan_duration=job.scan_duration,
        pages_scanned=job.pages_scanned,
        created_at=job.created_at.isoformat() if job.created_at else None,
        completed_at=job.completed_at.isoformat() if job.completed_at else None,
        error=job.error_message,
        progress=job.progress,
        token_usage=token_usage,
    )


@read_router.get("/critique", operation_id="list_critiques")
async def list_critiques(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    status: str | None = Query(default=None, description="Filter by job status"),
    db: AsyncSession = Depends(get_db),
) -> CritiqueListResponse:
    """List critique jobs for the current tenant."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)

    jobs, total = await service.list_jobs(db, tenant_id, page, per_page, status=status)

    items = [
        CritiqueResultResponse(
            job_id=job.job_id,
            status=job.status,
            url=job.url,
            findings=[FindingResponse(**f) for f in job.findings] if job.findings else None,
            category_scores=job.category_scores,
            overall_score=job.overall_score,
            confidence_scores=job.confidence_scores,
            summary=job.summary,
            scan_duration=job.scan_duration,
            pages_scanned=job.pages_scanned,
            created_at=job.created_at.isoformat() if job.created_at else None,
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            error=job.error_message,
        )
        for job in jobs
    ]

    return CritiqueListResponse(
        items=items,
        total=total,
        page=page,
        per_page=per_page,
    )


@read_router.delete(
    "/critique/{job_id}",
    status_code=204,
    responses={
        404: {"description": "Critique job not found"},
    },
    operation_id="delete_critique",
)
async def delete_critique(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a critique job."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    deleted = await service.delete_job(db, job_id, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
