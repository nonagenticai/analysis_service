"""SQLAlchemy ORM models for review domain."""

import datetime as dt
from datetime import datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

# ``tenant_id`` is a native Postgres ``uuid`` (migration 0013), declared with
# **as_uuid=False**. That flag is LOAD-BEARING, not style:
#
# * ``as_uuid=True`` returns ``uuid.UUID`` objects, and ``uuid.UUID(t) == t`` is
#   False for every string ``t``. This repo carries the tenant as ``str`` in ~118
#   annotations across 31 files, so every ``row.tenant_id == some_str`` ownership
#   guard would silently evaluate False — 404ing the owner on their own rows —
#   and every scope predicate would match nothing. ``==`` between ``UUID`` and
#   ``str`` is not a type error, so ty and ruff stay green while prod breaks.
# * ``bind_session_tenant`` ends in ``set_config('app.current_tenant', $2, true)``,
#   which is ``(text, text, bool)`` with no uuid overload; a ``uuid.UUID`` there
#   raises ``UndefinedFunction`` and kills every transaction under RLS.
#
# With ``as_uuid=False`` the Python surface does not change at all — the
# ``Mapped[str]`` / ``Mapped[str | None]`` annotations below are still exactly
# right — and only the Postgres column type moves ``varchar(255)`` -> ``uuid``.
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base


class ReviewJob(Base):
    """Async review job tracking."""

    __tablename__ = "review_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    review_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Phase 2: User context and URL tracking
    user_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class ReviewUsage(Base):
    """Daily usage counters per tenant for review and error totals."""

    __tablename__ = "review_usage"
    __table_args__ = (UniqueConstraint("tenant_id", "date", name="uq_usage_tenant_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    review_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


class ReviewBatch(Base):
    """Tracks a batch of review jobs with aggregate completion state."""

    __tablename__ = "review_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    job_ids: Mapped[list] = mapped_column(JSONB, server_default="[]")
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
