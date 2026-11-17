"""Tests for brand_repository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domains.brand.services.brand_repository import BrandRepository


def _make_mock_session() -> AsyncMock:
    """Create a mock AsyncSession."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.delete = AsyncMock()
    return session


def _make_mock_brand(**kwargs):
    """Create a mock Brand object."""
    brand = MagicMock()
    brand.id = kwargs.get("id", 1)
    brand.tenant_id = kwargs.get("tenant_id", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")
    brand.domain = kwargs.get("domain", "example.com")
    brand.name = kwargs.get("name", "Example")
    brand.logo_url = kwargs.get("logo_url", "")
    brand.logo_asset_id = kwargs.get("logo_asset_id")
    brand.has_logo = kwargs.get("has_logo", False)
    brand.primary_color = kwargs.get("primary_color", "#2563EB")
    brand.secondary_color = kwargs.get("secondary_color", "#1E40AF")
    brand.fonts = kwargs.get("fonts")
    brand.color_palette = kwargs.get("color_palette")
    brand.social_images = kwargs.get("social_images")
    brand.source = kwargs.get("source", "url")
    brand.confidence_score = kwargs.get("confidence_score", 0.0)
    brand.version = kwargs.get("version", 1)
    return brand


class TestLogExtraction:
    @pytest.mark.asyncio
    async def test_creates_log_entry(self) -> None:
        db = _make_mock_session()
        await BrandRepository.log_extraction(
            db,
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            "https://example.com",
            "url",
            {"name": "Test"},
        )
        db.add.assert_called_once()
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_with_none_url(self) -> None:
        db = _make_mock_session()
        await BrandRepository.log_extraction(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", None, "analysis", {"name": "Test"}
        )
        db.add.assert_called_once()


class TestUpsertBrand:
    @pytest.mark.asyncio
    async def test_creates_new_brand(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        await BrandRepository.upsert_brand(
            db,
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            "example.com",
            {"name": "Example", "primary_color": "#FF0000"},
            source="url",
        )
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_updates_existing_brand(self) -> None:
        db = _make_mock_session()
        existing = _make_mock_brand(name="Old Name", version=1)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        db.execute.return_value = mock_result

        await BrandRepository.upsert_brand(
            db,
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            "example.com",
            {"name": "New Name"},
            source="url",
        )
        # Should increment version
        assert existing.version == 2
        # Should have added a BrandVersion snapshot
        assert db.add.called


class TestGetBrand:
    @pytest.mark.asyncio
    async def test_returns_brand(self) -> None:
        db = _make_mock_session()
        mock_brand = _make_mock_brand()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_brand
        db.execute.return_value = mock_result

        result = await BrandRepository.get_brand(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com"
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await BrandRepository.get_brand(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "notfound.com"
        )
        assert result is None


class TestListBrands:
    @pytest.mark.asyncio
    async def test_returns_brands_and_count(self) -> None:
        db = _make_mock_session()
        # First call for count
        count_result = MagicMock()
        count_result.scalar.return_value = 5
        # Second call for brands
        brands_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [_make_mock_brand(), _make_mock_brand()]
        brands_result.scalars.return_value = mock_scalars
        db.execute.side_effect = [count_result, brands_result]

        brands, total = await BrandRepository.list_brands(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", page=1, page_size=20
        )
        assert total == 5
        assert len(brands) == 2


class TestGetBrandHistory:
    @pytest.mark.asyncio
    async def test_returns_versions(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [MagicMock(), MagicMock()]
        mock_result.scalars.return_value = mock_scalars
        db.execute.return_value = mock_result

        history = await BrandRepository.get_brand_history(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com"
        )
        assert len(history) == 2


class TestDeleteBrand:
    @pytest.mark.asyncio
    async def test_deletes_existing(self) -> None:
        db = _make_mock_session()
        mock_brand = _make_mock_brand()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_brand
        db.execute.return_value = mock_result

        result = await BrandRepository.delete_brand(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com"
        )
        assert result is True
        db.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self) -> None:
        db = _make_mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await BrandRepository.delete_brand(
            db, "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "notfound.com"
        )
        assert result is False
