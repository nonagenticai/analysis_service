"""SQLAlchemy ORM model for the analyze-embedded review-job durable store.

The ``/api/analyze/review`` sub-review feature used to keep job state in an
in-memory ``TTLCache`` only (``app.state.review_jobs``). Under the service's
multi-replica deployment that made a create-on-podA / poll-on-podB sequence a
permanent 404. This table is the shared, durable backing store — mirrored by a
Redis write-through cache exactly like ``analysis_jobs`` — so the GET can fall
back to it from any replica.

This is DISTINCT from the separate DB-backed ``review`` domain
(``src/domains/review`` → ``review_jobs`` table). That domain is untouched;
this table (``analyze_review_jobs``) is owned solely by the analyze domain.

NOTE — deliberately a standalone module, NOT part of ``analyze/models.py``:
migration ``0005_rls`` imports ``src.domains.analyze.models`` at module scope to
render RLS from live ``Base.metadata``. Registering this table there would make
``0005`` try to ``ALTER TABLE analyze_review_jobs`` before ``0007`` creates it on
a fresh ``alembic upgrade``. Keeping it out of that import chain (the way
``icp_extractions`` stays out of ``0005``) avoids the ordering hazard; its RLS is
applied by ``0007`` (prod) and ``register_rls_ddl`` on ``create_all`` (tests).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
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


class AnalyzeReviewJob(Base):
    """Durable state for an analyze-embedded review job (``/api/analyze/review``)."""

    __tablename__ = "analyze_review_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    review_types: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    reviews: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_time_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
