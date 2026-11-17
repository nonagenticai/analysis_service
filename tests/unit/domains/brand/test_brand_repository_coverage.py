"""Extended tests for brand_repository.py -- covers logo asset CRUD operations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domains.brand.services.brand_repository import BrandRepository

# Every logo fixture in this module is seeded under "7eed672d-a7ff-5f16-87d7-b6a493fee523"; the repository's
# by-id lookups are tenant-scoped, so the reads must name that same tenant.
_LOGO_TENANT = "7eed672d-a7ff-5f16-87d7-b6a493fee523"


def _make_mock_session() -> AsyncMock:
    """Create a mock AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return session


class TestSaveLogoAsset:
    @pytest.mark.asyncio
    async def test_creates_logo_record(self) -> None:
        db = _make_mock_session()
        await BrandRepository.save_logo_asset(
            db,
            asset_id="asset-123",
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            domain="example.com",
            original_url="https://example.com/logo.png",
            storage_path="/tmp/logos/asset-123.png",
            content_type="image/png",
            size_bytes=1024,
            width=200,
            height=100,
        )
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_without_dimensions(self) -> None:
        db = _make_mock_session()
        await BrandRepository.save_logo_asset(
            db,
            asset_id="asset-456",
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            domain="example.com",
            original_url="https://example.com/logo.svg",
            storage_path="/tmp/logos/asset-456.svg",
            content_type="image/svg+xml",
            size_bytes=512,
        )
        db.add.assert_called_once()


class TestGetLogoAsset:
    @pytest.mark.asyncio
    async def test_returns_logo_asset(self) -> None:
        db = _make_mock_session()
        mock_record = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = mock_result

        result = await BrandRepository.get_logo_asset(db, "asset-123", tenant_id=_LOGO_TENANT)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await BrandRepository.get_logo_asset(db, "missing-asset", tenant_id=_LOGO_TENANT)
        assert result is None


class TestGetLogoForDomain:
    @pytest.mark.asyncio
    async def test_returns_latest_logo(self) -> None:
        db = _make_mock_session()
        mock_record = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = mock_result

        result = await BrandRepository.get_logo_for_domain(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com"
        )
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_returns_none_when_no_logo(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await BrandRepository.get_logo_for_domain(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "nologo.com"
        )
        assert result is None


class TestDeleteLogoAsset:
    @pytest.mark.asyncio
    async def test_deletes_existing_logo(self) -> None:
        db = _make_mock_session()
        mock_record = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = mock_result

        result = await BrandRepository.delete_logo_asset(db, "asset-123", tenant_id=_LOGO_TENANT)
        assert result is True
        db.delete.assert_called_once_with(mock_record)
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await BrandRepository.delete_logo_asset(
            db, "missing-asset", tenant_id=_LOGO_TENANT
        )
        assert result is False


class TestUpsertBrandNoChanges:
    """Test upsert_brand when no fields changed."""

    @pytest.mark.asyncio
    async def test_no_version_increment_without_changes(self) -> None:
        db = _make_mock_session()
        existing = MagicMock()
        existing.id = 1
        existing.tenant_id = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        existing.domain = "example.com"
        existing.name = "Example"
        existing.logo_url = ""
        existing.has_logo = False
        existing.primary_color = "#2563EB"
        existing.secondary_color = "#1E40AF"
        existing.fonts = None
        existing.color_palette = None
        existing.social_images = None
        existing.source = "url"
        existing.confidence_score = 0.5
        existing.tagline = ""
        existing.brand_voice = ""
        existing.social_profiles = None
        existing.industry = None
        existing.content_analysis = None
        existing.logo_intelligence = None
        existing.quality_score = None
        existing.extraction_methods = None
        existing.font_family_primary = ""
        existing.font_family_secondary = ""
        existing.version = 3

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        db.execute.return_value = mock_result

        # Pass same data as existing -- no changes
        await BrandRepository.upsert_brand(
            db,
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            "example.com",
            {"name": "Example", "primary_color": "#2563EB"},
            source="url",
        )
        # Version should not change (no changed_fields)
        assert existing.version == 3
