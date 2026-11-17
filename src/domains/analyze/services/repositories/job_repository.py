"""Repository for AnalysisJob CRUD operations.

All read/write methods that look up a row by ``job_id`` REQUIRE a ``tenant_id``
keyword argument. The tenant filter is enforced inside the SQL WHERE clause —
this is the security boundary for the service, not a route-level check. A
wrong-tenant lookup returns None and an unauthorised UPDATE is a zero-row
no-op.

``cancel()`` performs a single atomic ``UPDATE ... WHERE status NOT IN (terminal)
RETURNING`` so that a concurrent completion racing the cancel cannot produce
a ``cancelled`` row that lost the completion update.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.models import AnalysisJob

logger = logging.getLogger(__name__)


class AnalysisJobRepository:
    """CRUD operations for AnalysisJob model."""

    @staticmethod
    async def create(
        session: AsyncSession,
        job_id: str,
        tenant_id: str,
        total_pages: int,
        user_context: dict[str, Any] | None = None,
    ) -> AnalysisJob:
        """Create a new analysis job record.

        Args:
            session: Async database session.
            job_id: Unique job identifier.
            tenant_id: Tenant identifier.
            total_pages: Number of pages in the analysis.
            user_context: Optional user context dict.

        Returns:
            The created AnalysisJob instance.
        """
        logger.debug("DB creating analysis job: job_id=%s tenant=%s", job_id, tenant_id)
        job = AnalysisJob(
            job_id=job_id,
            tenant_id=tenant_id,
            total_pages=total_pages,
            status="processing",
            pages_input=user_context,
        )
        session.add(job)
        await session.flush()
        return job

    @staticmethod
    async def get(session: AsyncSession, job_id: str, *, tenant_id: str) -> AnalysisJob | None:
        """Get a job by its job_id, scoped to the caller's tenant.

        Args:
            session: Async database session.
            job_id: The job identifier to look up.
            tenant_id: Tenant scope; cross-tenant lookups return None.

        Returns:
            The AnalysisJob or None if not found.
        """
        logger.debug("DB fetching analysis job: job_id=%s tenant=%s", job_id, tenant_id)
        result = await session.execute(
            select(AnalysisJob).where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def update_status(
        session: AsyncSession,
        job_id: str,
        status: str,
        *,
        tenant_id: str,
        results: dict[str, Any] | None = None,
        error: str | None = None,
        pages_analyzed: list[dict] | None = None,
        site_analysis: dict | None = None,
        narrative_extraction: dict | None = None,
    ) -> None:
        """Update job status and optionally store results.

        Tenant-scoped — a wrong-tenant call is a zero-row UPDATE.
        """
        logger.debug(
            "DB updating job status: job_id=%s tenant=%s status=%s",
            job_id,
            tenant_id,
            status,
        )
        values: dict[str, Any] = {"status": status}
        if results is not None:
            values["results_json"] = results
        if error is not None:
            values["error"] = error
        if pages_analyzed is not None:
            values["pages_analyzed"] = pages_analyzed
        if site_analysis is not None:
            values["site_analysis"] = site_analysis
        if narrative_extraction is not None:
            values["narrative_extraction"] = narrative_extraction
        await session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
            )
            .values(**values)
        )
        await session.flush()

    @staticmethod
    async def list_by_tenant(
        session: AsyncSession,
        tenant_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[AnalysisJob]:
        """List jobs for a given tenant, newest first.

        Args:
            session: Async database session.
            tenant_id: Tenant identifier.
            limit: Maximum results to return.
            offset: Pagination offset.

        Returns:
            List of AnalysisJob instances.
        """
        result = await session.execute(
            select(AnalysisJob)
            .where(AnalysisJob.tenant_id == tenant_id)
            .order_by(AnalysisJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    @staticmethod
    async def list_by_tenant_filtered(
        session: AsyncSession,
        tenant_id: str,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[AnalysisJob], int]:
        """List jobs for a tenant with optional status filter. Returns (jobs, total_count)."""
        query = select(AnalysisJob).where(AnalysisJob.tenant_id == tenant_id)
        count_query = (
            select(func.count()).select_from(AnalysisJob).where(AnalysisJob.tenant_id == tenant_id)
        )
        if status:
            query = query.where(AnalysisJob.status == status)
            count_query = count_query.where(AnalysisJob.status == status)
        query = query.order_by(AnalysisJob.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(query)
        count_result = await session.execute(count_query)
        return list(result.scalars().all()), count_result.scalar_one()

    @staticmethod
    async def store_artifacts(
        session: AsyncSession,
        job_id: str,
        artifacts: list[dict],
        *,
        tenant_id: str,
    ) -> None:
        """Store analysis artifacts for a job (tenant-scoped)."""
        logger.debug(
            "DB storing artifacts: job_id=%s tenant=%s count=%d",
            job_id,
            tenant_id,
            len(artifacts),
        )
        await session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
            )
            .values(analysis_artifacts=artifacts)
        )
        await session.flush()

    @staticmethod
    async def get_artifacts(
        session: AsyncSession,
        job_id: str,
        *,
        tenant_id: str,
    ) -> dict | None:
        """Get analysis artifacts for a job (tenant-scoped)."""
        logger.debug("DB fetching artifacts: job_id=%s tenant=%s", job_id, tenant_id)
        result = await session.execute(
            select(AnalysisJob.analysis_artifacts).where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
            )
        )
        row = result.scalar_one_or_none()
        return row if row is not None else None

    @staticmethod
    async def cancel(session: AsyncSession, job_id: str, *, tenant_id: str) -> AnalysisJob | None:
        """Atomically cancel a job, race-safe against concurrent completion.

        A naive read → branch-on-terminal → write sequence loses to a worker
        that completes the job between our SELECT and our UPDATE: our cancel
        would clobber the freshly written ``completed`` status. The fix is a
        single ``UPDATE ... WHERE status NOT IN (terminal) RETURNING`` —
        Postgres serialises the predicate evaluation and write atomically, so
        either we win (RETURNING yields the cancelled row) or the competing
        completion wins (zero rows updated and a follow-up SELECT shows the
        terminal state).

        Tenant-scoped: a wrong-tenant call returns None even if the job_id
        exists for another tenant.
        """
        logger.debug("DB cancelling job: job_id=%s tenant=%s", job_id, tenant_id)
        result = await session.execute(
            update(AnalysisJob)
            .where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
                AnalysisJob.status.not_in(("completed", "failed", "cancelled")),
            )
            .values(status="cancelled")
            .returning(AnalysisJob)
        )
        cancelled = result.scalar_one_or_none()
        if cancelled is not None:
            await session.flush()
            return cancelled
        # UPDATE matched zero rows: either the job doesn't exist for this
        # tenant, or it's already in a terminal state. SELECT to disambiguate.
        sel = await session.execute(
            select(AnalysisJob).where(
                AnalysisJob.job_id == job_id,
                AnalysisJob.tenant_id == tenant_id,
            )
        )
        return sel.scalar_one_or_none()
