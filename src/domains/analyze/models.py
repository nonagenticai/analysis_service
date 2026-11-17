"""SQLAlchemy ORM models for analyzer domain database tables."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
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


class AnalysisJob(Base):
    """Async analysis job tracking."""

    __tablename__ = "analysis_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    pages_input: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    pages_analyzed: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    site_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    narrative_extraction: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    results_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    analysis_artifacts: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    total_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AnalysisUsage(Base):
    """Usage tracking per tenant per day."""

    __tablename__ = "analysis_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    analysis_count: Mapped[int] = mapped_column(Integer, default=0)
    pages_analyzed: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


class UserContextProfile(Base):
    """Saved user context profiles for reuse across analyses."""

    __tablename__ = "user_context_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    profile_name: Mapped[str] = mapped_column(String(255))
    context_data: Mapped[dict] = mapped_column(JSONB)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
