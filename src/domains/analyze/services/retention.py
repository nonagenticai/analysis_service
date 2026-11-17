"""Data retention cleanup for ``analysis_jobs``.

Before migration 0015 this table had NO retention of any kind: 0005 gave the
critique domain ``prune_critique_data`` and the review domain
``prune_review_jobs``, and ``analysis_jobs`` was in neither, so every analysis
ever run — results JSONB and all — stayed on disk forever.

Deliberately driven by the EXISTING ``settings.retention`` group rather than a new
``ANALYZE_RETENTION_`` one. "Do we delete old terminal job rows, and after how
many days" is one operator policy for this service, already expressed by
``RETENTION_ENABLED`` / ``RETENTION_JOB_RETENTION_DAYS`` and already governing
``prune_critique_data``. A second switch would let the two silently diverge and
would make ``RETENTION_ENABLED=false`` a lie.

⚠️ The terminal set includes ``'cancelled'``. ``AnalysisJobRepository.cancel()``
is the only writer of that literal anywhere in ``src/``, and before 0015 no
retention path in EITHER domain matched it — a cancelled job was as immortal as
a stranded one. It is NOT added to the critique or review predicates, where it
would be dead SQL.

The cross-tenant DELETE is routed through the ``prune_analysis_jobs`` SECURITY
DEFINER resolver (owner bypasses RLS): this loop runs with no tenant GUC, so
under ``TENANT_RLS_MODE=enforce`` a plain DELETE on the non-bypass runtime role
would fail-closed and delete 0 while logging nothing wrong.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings

logger = logging.getLogger(__name__)

_PRUNE_SQL = text("SELECT prune_analysis_jobs(:cutoff)")


async def prune_old_analysis_jobs(db: AsyncSession) -> int:
    """Delete terminal analysis jobs older than the configured retention period.

    Returns the number of rows deleted, across all tenants.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.retention.job_retention_days)

    deleted = int((await db.execute(_PRUNE_SQL, {"cutoff": cutoff})).scalar() or 0)
    await db.commit()
    if deleted:
        logger.info(
            "Analysis retention deleted %d terminal analysis_jobs row(s) older than %s",
            deleted,
            cutoff,
        )
    return deleted


async def run_analyze_retention_loop() -> None:
    """Background loop that prunes old terminal analysis jobs at configured intervals.

    Work FIRST, sleep after — a sleep-first loop never completes a single pass in a
    pod that restarts more often than the interval, which is the normal case during
    a rollout, so the prune would silently never run.
    """
    settings = get_settings()
    if not settings.retention.enabled:
        logger.info("Analysis data retention is disabled")
        return

    interval = settings.retention.cleanup_interval_hours * 3600
    logger.info(
        "Analysis retention loop started: jobs=%dd, interval=%dh",
        settings.retention.job_retention_days,
        settings.retention.cleanup_interval_hours,
    )

    while True:
        try:
            from src.shared.database.session import get_db_session

            async with get_db_session() as db:
                await prune_old_analysis_jobs(db)
        except asyncio.CancelledError:
            logger.info("Analysis retention loop cancelled")
            raise
        except Exception as exc:
            logger.error("Analysis retention cleanup failed: %s", exc, exc_info=True)

        await asyncio.sleep(interval)
