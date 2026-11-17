"""Extended tests for scan_cache module."""

from unittest.mock import AsyncMock, patch

from src.domains.critique.services.scan_cache import (
    _compute_cache_key,
    _compute_scanner_cache_key,
    get_cached_result,
    get_scanner_cached_result,
    invalidate_domain_cache,
    set_cached_result,
    set_scanner_cached_result,
)

_T = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"


class TestComputeCacheKey:
    """Test cache key computation."""

    def test_same_url_same_key(self):
        k1 = _compute_cache_key(_T, "https://example.com", ["seo"])
        k2 = _compute_cache_key(_T, "https://example.com", ["seo"])
        assert k1 == k2

    def test_different_url_different_key(self):
        k1 = _compute_cache_key(_T, "https://a.com", ["seo"])
        k2 = _compute_cache_key(_T, "https://b.com", ["seo"])
        assert k1 != k2

    def test_categories_sorted(self):
        k1 = _compute_cache_key(_T, "https://a.com", ["seo", "security"])
        k2 = _compute_cache_key(_T, "https://a.com", ["security", "seo"])
        assert k1 == k2

    def test_none_categories(self):
        k1 = _compute_cache_key(_T, "https://a.com", None)
        k2 = _compute_cache_key(_T, "https://a.com", None)
        assert k1 == k2

    def test_key_has_prefix(self):
        key = _compute_cache_key(_T, "https://a.com")
        assert key.startswith("critique:cache:")


class TestGetCachedResult:
    """Test get_cached_result function."""

    async def test_cache_disabled(self):
        with patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings:
            mock_settings.return_value.cache.enabled = False
            result = await get_cached_result(_T, "https://a.com")
        assert result is None

    async def test_cache_hit(self):
        cached_data = {"findings": [], "scores": {}}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=cached_data)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await get_cached_result(_T, "https://a.com", ["seo"])

        assert result == cached_data

    async def test_cache_miss(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await get_cached_result(_T, "https://a.com", ["seo"])

        assert result is None

    async def test_cache_error(self):
        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                side_effect=OSError("Redis down"),
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await get_cached_result(_T, "https://a.com")

        assert result is None


class TestSetCachedResult:
    """Test set_cached_result function."""

    async def test_cache_disabled(self):
        with patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings:
            mock_settings.return_value.cache.enabled = False
            result = await set_cached_result(_T, "https://a.com", ["seo"], {"data": 1})
        assert result is False

    async def test_cache_set_success(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            mock_settings.return_value.cache.ttl = 3600
            result = await set_cached_result(_T, "https://a.com", ["seo"], {"data": 1})

        assert result is True
        mock_client.set.assert_awaited_once()

    async def test_cache_set_error(self):
        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                side_effect=OSError("Redis down"),
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await set_cached_result(_T, "https://a.com", None, {"data": 1})

        assert result is False


class TestScannerCacheKey:
    """Test per-scanner cache key computation."""

    def test_different_scanners_different_keys(self):
        k1 = _compute_scanner_cache_key(_T, "https://a.com", "seo")
        k2 = _compute_scanner_cache_key(_T, "https://a.com", "a11y")
        assert k1 != k2

    def test_same_scanner_same_key(self):
        k1 = _compute_scanner_cache_key(_T, "https://a.com", "seo")
        k2 = _compute_scanner_cache_key(_T, "https://a.com", "seo")
        assert k1 == k2

    def test_instructions_affect_key(self):
        k1 = _compute_scanner_cache_key(_T, "https://a.com", "ux", "focus on mobile")
        k2 = _compute_scanner_cache_key(_T, "https://a.com", "ux", "focus on desktop")
        assert k1 != k2

    def test_none_instructions_matches_no_instructions(self):
        k1 = _compute_scanner_cache_key(_T, "https://a.com", "seo")
        k2 = _compute_scanner_cache_key(_T, "https://a.com", "seo", None)
        assert k1 == k2

    def test_has_scanner_prefix(self):
        key = _compute_scanner_cache_key(_T, "https://a.com", "seo")
        assert key.startswith("critique:scanner:")


class TestGetScannerCachedResult:
    """Test per-scanner cache retrieval."""

    async def test_cache_hit(self):
        findings = [{"title": "Missing title", "severity": "high"}]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=findings)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await get_scanner_cached_result(_T, "https://a.com", "seo")

        assert result == findings

    async def test_cache_miss(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            result = await get_scanner_cached_result(_T, "https://a.com", "seo")

        assert result is None

    async def test_cache_disabled(self):
        with patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings:
            mock_settings.return_value.cache.enabled = False
            result = await get_scanner_cached_result(_T, "https://a.com", "seo")
        assert result is None


class TestSetScannerCachedResult:
    """Test per-scanner cache storage."""

    async def test_set_success_with_domain_tracking(self):
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)
        mock_client.sadd = AsyncMock(return_value=1)
        mock_client.expire = AsyncMock(return_value=True)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            mock_settings.return_value.cache.scanner_ttl = 3600
            mock_settings.return_value.cache.domain_invalidation_enabled = True
            result = await set_scanner_cached_result(
                _T, "https://example.com/page", "seo", [{"title": "test"}]
            )

        assert result is True
        mock_client.sadd.assert_awaited_once()
        mock_client.expire.assert_awaited_once()

    async def test_cache_disabled(self):
        with patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings:
            mock_settings.return_value.cache.enabled = False
            result = await set_scanner_cached_result(
                _T, "https://a.com", "seo", [{"title": "test"}]
            )
        assert result is False


class TestInvalidateDomainCache:
    """Test domain-level cache invalidation."""

    async def test_invalidate_domain(self):
        mock_client = AsyncMock()
        mock_client.smembers = AsyncMock(return_value={"key1", "key2", "key3"})
        mock_client.delete = AsyncMock(return_value=True)

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            deleted = await invalidate_domain_cache("example.com")

        assert deleted == 3
        # 3 scanner keys + 1 domain key set
        assert mock_client.delete.await_count == 4

    async def test_invalidate_empty_domain(self):
        mock_client = AsyncMock()
        mock_client.smembers = AsyncMock(return_value=set())

        with (
            patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings,
            patch(
                "src.domains.critique.services.scan_cache.get_redis_client",
                return_value=mock_client,
            ),
        ):
            mock_settings.return_value.cache.enabled = True
            deleted = await invalidate_domain_cache("example.com")

        assert deleted == 0

    async def test_invalidate_cache_disabled(self):
        with patch("src.domains.critique.services.scan_cache.get_settings") as mock_settings:
            mock_settings.return_value.cache.enabled = False
            deleted = await invalidate_domain_cache("example.com")
        assert deleted == 0
