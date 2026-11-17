"""Repository for AnalyzeReviewJob CRUD (the analyze-embedded review sub-jobs).

All lookups by ``review_job_id`` REQUIRE a ``tenant_id`` keyword — the tenant
filter is enforced in the SQL WHERE clause, which is the isolation boundary (a
wrong-tenant read returns None; a wrong-tenant write is a zero-row no-op). This
mirrors ``AnalysisJobRepository`` so the durable review-job store behaves like
the analysis-job store it sits beside.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.review_job_models import AnalyzeReviewJob

logger = logging.getLogger(__name__)


class AnalyzeReviewJobRepository:
    """CRUD operations for the ``analyze_review_jobs`` durable store."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        review_job_id: str,
        tenant_id: str,
        review_types: list[str],
    ) -> AnalyzeReviewJob:
        """Insert a new review job in the ``processing`` state."""
        logger.debug("DB creating review job: review_job_id=%s tenant=%s", review_job_id, tenant_id)
        job = AnalyzeReviewJob(
            review_job_id=review_job_id,
            tenant_id=tenant_id,
            status="processing",
            review_types=review_types,
        )
        session.add(job)
        await session.flush()
        return job

    @staticmethod
    async def update_result(
        session: AsyncSession,
        *,
        review_job_id: str,
        tenant_id: str,
        status: str,
        reviews: dict[str, Any] | None = None,
        error: str | None = None,
        processing_time_seconds: float | None = None,
        llm_calls: int | None = None,
    ) -> None:
        """Update a review job's terminal state (tenant-scoped; zero-row no-op if wrong tenant)."""
        logger.debug(
            "DB updating review job: review_job_id=%s tenant=%s status=%s",
            review_job_id,
            tenant_id,
            status,
        )
        values: dict[str, Any] = {"status": status}
        if reviews is not None:
            values["reviews"] = reviews
        if error is not None:
            values["error"] = error
        if processing_time_seconds is not None:
            values["processing_time_seconds"] = processing_time_seconds
        if llm_calls is not None:
            values["llm_calls"] = llm_calls
        await session.execute(
            update(AnalyzeReviewJob)
            .where(
                AnalyzeReviewJob.review_job_id == review_job_id,
                AnalyzeReviewJob.tenant_id == tenant_id,
            )
            .values(**values)
        )
        await session.flush()

    @staticmethod
    async def get(
        session: AsyncSession, review_job_id: str, *, tenant_id: str
    ) -> AnalyzeReviewJob | None:
        """Fetch a review job by id, scoped to the caller's tenant (cross-tenant → None)."""
        logger.debug("DB fetching review job: review_job_id=%s tenant=%s", review_job_id, tenant_id)
        result = await session.execute(
            select(AnalyzeReviewJob).where(
                AnalyzeReviewJob.review_job_id == review_job_id,
                AnalyzeReviewJob.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()
