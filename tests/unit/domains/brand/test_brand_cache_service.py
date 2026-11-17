"""Tests for brand_cache_service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.brand_cache_service import BrandCacheService


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=False)
    return redis


@pytest.fixture
def cache_service(mock_redis: AsyncMock) -> BrandCacheService:
    with patch("src.domains.brand.services.brand_cache_service.get_settings") as mock_settings:
        mock_cache = type(
            "CacheSettings", (), {"brand_ttl": 3600, "enabled": True, "key_prefix": "brand"}
        )()
        mock_settings.return_value = type("Settings", (), {"cache": mock_cache})()
        return BrandCacheService(redis_client=mock_redis)


class TestMakeKey:
    def test_consistent_key(self, cache_service: BrandCacheService) -> None:
        key1 = cache_service._make_key("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
        key2 = cache_service._make_key("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
        assert key1 == key2

    def test_different_tenants_different_keys(self, cache_service: BrandCacheService) -> None:
        key1 = cache_service._make_key("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
        key2 = cache_service._make_key("2fbf8d17-6d40-59df-b603-a3f7542cb84a", "example.com")
        assert key1 != key2

    def test_key_format(self, cache_service: BrandCacheService) -> None:
        key = cache_service._make_key("7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com")
        assert key.startswith("brand:7eed672d-a7ff-5f16-87d7-b6a493fee523:")


class TestGet:
    @pytest.mark.asyncio
    async def test_returns_none_on_miss(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        result = await cache_service.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_brand_on_hit(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        brand_data = {
            "name": "Acme",
            "domain": "acme.com",
            "primary_color": "#FF0000",
            "secondary_color": "#CC0000",
            "has_logo": False,
            "logo_url": "",
        }
        mock_redis.get.return_value = brand_data
        result = await cache_service.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "acme.com")
        assert result is not None
        assert result.name == "Acme"
        assert result.domain == "acme.com"


class TestGetByUrl:
    @pytest.mark.asyncio
    async def test_extracts_domain(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        brand_data = {
            "name": "Test",
            "domain": "test.com",
            "primary_color": "#000",
            "secondary_color": "#000",
            "has_logo": False,
            "logo_url": "",
        }
        mock_redis.get.return_value = brand_data
        result = await cache_service.get_by_url(
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "https://www.test.com/page"
        )
        assert result is not None
        assert result.name == "Test"


class TestSet:
    @pytest.mark.asyncio
    async def test_sets_with_default_ttl(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        brand = BrandResponse(name="Test", domain="test.com")
        result = await cache_service.set("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "test.com", brand)
        assert result is True
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args[1].get("ttl") == 3600 or call_args[0][2] == 3600

    @pytest.mark.asyncio
    async def test_sets_with_custom_ttl(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        brand = BrandResponse(name="Test", domain="test.com")
        await cache_service.set("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "test.com", brand, ttl=60)
        call_args = mock_redis.set.call_args
        # The TTL should be 60, not the default
        assert 60 in call_args[0] or call_args[1].get("ttl") == 60


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate(
        self, cache_service: BrandCacheService, mock_redis: AsyncMock
    ) -> None:
        result = await cache_service.invalidate(
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com"
        )
        assert result is True
        mock_redis.delete.assert_called_once()


class TestDisabled:
    @pytest.mark.asyncio
    async def test_get_returns_none_when_disabled(self, mock_redis: AsyncMock) -> None:
        with patch("src.domains.brand.services.brand_cache_service.get_settings") as mock_settings:
            mock_cache = type(
                "CacheSettings", (), {"brand_ttl": 3600, "enabled": False, "key_prefix": "brand"}
            )()
            mock_settings.return_value = type("Settings", (), {"cache": mock_cache})()
            service = BrandCacheService(redis_client=mock_redis)
            result = await service.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
            assert result is None
            mock_redis.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_returns_false_when_disabled(self, mock_redis: AsyncMock) -> None:
        with patch("src.domains.brand.services.brand_cache_service.get_settings") as mock_settings:
            mock_cache = type(
                "CacheSettings", (), {"brand_ttl": 3600, "enabled": False, "key_prefix": "brand"}
            )()
            mock_settings.return_value = type("Settings", (), {"cache": mock_cache})()
            service = BrandCacheService(redis_client=mock_redis)
            brand = BrandResponse(name="Test", domain="test.com")
            result = await service.set("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "test.com", brand)
            assert result is False
