"""Brand data repository.

Data access layer for brand database operations.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.brand.models import Brand, BrandExtractionLog, BrandVersion, LogoAssetRecord

logger = logging.getLogger(__name__)


class BrandRepository:
    """Data access layer for brand operations."""

    @staticmethod
    async def log_extraction(
        db: AsyncSession,
        tenant_id: str,
        url: str | None,
        source: str,
        result: dict[str, Any],
    ) -> BrandExtractionLog:
        """Log a brand extraction event to the audit table.

        Args:
            db: Async database session.
            tenant_id: Tenant identifier.
            url: Source URL, or None for analysis-based extractions.
            source: Extraction source type ("analysis", "url", "batch_url").
            result: Extraction result data as a JSON-serializable dict.

        Returns:
            The created BrandExtractionLog record.
        """
        log = BrandExtractionLog(
            tenant_id=tenant_id,
            url=url,
            source=source,
            result=result,
        )
        db.add(log)
        await db.flush()
        logger.debug("Extraction logged, tenant=%s, source=%s, url=%s", tenant_id, source, url)
        return log

    @staticmethod
    async def upsert_brand(
        db: AsyncSession,
        tenant_id: str,
        domain: str,
        brand_data: dict[str, Any],
        source: str = "url",
    ) -> Brand:
        """Create or update a brand record with version tracking.

        If brand exists: detect changed fields, create BrandVersion snapshot,
        update changed fields, and increment version. If new: create Brand
        with version=1.

        Args:
            db: Async database session.
            tenant_id: Tenant identifier.
            domain: Brand domain.
            brand_data: Full brand data as a JSON-serializable dict.
            source: How the data was obtained ("url", "analysis", "override").

        Returns:
            The created or updated Brand record.
        """
        # Check if brand exists
        stmt = select(Brand).where(Brand.tenant_id == tenant_id, Brand.domain == domain)
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            # Detect changed fields
            changed_fields = []
            for field_name in (
                "name",
                "logo_url",
                "has_logo",
                "primary_color",
                "secondary_color",
                "fonts",
                "color_palette",
                "social_images",
                "source",
                "confidence_score",
                "tagline",
                "brand_voice",
                "social_profiles",
                "industry",
                "content_analysis",
                "logo_intelligence",
                "quality_score",
                "extraction_methods",
                "font_family_primary",
                "font_family_secondary",
            ):
                old_val = getattr(existing, field_name, None)
                new_val = brand_data.get(field_name)
                if new_val is not None and old_val != new_val:
                    changed_fields.append(field_name)

            if changed_fields:
                # Create version snapshot of current state
                snapshot = {
                    "name": existing.name,
                    "domain": existing.domain,
                    "logo_url": existing.logo_url,
                    "has_logo": existing.has_logo,
                    "primary_color": existing.primary_color,
                    "secondary_color": existing.secondary_color,
                    "source": existing.source,
                    "version": existing.version,
                }
                version = BrandVersion(
                    brand_id=existing.id,
                    tenant_id=tenant_id,
                    domain=domain,
                    version=existing.version,
                    data=snapshot,
                    changed_fields=changed_fields,
                )
                db.add(version)

                # Update brand
                for field_name in changed_fields:
                    if field_name in brand_data:
                        setattr(existing, field_name, brand_data[field_name])
                existing.version += 1
                existing.source = source

            logger.debug(
                "Brand updated, domain=%s, version=%d, changed=%s",
                domain,
                existing.version,
                changed_fields,
            )
            await db.flush()
            return existing

        # Create new brand
        brand = Brand(
            tenant_id=tenant_id,
            domain=domain,
            name=brand_data.get("name", ""),
            logo_url=brand_data.get("logo_url", ""),
            logo_asset_id=brand_data.get("logo_asset_id"),
            has_logo=brand_data.get("has_logo", False),
            primary_color=brand_data.get("primary_color", "#2563EB"),
            secondary_color=brand_data.get("secondary_color", "#1E40AF"),
            fonts=brand_data.get("fonts"),
            color_palette=brand_data.get("color_palette"),
            social_images=brand_data.get("social_images"),
            source=source,
            confidence_score=brand_data.get("confidence_score", 0.0),
            tagline=brand_data.get("tagline", ""),
            brand_voice=brand_data.get("brand_voice", ""),
            social_profiles=brand_data.get("social_profiles"),
            industry=brand_data.get("industry"),
            content_analysis=brand_data.get("content_analysis"),
            logo_intelligence=brand_data.get("logo_intelligence"),
            quality_score=brand_data.get("quality_report") or brand_data.get("quality_score"),
            extraction_methods=brand_data.get("extraction_methods"),
            font_family_primary=brand_data.get("font_family_primary", ""),
            font_family_secondary=brand_data.get("font_family_secondary", ""),
            version=1,
        )
        db.add(brand)
        await db.flush()
        logger.debug("Brand created, domain=%s, tenant=%s", domain, tenant_id)
        return brand

    @staticmethod
    async def get_brand(db: AsyncSession, tenant_id: str, domain: str) -> Brand | None:
        """Get a brand by tenant and domain."""
        stmt = select(Brand).where(Brand.tenant_id == tenant_id, Brand.domain == domain)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def list_brands(
        db: AsyncSession,
        tenant_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Brand], int]:
        """List brands for a tenant with pagination.

        Returns (brands, total_count).
        """
        # Count
        count_stmt = select(func.count(Brand.id)).where(Brand.tenant_id == tenant_id)
        count_result = await db.execute(count_stmt)
        total = count_result.scalar() or 0

        # Fetch page
        offset = (page - 1) * page_size
        stmt = (
            select(Brand)
            .where(Brand.tenant_id == tenant_id)
            .order_by(Brand.updated_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        result = await db.execute(stmt)
        brands = list(result.scalars().all())

        return brands, total

    @staticmethod
    async def get_brand_history(
        db: AsyncSession, tenant_id: str, domain: str
    ) -> list[BrandVersion]:
        """Get version history for a brand."""
        stmt = (
            select(BrandVersion)
            .where(BrandVersion.tenant_id == tenant_id, BrandVersion.domain == domain)
            .order_by(BrandVersion.version.desc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def delete_brand(db: AsyncSession, tenant_id: str, domain: str) -> bool:
        """Delete a brand and its versions (cascade)."""
        stmt = select(Brand).where(Brand.tenant_id == tenant_id, Brand.domain == domain)
        result = await db.execute(stmt)
        brand = result.scalar_one_or_none()
        if brand:
            await db.delete(brand)
            await db.flush()
            return True
        return False

    @staticmethod
    async def save_logo_asset(
        db: AsyncSession,
        asset_id: str,
        tenant_id: str,
        domain: str,
        original_url: str,
        storage_path: str,
        content_type: str,
        size_bytes: int,
        width: int | None = None,
        height: int | None = None,
    ) -> LogoAssetRecord:
        """Save a logo asset record to the database.

        Args:
            db: Async database session.
            asset_id: Unique asset identifier.
            tenant_id: Tenant identifier.
            domain: Domain the logo belongs to.
            original_url: Original download URL.
            storage_path: Local or MinIO storage path.
            content_type: MIME type of the image.
            size_bytes: File size in bytes.
            width: Optional image width in pixels.
            height: Optional image height in pixels.

        Returns:
            The created LogoAssetRecord.
        """
        record = LogoAssetRecord(
            asset_id=asset_id,
            tenant_id=tenant_id,
            domain=domain,
            original_url=original_url,
            storage_path=storage_path,
            content_type=content_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
        )
        db.add(record)
        await db.flush()
        return record

    @staticmethod
    async def get_logo_asset(
        db: AsyncSession, asset_id: str, *, tenant_id: str
    ) -> LogoAssetRecord | None:
        """Get a logo asset by asset_id, scoped to ``tenant_id``.

        Required and keyword-only: the asset id is the whole address of the
        stored bytes, so an unscoped lookup serves (and, via the delete route,
        destroys) any tenant's logo to anyone holding the id. ``None`` on
        mismatch so the caller answers 404, never 403.
        """
        stmt = select(LogoAssetRecord).where(
            LogoAssetRecord.asset_id == asset_id,
            LogoAssetRecord.tenant_id == tenant_id,
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def get_logo_for_domain(
        db: AsyncSession, tenant_id: str, domain: str
    ) -> LogoAssetRecord | None:
        """Get the most recent logo asset for a domain."""
        stmt = (
            select(LogoAssetRecord)
            .where(LogoAssetRecord.tenant_id == tenant_id, LogoAssetRecord.domain == domain)
            .order_by(LogoAssetRecord.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    async def delete_logo_asset(db: AsyncSession, asset_id: str, *, tenant_id: str) -> bool:
        """Delete a logo asset record owned by ``tenant_id``."""
        stmt = select(LogoAssetRecord).where(
            LogoAssetRecord.asset_id == asset_id,
            LogoAssetRecord.tenant_id == tenant_id,
        )
        result = await db.execute(stmt)
        record = result.scalar_one_or_none()
        if record:
            await db.delete(record)
            await db.flush()
            return True
        return False
