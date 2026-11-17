"""Background job cleanup service.

Periodically removes completed/failed review jobs older than the retention period.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.shared.database.session import get_db_session

logger = logging.getLogger(__name__)


async def cleanup_old_jobs(
    retention_hours: int = 72,
    interval_minutes: int = 60,
) -> None:
    """Background task that periodically cleans up old completed/failed jobs.

    Args:
        retention_hours: Hours to retain completed/failed jobs before deletion.
        interval_minutes: Minutes between cleanup runs.
    """
    logger.info(
        "Job cleanup started: retention=%dh, interval=%dm",
        retention_hours,
        interval_minutes,
    )
    # Work FIRST, sleep after. A sleep-first loop never completes a single pass
    # in a pod that restarts more often than ``interval_minutes`` -- which is the
    # normal case during a rollout -- so the prune silently never runs.
    while True:
        try:
            cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
            # Cross-tenant prune via the SECURITY DEFINER resolver (bypasses RLS):
            # this background loop runs with no tenant GUC, so under enforce a plain
            # DELETE on the non-bypass runtime role would fail-closed (delete 0). The
            # resolver is owned by the migration user and prunes across all tenants.
            async with get_db_session() as db:
                deleted = (
                    await db.execute(text("SELECT prune_review_jobs(:cutoff)"), {"cutoff": cutoff})
                ).scalar() or 0
                if deleted:
                    # `prune_review_jobs` spans three tables since migration 0014
                    # (review_jobs + analyze_review_jobs + review_batches); the
                    # return value is their combined row count.
                    logger.info(
                        "Cleaned up %d old review rows across review_jobs, "
                        "analyze_review_jobs and review_batches (older than %s)",
                        deleted,
                        cutoff,
                    )
        except asyncio.CancelledError:
            logger.info("Job cleanup task cancelled")
            break
        except Exception as e:
            logger.error("Job cleanup error: %s", e, exc_info=True)
        try:
            await asyncio.sleep(interval_minutes * 60)
        except asyncio.CancelledError:
            logger.info("Job cleanup task cancelled")
            break
