"""SQLAlchemy ORM model for ICP extractions (audit trail).

One row per successful ``/api/brand/icp-from-url`` extraction, scoped by
``tenant_id`` so Row-Level Security isolates tenants (policy auto-rendered from
``w2v_common.database.rls`` on the ``tenant_id`` column; see migration
``0006_icp_extractions``). Persistence is best-effort — a write failure never
fails the request — so this table is a history/audit convenience, not on the
request's critical path.
"""

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


class IcpExtraction(Base):
    """A persisted ICP extraction result.

    Attributes:
        id: Surrogate primary key.
        tenant_id: Owning tenant (RLS predicate column).
        domain: Bare domain the extraction was keyed on (``www.`` stripped).
        url: The exact URL requested.
        response: The full flat ICP response object as returned to recon.
        confidence: Grounded-key coverage of the extraction (0-1).
        created_at: Row creation timestamp.
    """

    __tablename__ = "icp_extractions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
