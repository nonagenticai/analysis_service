"""SQLAlchemy ORM models for brand extraction."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base import Base


class BrandExtractionLog(Base):
    """Log of brand extractions for auditing."""

    __tablename__ = "brand_extraction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20))  # "analysis" or "url"
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Brand(Base):
    """Canonical brand record for a tenant+domain."""

    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("tenant_id", "domain", name="uq_brand_tenant_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(String(512), default="")
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    logo_asset_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_logo: Mapped[bool] = mapped_column(Boolean, default=False)
    primary_color: Mapped[str] = mapped_column(String(20), default="#2563EB")
    secondary_color: Mapped[str] = mapped_column(String(20), default="#1E40AF")
    fonts: Mapped[list | None] = mapped_column(JSONB, nullable=True)  # list of FontInfo dicts
    color_palette: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # ColorPalette dict
    social_images: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # SocialImages dict
    extraction_metadata: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )  # ExtractionMetadata dict
    source: Mapped[str] = mapped_column(String(20), default="url")  # "analysis" or "url"
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Round 2 fields
    tagline: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    brand_voice: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    social_profiles: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    industry: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    logo_intelligence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    quality_score: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extraction_methods: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    font_family_primary: Mapped[str | None] = mapped_column(String(255), nullable=True, default="")
    font_family_secondary: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=""
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    versions: Mapped[list[BrandVersion]] = relationship(
        "BrandVersion", back_populates="brand", lazy="selectin"
    )


class BrandVersion(Base):
    """Historical snapshot of brand data at a specific version."""

    __tablename__ = "brand_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("brands.id", ondelete="CASCADE"),
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str] = mapped_column(String(512))
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSONB)  # Full brand snapshot
    changed_fields: Mapped[list | None] = mapped_column(
        JSONB, nullable=True
    )  # List of fields that changed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    brand: Mapped[Brand] = relationship("Brand", back_populates="versions")


class BrandOverrideRecord(Base):
    """Record of a manual brand override."""

    __tablename__ = "brand_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str] = mapped_column(String(512), index=True)
    overrides: Mapped[dict] = mapped_column(JSONB)
    applied_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LogoAssetRecord(Base):
    """Record of a stored logo file."""

    __tablename__ = "logo_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str] = mapped_column(String(512))
    original_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(100), default="image/png")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
