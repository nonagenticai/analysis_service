"""Job + batch lifecycle CRUD methods for ``CritiqueService``."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.models import CritiqueBatch, CritiqueFinding, CritiqueJob
from src.domains.critique.services.report_generator import _extract_domain

logger = logging.getLogger(__name__)


class JobLifecycleMixin:
    """Mixin for ``CritiqueService`` providing CRUD + batch helpers."""

    async def create_job(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        url: str | None = None,
        categories: list[str] | None = None,
        max_pages: int = 1,
        instructions: str | None = None,
        github_repo: str | None = None,
        callback_url: str | None = None,
        tags: list[str] | None = None,
        webhook_config: dict | None = None,
        auth_info: dict | None = None,
    ) -> str:
        """Create a new critique job, returns job_id.

        When ``url`` is None (the caller scores pre-rendered ``html`` /
        ``artifact_ref`` instead), a unique synthetic ``rendered://{job_id}``
        URL is stored so the ~8 downstream ``job.url`` references remain valid.
        It has no hostname (``_extract_domain`` → "") and is unique per job, so
        the live fetch/live scanners are never driven against it and the
        URL-keyed cache naturally misses.
        """
        job_id = str(uuid.uuid7())
        resolved_url = url or f"rendered://{job_id}"
        logger.info("Creating critique job: url=%s tenant=%s", resolved_url, tenant_id)
        job = CritiqueJob(
            job_id=job_id,
            tenant_id=tenant_id,
            url=resolved_url,
            domain=_extract_domain(resolved_url),
            categories=categories,
            max_pages=max_pages,
            instructions=instructions,
            github_repo=github_repo,
            callback_url=callback_url,
            tags=tags,
            webhook_config=webhook_config,
            auth_info=auth_info,
            status="pending",
        )
        db.add(job)
        await db.flush()

        from src.domains.critique.services import critique_service as _pkg

        await _pkg.log_event(
            db,
            event_type="job_created",
            tenant_id=tenant_id,
            job_id=job_id,
            details={"url": resolved_url, "categories": categories},
        )
        await db.commit()

        return job_id

    async def mark_failed(
        self, db: AsyncSession, job_id: str, error: str, *, tenant_id: str
    ) -> None:
        """Mark a critique job as failed, within ``tenant_id``."""
        job = await self._get_job(db, job_id, tenant_id=tenant_id)
        if job:
            job.status = "failed"
            job.error_message = error
            job.completed_at = datetime.now(UTC)
            await db.commit()

    async def get_job(self, db: AsyncSession, job_id: str, *, tenant_id: str) -> CritiqueJob | None:
        """Get a critique job by ID, scoped to ``tenant_id``.

        ``tenant_id`` is required and keyword-only on purpose: a by-id lookup
        that forgets it is a silent cross-tenant read, and a positional
        optional argument is exactly the kind of thing a caller drops. A
        mismatch returns ``None`` so the caller answers 404 — never 403, which
        would confirm the id exists to a tenant that must not learn that.
        """
        return await self._get_job(db, job_id, tenant_id=tenant_id)

    async def list_jobs(
        self,
        db: AsyncSession,
        tenant_id: str,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
    ) -> tuple[list[CritiqueJob], int]:
        """List critique jobs for a tenant with pagination."""
        conditions = [CritiqueJob.tenant_id == tenant_id]
        if status:
            conditions.append(CritiqueJob.status == status)

        count_result = await db.execute(select(func.count(CritiqueJob.id)).where(*conditions))
        total = count_result.scalar() or 0

        offset = (page - 1) * per_page
        result = await db.execute(
            select(CritiqueJob)
            .where(*conditions)
            .order_by(desc(CritiqueJob.created_at))
            .offset(offset)
            .limit(per_page)
        )
        jobs = list(result.scalars().all())
        return jobs, total

    async def delete_job(self, db: AsyncSession, job_id: str, *, tenant_id: str) -> bool:
        """Delete a critique job owned by ``tenant_id``, findings included.

        ``critique_findings.job_id`` is a plain VARCHAR reference — there is no FK
        and therefore no ``ON DELETE CASCADE``. Deleting only the job left its
        findings behind forever: unreachable (every read path starts from a
        tenant-scoped job) but still on disk, and before this table carried a
        ``tenant_id`` they were entirely outside RLS. Both deletes run in the one
        transaction committed below, so a tenant's delete never half-applies.
        """
        job = await self._get_job(db, job_id, tenant_id=tenant_id)
        if not job:
            return False
        # Scoped by job_id alone, deliberately: ``critique_jobs.job_id`` is UNIQUE
        # and ``_get_job`` above already proved this tenant owns it, so job_id is
        # exact. An extra ``tenant_id == tenant_id`` predicate would SKIP the
        # NULL-tenant findings an old replica can write during the expand/contract
        # rolling window (see migration 0008), re-orphaning them. Under enforce the
        # DELETE policy scopes the statement regardless.
        await db.execute(delete(CritiqueFinding).where(CritiqueFinding.job_id == job_id))
        await db.delete(job)
        await db.commit()
        return True

    async def create_batch(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        urls: list[str],
        categories: list[str] | None = None,
        max_pages: int = 1,
        instructions: str | None = None,
        callback_url: str | None = None,
    ) -> tuple[str, list[str]]:
        """Create a batch of critique jobs. Returns (batch_id, job_ids)."""
        batch_id = str(uuid.uuid7())
        job_ids: list[str] = []

        for url in urls:
            jid = await self.create_job(
                db,
                tenant_id=tenant_id,
                url=url,
                categories=categories,
                max_pages=max_pages,
                instructions=instructions,
            )
            job_ids.append(jid)

        batch = CritiqueBatch(
            batch_id=batch_id,
            tenant_id=tenant_id,
            job_ids=job_ids,
            total=len(urls),
            status="pending",
            callback_url=callback_url,
        )
        db.add(batch)
        await db.commit()
        return batch_id, job_ids

    async def get_batch(
        self, db: AsyncSession, batch_id: str, *, tenant_id: str
    ) -> CritiqueBatch | None:
        """Get a batch by ID, scoped to ``tenant_id``.

        Required and keyword-only, same contract as ``get_job``: the batch body
        carries status, totals and the full ``job_ids`` list, so an unscoped
        fetch hands a stranger a map of another tenant's work. A mismatch
        returns ``None`` so the caller answers 404, never 403.
        """
        result = await db.execute(
            select(CritiqueBatch).where(
                CritiqueBatch.batch_id == batch_id,
                CritiqueBatch.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def update_batch_status(self, db: AsyncSession, batch_id: str, *, tenant_id: str) -> None:
        """Update batch status from its constituent jobs, within ``tenant_id``."""
        batch = await self.get_batch(db, batch_id, tenant_id=tenant_id)
        if not batch:
            return

        completed = 0
        failed = 0
        # The batch row carries the owning tenant, so the constituent jobs are
        # counted within it — a job id that landed in another tenant's batch
        # (or a stale/foreign id) must not contribute to this batch's tallies.
        for jid in batch.job_ids:
            job = await self._get_job(db, jid, tenant_id=batch.tenant_id)
            if job:
                if job.status == "completed":
                    completed += 1
                elif job.status == "failed":
                    failed += 1

        batch.completed = completed
        batch.failed = failed
        if completed + failed >= batch.total:
            batch.status = "completed"
            batch.completed_at = datetime.now(UTC)
        elif completed + failed > 0:
            batch.status = "running"

        await db.commit()

    async def _get_job(
        self, db: AsyncSession, job_id: str, *, tenant_id: str
    ) -> CritiqueJob | None:
        """Fetch one job by id, constrained to ``tenant_id``.

        ``tenant_id`` used to be optional here "so that the one path with no
        tenant in hand can still be expressed explicitly" — but no such path
        exists any more. All five call sites already source a required ``str``:
        ``get_job``, ``mark_failed``, ``delete_job`` and ``process_job`` take a
        keyword-only ``tenant_id: str``, and ``update_batch_status`` uses the
        owning ``batch.tenant_id``. The default was therefore only ever a way
        to forget the filter, so it is gone and the predicate is unconditional.
        """
        result = await db.execute(
            select(CritiqueJob).where(
                CritiqueJob.job_id == job_id,
                CritiqueJob.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()
