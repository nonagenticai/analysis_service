"""Audit logging for critique_service.

Records events for compliance and debugging.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.models import AuditLog

logger = logging.getLogger(__name__)


async def log_event(
    db: AsyncSession,
    *,
    event_type: str,
    tenant_id: str | None = None,
    job_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Log an audit event to DB and structured log.

    Event types: job_created, job_started, job_completed, job_failed,
                 job_cached, rate_limited, batch_created.
    """
    try:
        audit = AuditLog(
            event_type=event_type,
            tenant_id=tenant_id,
            job_id=job_id,
            details=details,
            created_at=datetime.now(UTC),
        )
        db.add(audit)
        # Don't commit here - let the caller manage the transaction

        logger.info(
            "AUDIT: %s tenant=%s job=%s %s",
            event_type,
            tenant_id or "-",
            job_id or "-",
            details or "",
        )
    except (OSError, SQLAlchemyError) as e:
        logger.error("Failed to log audit event: %s", e)
