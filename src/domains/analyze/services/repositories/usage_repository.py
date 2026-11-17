"""Repository for AnalysisUsage tracking."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.models import AnalysisUsage

logger = logging.getLogger(__name__)


class AnalysisUsageRepository:
    """CRUD operations for usage tracking."""

    @staticmethod
    async def record_usage(
        session: AsyncSession,
        tenant_id: str,
        job_id: str,
        pages_count: int,
    ) -> AnalysisUsage:
        """Record a usage entry for an analysis job.

        Args:
            session: Async database session.
            tenant_id: Tenant identifier.
            job_id: The analysis job identifier (for logging).
            pages_count: Number of pages analyzed.

        Returns:
            The created AnalysisUsage record.
        """
        logger.debug("DB recording usage: tenant=%s pages=%d", tenant_id, pages_count)
        usage = AnalysisUsage(
            tenant_id=tenant_id,
            pages_analyzed=pages_count,
            analysis_count=1,
        )
        session.add(usage)
        await session.flush()
        return usage

    @staticmethod
    async def get_daily_usage(session: AsyncSession, tenant_id: str) -> int:
        """Get today's total pages analyzed for a tenant.

        Args:
            session: Async database session.
            tenant_id: Tenant identifier.

        Returns:
            Total pages analyzed today.
        """
        today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        result = await session.execute(
            select(func.coalesce(func.sum(AnalysisUsage.pages_analyzed), 0)).where(
                AnalysisUsage.tenant_id == tenant_id,
                AnalysisUsage.date >= today_start,
            )
        )
        return int(result.scalar() or 0)
