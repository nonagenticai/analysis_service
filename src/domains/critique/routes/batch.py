"""Batch critique endpoints (feature-flagged)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    BatchCritiqueRequest,
    BatchCritiqueResponse,
    BatchStatusResponse,
)
from src.shared.database.session import get_db
from src.shared.security.url_validator import validate_callback_url

from ._helpers import (
    _background_tasks,
    _get_critique_service,
    _get_tenant_id,
    _process_job_background,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/critique/batch",
    responses={
        400: {"description": "Invalid callback URL or scan URL (SSRF guard)"},
        422: {"description": "Validation error"},
        501: {"description": "Batch scan feature is not enabled"},
    },
    operation_id="create_critique_batch",
)
async def create_batch_critique(
    body: BatchCritiqueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BatchCritiqueResponse:
    """Start a batch website critique (feature-flagged)."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.batch_scan:
        raise HTTPException(status_code=501, detail="Batch scan feature is not enabled")

    # Validate the batch callback URL for SSRF before persisting any jobs.
    if body.callback_url:
        try:
            validate_callback_url(body.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid callback URL: {exc}") from exc

    # Validate every SCAN target for SSRF before persisting any job. Each entry
    # of ``body.urls`` is tenant-supplied and lands in a critique job whose
    # downstream sinks (headless-Chromium ``page.goto``, the ZAP spider target,
    # the ``_fetch_html`` httpx GET) are otherwise unguarded. The batch is
    # all-or-nothing: one bad URL rejects the whole request rather than creating
    # a partial batch whose status can never reach ``completed``.
    #
    # Deliberately NOT a pydantic field validator on ``BatchCritiqueRequest``:
    # the guard does DNS I/O, and the schema is also deserialized by S2S callers
    # and in tests, where a blocking resolver call during model construction
    # would be both a latency and a correctness hazard.
    for candidate in body.urls:
        try:
            validate_callback_url(candidate)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid URL '{candidate}': {exc}"
            ) from exc

    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)
    logger.info("Batch critique requested: %d URLs tenant=%s", len(body.urls), tenant_id)

    batch_id, job_ids = await service.create_batch(
        db,
        tenant_id=tenant_id,
        urls=body.urls,
        categories=body.categories,
        max_pages=body.max_pages,
        instructions=body.instructions,
        callback_url=body.callback_url,
    )

    for jid in job_ids:
        task = asyncio.create_task(
            _process_job_background(service, jid, tenant_id, llm_config=body.llm_config)
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    logger.info("Batch created: batch_id=%s jobs=%d", batch_id, len(job_ids))
    return BatchCritiqueResponse(
        batch_id=batch_id,
        job_ids=job_ids,
        total=len(job_ids),
    )


@router.get(
    "/critique/batch/{batch_id}",
    responses={
        404: {"description": "Batch not found"},
    },
    operation_id="get_critique_batch_status",
)
async def get_batch_status(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BatchStatusResponse:
    """Get batch critique status."""
    tenant_id = _get_tenant_id(request)
    service = _get_critique_service(request)

    await service.update_batch_status(db, batch_id, tenant_id=tenant_id)

    batch = await service.get_batch(db, batch_id, tenant_id=tenant_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    return BatchStatusResponse(
        batch_id=batch.batch_id,
        status=batch.status,
        total=batch.total,
        completed=batch.completed,
        failed=batch.failed,
        job_ids=batch.job_ids,
    )
