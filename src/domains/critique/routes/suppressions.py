"""Critique suppression endpoints (WS-6)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    CreateSuppressionRequest,
    SuppressionListResponse,
    SuppressionResponse,
)
from src.shared.database.session import get_db

from ._helpers import _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/critique/suppressions",
    status_code=201,
    responses={
        400: {"description": "Invalid expires_at format"},
        422: {"description": "Validation error"},
    },
    operation_id="create_critique_suppression",
)
async def create_suppression(
    body: CreateSuppressionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SuppressionResponse:
    """Create a finding suppression rule."""
    import uuid
    from datetime import datetime

    from src.domains.critique.models import FindingSuppression

    tenant_id = _get_tenant_id(request)
    logger.info(
        "Suppression creation requested: tenant=%s check_id=%s",
        tenant_id,
        body.check_id,
    )
    suppression_id = str(uuid.uuid7())

    expires_at = None
    if body.expires_at:
        try:
            expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid expires_at format") from None

    suppression = FindingSuppression(
        suppression_id=suppression_id,
        tenant_id=tenant_id,
        domain=body.domain,
        check_id=body.check_id,
        category=body.category,
        reason=body.reason,
        status=body.status,
        expires_at=expires_at,
    )
    db.add(suppression)
    await db.commit()

    logger.info("Suppression created: suppression_id=%s", suppression_id)
    return SuppressionResponse(
        id=suppression_id,
        tenant_id=tenant_id,
        domain=body.domain,
        check_id=body.check_id,
        category=body.category,
        reason=body.reason,
        status=body.status,
        expires_at=body.expires_at,
        created_at=suppression.created_at.isoformat() if suppression.created_at else None,
    )


@router.get("/critique/suppressions", operation_id="list_critique_suppressions")
async def list_suppressions(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SuppressionListResponse:
    """List finding suppression rules for the current tenant."""
    from sqlalchemy import select

    from src.domains.critique.models import FindingSuppression

    tenant_id = _get_tenant_id(request)
    logger.debug("Listing suppressions for tenant=%s", tenant_id)
    result = await db.execute(
        select(FindingSuppression).where(FindingSuppression.tenant_id == tenant_id)
    )
    suppressions = list(result.scalars().all())
    items = [
        SuppressionResponse(
            id=s.suppression_id,
            tenant_id=s.tenant_id,
            domain=s.domain,
            check_id=s.check_id,
            category=s.category,
            reason=s.reason,
            status=s.status,
            expires_at=s.expires_at.isoformat() if s.expires_at else None,
            created_at=s.created_at.isoformat() if s.created_at else None,
        )
        for s in suppressions
    ]
    return SuppressionListResponse(items=items, total=len(items))


@router.delete(
    "/critique/suppressions/{suppression_id}",
    status_code=204,
    responses={
        404: {"description": "Suppression not found"},
    },
    operation_id="delete_critique_suppression",
)
async def delete_suppression(
    suppression_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a suppression rule."""
    from sqlalchemy import select

    from src.domains.critique.models import FindingSuppression

    tenant_id = _get_tenant_id(request)
    logger.info("Suppression deletion requested: suppression_id=%s", suppression_id)
    # Scoped in the SELECT, not after it: a post-fetch check is one `return`
    # away from being skipped, and this path deletes.
    result = await db.execute(
        select(FindingSuppression).where(
            FindingSuppression.suppression_id == suppression_id,
            FindingSuppression.tenant_id == tenant_id,
        )
    )
    suppression = result.scalar_one_or_none()
    if not suppression:
        raise HTTPException(status_code=404, detail="Suppression not found")
    await db.delete(suppression)
    await db.commit()
    logger.info("Suppression deleted: suppression_id=%s", suppression_id)
