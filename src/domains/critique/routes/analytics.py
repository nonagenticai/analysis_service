"""Critique analytics endpoints (WS-5): trends, domain, summary, top findings."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    DomainAnalyticsResponse,
    DomainSummaryItem,
    DomainTrendResponse,
    TenantAnalyticsResponse,
    TopFindingResponse,
    TrendDataPoint,
)
from src.shared.database.session import get_db

from ._helpers import _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/critique/analytics/trends",
    responses={
        501: {"description": "Analytics feature is not enabled"},
    },
    operation_id="get_critique_analytics_trends",
)
async def get_analytics_trends(
    request: Request,
    domain: str = Query(..., description="Domain to get trends for"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days"),
    db: AsyncSession = Depends(get_db),
) -> DomainTrendResponse:
    """Get score trends for a domain over time (feature-flagged)."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.analytics:
        raise HTTPException(status_code=501, detail="Analytics feature is not enabled")

    tenant_id = _get_tenant_id(request)
    logger.info("Analytics trends requested: domain=%s days=%d tenant=%s", domain, days, tenant_id)
    analytics = request.app.state.analytics_service
    data = await analytics.get_domain_trends(db, tenant_id, domain, days)
    logger.info("Analytics trends completed: domain=%s points=%d", domain, len(data))
    return DomainTrendResponse(
        domain=domain,
        days=days,
        data_points=[TrendDataPoint(**d) for d in data],
    )


@router.get(
    "/critique/analytics/domain/{domain}",
    responses={
        501: {"description": "Analytics feature is not enabled"},
    },
    operation_id="get_critique_domain_analytics",
)
async def get_domain_analytics(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DomainAnalyticsResponse:
    """Get aggregate analytics for a domain (feature-flagged)."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.analytics:
        raise HTTPException(status_code=501, detail="Analytics feature is not enabled")

    tenant_id = _get_tenant_id(request)
    logger.info("Domain analytics requested: domain=%s tenant=%s", domain, tenant_id)
    analytics = request.app.state.analytics_service
    data = await analytics.get_domain_analytics(db, tenant_id, domain)
    top_findings = [TopFindingResponse(**f) for f in data.get("top_recurring_findings", [])]
    return DomainAnalyticsResponse(
        domain=data["domain"],
        total_scans=data["total_scans"],
        avg_score=data.get("avg_score"),
        latest_score=data.get("latest_score"),
        score_trend=data.get("score_trend", "stable"),
        top_recurring_findings=top_findings,
        category_averages=data.get("category_averages", {}),
    )


@router.get(
    "/critique/analytics/summary",
    responses={
        501: {"description": "Analytics feature is not enabled"},
    },
    operation_id="get_critique_tenant_summary",
)
async def get_tenant_summary(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TenantAnalyticsResponse:
    """Get tenant-wide analytics summary (feature-flagged)."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.analytics:
        raise HTTPException(status_code=501, detail="Analytics feature is not enabled")

    tenant_id = _get_tenant_id(request)
    logger.info("Tenant analytics summary requested: tenant=%s", tenant_id)
    analytics = request.app.state.analytics_service
    data = await analytics.get_tenant_summary(db, tenant_id)
    return TenantAnalyticsResponse(
        tenant_id=data["tenant_id"],
        total_scans=data["total_scans"],
        total_domains=data["total_domains"],
        avg_score=data.get("avg_score"),
        domains=[DomainSummaryItem(**d) for d in data.get("domains", [])],
    )


@router.get(
    "/critique/analytics/findings/top",
    responses={
        501: {"description": "Analytics feature is not enabled"},
    },
    operation_id="get_critique_top_findings",
)
async def get_top_findings(
    request: Request,
    domain: str | None = Query(default=None, description="Filter by domain"),
    category: str | None = Query(default=None, description="Filter by category"),
    severity: str | None = Query(default=None, description="Filter by severity"),
    limit: int = Query(default=20, ge=1, le=100),
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[TopFindingResponse]:
    """Get most common findings across jobs (feature-flagged)."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.analytics:
        raise HTTPException(status_code=501, detail="Analytics feature is not enabled")

    tenant_id = _get_tenant_id(request)
    logger.info("Top findings requested: tenant=%s domain=%s", tenant_id, domain)
    analytics = request.app.state.analytics_service
    data = await analytics.get_top_findings(
        db,
        tenant_id,
        domain=domain,
        category=category,
        severity=severity,
        limit=limit,
        days=days,
    )
    return [TopFindingResponse(**f) for f in data]
