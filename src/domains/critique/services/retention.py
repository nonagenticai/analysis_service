"""Data retention cleanup service."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings

logger = logging.getLogger(__name__)


async def cleanup_old_data(db: AsyncSession) -> dict[str, int]:
    """Delete data older than configured retention periods (across all tenants).

    Routes the cross-tenant deletes through the ``prune_critique_data`` SECURITY
    DEFINER resolver (owner bypasses RLS): this runs in a background loop with no
    tenant GUC, so under enforce a plain DELETE on the non-bypass runtime role would
    fail-closed (delete 0). Returns counts of deleted rows per table.
    """
    settings = get_settings()
    job_cutoff = datetime.now(UTC) - timedelta(days=settings.retention.job_retention_days)
    audit_cutoff = datetime.now(UTC) - timedelta(days=settings.retention.audit_retention_days)

    result = (
        await db.execute(
            text("SELECT prune_critique_data(:job_cutoff, :audit_cutoff)"),
            {"job_cutoff": job_cutoff, "audit_cutoff": audit_cutoff},
        )
    ).scalar()
    await db.commit()
    counts: dict[str, int] = dict(result) if isinstance(result, dict) else {}
    logger.info("Retention cleanup completed: %s", counts)
    return counts


async def run_retention_loop() -> None:
    """Background loop that runs cleanup at configured intervals."""
    settings = get_settings()
    if not settings.retention.enabled:
        logger.info("Data retention is disabled")
        return

    interval = settings.retention.cleanup_interval_hours * 3600
    logger.info(
        "Retention loop started: jobs=%dd, audit=%dd, interval=%dh",
        settings.retention.job_retention_days,
        settings.retention.audit_retention_days,
        settings.retention.cleanup_interval_hours,
    )

    while True:
        try:
            from src.shared.database.session import get_db_session

            async with get_db_session() as db:
                await cleanup_old_data(db)
        except Exception as e:
            logger.error("Retention cleanup failed: %s", e, exc_info=True)

        await asyncio.sleep(interval)
