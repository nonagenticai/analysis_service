"""Review job lifecycle management.

Static methods following tts_service/src/services/job_manager.py pattern.
All methods take AsyncSession as first parameter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from datetime import date as date_type

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.review.models import ReviewBatch, ReviewJob, ReviewUsage

logger = logging.getLogger(__name__)


class JobManager:
    """Manages review job lifecycle in the database."""

    @staticmethod
    async def create_job(
        db: AsyncSession,
        *,
        job_id: str,
        tenant_id: str,
        review_types: list[str],
        url: str | None = None,
        domain: str | None = None,
        user_context: dict | None = None,
    ) -> ReviewJob:
        """Create a new review job."""
        job = ReviewJob(
            job_id=job_id,
            tenant_id=tenant_id,
            status="processing",
            review_types=review_types,
            url=url,
            domain=domain,
            user_context=user_context,
        )
        db.add(job)
        await db.flush()
        logger.info("Created review job %s for tenant %s", job_id, tenant_id)
        return job

    @staticmethod
    async def get_job(db: AsyncSession, job_id: str, *, tenant_id: str) -> ReviewJob | None:
        """Get a review job by job_id, scoped to ``tenant_id``.

        ``tenant_id`` is required and keyword-only on purpose: a by-id lookup
        that forgets it is a silent cross-tenant read. A mismatch returns
        ``None`` so the caller answers 404 — never 403, which would confirm to
        a stranger that the id exists.
        """
        logger.debug("Fetching job: job_id=%s tenant=%s", job_id, tenant_id)
        result = await db.execute(
            select(ReviewJob).where(
                ReviewJob.job_id == job_id,
                ReviewJob.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def mark_completed(
        db: AsyncSession,
        job_id: str,
        results: dict,
    ) -> None:
        """Mark a job as completed with results."""
        logger.debug("Marking job completed: job_id=%s", job_id)
        await db.execute(
            update(ReviewJob)
            .where(ReviewJob.job_id == job_id)
            .values(
                status="completed",
                results=results,
                updated_at=datetime.now(UTC),
            )
        )

    @staticmethod
    async def mark_failed(
        db: AsyncSession,
        job_id: str,
        error_message: str,
    ) -> None:
        """Mark a job as failed with error message."""
        logger.debug("Marking job failed: job_id=%s, error=%s", job_id, error_message)
        await db.execute(
            update(ReviewJob)
            .where(ReviewJob.job_id == job_id)
            .values(
                status="failed",
                error=error_message,
                updated_at=datetime.now(UTC),
            )
        )

    @staticmethod
    async def list_jobs(
        db: AsyncSession,
        *,
        tenant_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ReviewJob], int]:
        """List one tenant's review jobs, paginated. Returns (jobs, total_count).

        ``tenant_id`` is required and keyword-only. It used to default to
        ``None`` and the filter was applied only ``if tenant_id`` — so a caller
        with no tenant in hand listed EVERY tenant's jobs, and the count with
        it. Only RLS stood between that and a live leak. A list route with no
        tenant must return nothing, never everything, so the predicate is now
        unconditional and the caller has to name the tenant it is listing for.
        """
        query = select(ReviewJob).where(ReviewJob.tenant_id == tenant_id)
        query = query.order_by(ReviewJob.created_at.desc())

        # Get total count
        from sqlalchemy import func

        count_query = (
            select(func.count()).select_from(ReviewJob).where(ReviewJob.tenant_id == tenant_id)
        )
        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # Get paginated results
        query = query.offset(offset).limit(limit)
        result = await db.execute(query)
        jobs = list(result.scalars().all())

        return jobs, total

    @staticmethod
    async def record_usage(
        db: AsyncSession,
        *,
        tenant_id: str,
        is_error: bool = False,
    ) -> None:
        """Record usage for daily aggregation (upsert).

        Atomically increments review_count or error_count per tenant per day.
        """
        today = date_type.today()
        stmt = pg_insert(ReviewUsage).values(
            tenant_id=tenant_id,
            date=today,
            review_count=0 if is_error else 1,
            error_count=1 if is_error else 0,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_usage_tenant_date",
            set_={
                "review_count": ReviewUsage.review_count + (0 if is_error else 1),
                "error_count": ReviewUsage.error_count + (1 if is_error else 0),
            },
        )
        await db.execute(stmt)

    @staticmethod
    async def update_results_partial(
        db: AsyncSession,
        job_id: str,
        results: dict,
    ) -> None:
        """Update job results incrementally (for SSE progress tracking)."""
        logger.debug("Updating partial results: job_id=%s, keys=%s", job_id, list(results.keys()))
        await db.execute(
            update(ReviewJob)
            .where(ReviewJob.job_id == job_id)
            .values(
                results=results,
                updated_at=datetime.now(UTC),
            )
        )

    # ------------------------------------------------------------------
    # Phase 4.3: Usage queries
    # ------------------------------------------------------------------

    @staticmethod
    async def get_usage(
        db: AsyncSession,
        *,
        tenant_id: str,
        start_date: date_type | None = None,
        end_date: date_type | None = None,
    ) -> list[ReviewUsage]:
        """Get usage records with optional date range filter."""
        logger.debug(
            "Fetching usage: tenant_id=%s, start=%s, end=%s", tenant_id, start_date, end_date
        )
        query = select(ReviewUsage).where(ReviewUsage.tenant_id == tenant_id)
        if start_date:
            query = query.where(ReviewUsage.date >= start_date)
        if end_date:
            query = query.where(ReviewUsage.date <= end_date)
        query = query.order_by(ReviewUsage.date.desc())
        result = await db.execute(query)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Phase 4.4: Domain history
    # ------------------------------------------------------------------

    @staticmethod
    async def get_domain_history(
        db: AsyncSession,
        *,
        domain: str,
        tenant_id: str,
        limit: int = 10,
    ) -> list[ReviewJob]:
        """Get one tenant's review history for a specific domain.

        ``tenant_id`` is required for the same reason as :meth:`list_jobs`: the
        old ``None`` default plus an ``if tenant_id`` filter turned "no tenant"
        into "every tenant", which leaked who else had reviewed a domain.
        """
        logger.debug(
            "Fetching domain history: domain=%s tenant=%s limit=%d", domain, tenant_id, limit
        )
        query = select(ReviewJob).where(
            ReviewJob.domain == domain,
            ReviewJob.tenant_id == tenant_id,
        )
        query = query.order_by(ReviewJob.created_at.desc()).limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Phase 4.5: Batch management
    # ------------------------------------------------------------------

    @staticmethod
    async def create_batch(
        db: AsyncSession,
        *,
        batch_id: str,
        tenant_id: str,
        job_ids: list[str],
        total: int,
        callback_url: str | None = None,
    ) -> ReviewBatch:
        """Create a new batch record."""
        batch = ReviewBatch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            job_ids=job_ids,
            total=total,
            status="processing",
            callback_url=callback_url,
        )
        db.add(batch)
        await db.flush()
        logger.info("Created batch %s with %d jobs", batch_id, total)
        return batch

    @staticmethod
    async def get_batch(db: AsyncSession, batch_id: str, *, tenant_id: str) -> ReviewBatch | None:
        """Get a batch by batch_id, scoped to ``tenant_id``.

        Same contract as ``get_job``: required, keyword-only, ``None`` on
        mismatch so the caller answers 404. The batch body carries the full
        ``job_ids`` list, so an unscoped fetch is a map of another tenant's work.
        """
        logger.debug("Fetching batch: batch_id=%s tenant=%s", batch_id, tenant_id)
        result = await db.execute(
            select(ReviewBatch).where(
                ReviewBatch.batch_id == batch_id,
                ReviewBatch.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _get_batch_unscoped(db: AsyncSession, batch_id: str) -> ReviewBatch | None:
        """Fetch a batch with NO tenant predicate — internal aggregation only.

        Named with a leading underscore and "unscoped" so it can never be
        mistaken for the request-facing ``get_batch``. Its one caller,
        ``update_batch_progress``, is always reached through a route that has
        already resolved the batch within the caller's tenant.
        """
        result = await db.execute(select(ReviewBatch).where(ReviewBatch.batch_id == batch_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def update_batch_progress(db: AsyncSession, batch_id: str) -> None:
        """Update batch progress by checking status of individual jobs.

        When all jobs are done and the batch has a callback_url, fires the
        completion webhook via ``deliver_webhook()``.
        """
        batch = await JobManager._get_batch_unscoped(db, batch_id)
        if not batch or not batch.job_ids:
            return

        from sqlalchemy import func

        completed_q = (
            select(func.count())
            .select_from(ReviewJob)
            .where(
                ReviewJob.job_id.in_(batch.job_ids),
                ReviewJob.status == "completed",
            )
        )
        failed_q = (
            select(func.count())
            .select_from(ReviewJob)
            .where(
                ReviewJob.job_id.in_(batch.job_ids),
                ReviewJob.status == "failed",
            )
        )

        completed = (await db.execute(completed_q)).scalar() or 0
        failed = (await db.execute(failed_q)).scalar() or 0

        status = "processing"
        completed_at = None
        if completed + failed >= batch.total:
            status = "completed" if failed == 0 else "partial"
            completed_at = datetime.now(UTC)

        await db.execute(
            update(ReviewBatch)
            .where(ReviewBatch.batch_id == batch_id)
            .values(
                completed=completed,
                failed=failed,
                status=status,
                completed_at=completed_at,
            )
        )

        # Task 3: Fire batch completion webhook when all jobs are done
        if completed_at is not None and batch.callback_url:
            from src.config import get_settings

            settings = get_settings()
            if settings.webhook.enabled:
                from src.domains.review.services.webhook import deliver_webhook

                payload = {
                    "batch_id": batch_id,
                    "status": status,
                    "total": batch.total,
                    "completed": completed,
                    "failed": failed,
                    "job_ids": batch.job_ids,
                }
                await deliver_webhook(batch.callback_url, payload)
