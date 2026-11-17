"""Tests for icp_cache_service (mirror of brand_cache_service tests)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.domains.icp.schemas import ICPResponse
from src.domains.icp.services.icp_cache_service import IcpCacheService


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    return redis


def _make_cache(mock_redis: AsyncMock, *, enabled: bool = True) -> IcpCacheService:
    with patch("src.domains.icp.services.icp_cache_service.get_settings") as mock_settings:
        mock_cache = type(
            "CacheSettings", (), {"icp_ttl": 3600, "enabled": enabled, "key_prefix": "analysis"}
        )()
        mock_settings.return_value = type("Settings", (), {"cache": mock_cache})()
        return IcpCacheService(redis_client=mock_redis)


@pytest.fixture
def cache_service(mock_redis: AsyncMock) -> IcpCacheService:
    return _make_cache(mock_redis)


class TestMakeKey:
    def test_consistent_key(self, cache_service: IcpCacheService) -> None:
        assert cache_service._make_key(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com"
        ) == cache_service._make_key("7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com")

    def test_different_tenants_different_keys(self, cache_service: IcpCacheService) -> None:
        assert cache_service._make_key(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com"
        ) != cache_service._make_key("t2", "example.com")

    def test_key_is_icp_namespaced(self, cache_service: IcpCacheService) -> None:
        # The ``:icp:`` namespace keeps ICP entries distinct from brand cache
        # entries under the same prefix/tenant/domain.
        assert cache_service._make_key(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com"
        ).startswith("analysis:icp:7eed672d-a7ff-5f16-87d7-b6a493fee523:")


class TestGetSet:
    async def test_returns_none_on_miss(
        self, cache_service: IcpCacheService, mock_redis: AsyncMock
    ) -> None:
        assert (
            await cache_service.get("7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com") is None
        )

    async def test_roundtrip_hit(
        self, cache_service: IcpCacheService, mock_redis: AsyncMock
    ) -> None:
        icp = ICPResponse(company="Acme", value_props=["fast"], confidence=0.18)
        mock_redis.get = AsyncMock(return_value=icp.model_dump(mode="json"))
        result = await cache_service.get_by_url(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "https://www.example.com/pricing"
        )
        assert result is not None
        assert result.company == "Acme"
        assert result.value_props == ["fast"]

    async def test_get_by_url_strips_www_and_path(
        self, cache_service: IcpCacheService, mock_redis: AsyncMock
    ) -> None:
        # www + path collapse to the same bare domain key as the set() path.
        await cache_service.get_by_url(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "https://www.example.com/pricing"
        )
        used_key = mock_redis.get.await_args.args[0]
        assert used_key == cache_service._make_key(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com"
        )

    async def test_set_uses_ttl(
        self, cache_service: IcpCacheService, mock_redis: AsyncMock
    ) -> None:
        await cache_service.set(
            "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com", ICPResponse(company="Acme")
        )
        _key, _value, ttl = mock_redis.set.await_args.args
        assert ttl == 3600

    async def test_disabled_is_noop(self, mock_redis: AsyncMock) -> None:
        disabled = _make_cache(mock_redis, enabled=False)
        assert await disabled.get("7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com") is None
        assert (
            await disabled.set(
                "7eed672d-a7ff-5f16-87d7-b6a493fee523", "example.com", ICPResponse(company="Acme")
            )
            is False
        )
        mock_redis.get.assert_not_awaited()
        mock_redis.set.assert_not_awaited()
