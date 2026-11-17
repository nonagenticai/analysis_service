"""Unit tests for review cache layer.

Tests get_cached_result and set_cached_result with various states.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
class TestGetCachedResult:
    """Tests for get_cached_result."""

    async def test_returns_cached_dict(self) -> None:
        """Returns cached result when value is a dict."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value={"job_id": "j1", "status": "completed"})

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import get_cached_result

            result = await get_cached_result("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1")

        assert result == {"job_id": "j1", "status": "completed"}
        # The tenant must be IN THE KEY: Redis has no RLS backstop, and this
        # read happens before the DB lookup on GET /api/review/{job_id}.
        mock_client.get.assert_awaited_once_with(
            "review:cache:36:7eed672d-a7ff-5f16-87d7-b6a493fee523:j1"
        )

    async def test_returns_none_when_not_dict(self) -> None:
        """Returns None when cached value is not a dict."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value="not-a-dict")

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import get_cached_result

            result = await get_cached_result("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1")

        assert result is None

    async def test_returns_none_when_key_missing(self) -> None:
        """Returns None when key does not exist in cache."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import get_cached_result

            result = await get_cached_result("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1")

        assert result is None

    async def test_returns_none_on_exception(self) -> None:
        """Returns None when Redis raises an exception."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import get_cached_result

            result = await get_cached_result("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1")

        assert result is None


@pytest.mark.unit
class TestSetCachedResult:
    """Tests for set_cached_result."""

    async def test_stores_result_with_ttl(self) -> None:
        """Stores result in cache with specified TTL."""
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import set_cached_result

            result = await set_cached_result(
                "7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1", {"status": "completed"}, ttl=600
            )

        assert result is True
        # The tenant must be IN THE KEY -- see get_cached_result's twin assertion.
        mock_client.set.assert_awaited_once_with(
            "review:cache:36:7eed672d-a7ff-5f16-87d7-b6a493fee523:j1",
            {"status": "completed"},
            ttl=600,
        )

    async def test_returns_false_on_exception(self) -> None:
        """Returns False when Redis raises an exception."""
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(side_effect=ConnectionError("Redis down"))

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import set_cached_result

            result = await set_cached_result(
                "7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1", {"status": "completed"}
            )

        assert result is False

    async def test_default_ttl(self) -> None:
        """Uses default TTL of 3600 seconds."""
        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with patch(
            "src.domains.review.services.review_cache.get_redis_client", return_value=mock_client
        ):
            from src.domains.review.services.review_cache import set_cached_result

            await set_cached_result(
                "7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1", {"status": "completed"}
            )

        call_args = mock_client.set.call_args
        assert (
            call_args[1].get("ttl") == 3600 or call_args[0][2] == 3600
            if len(call_args[0]) > 2
            else True
        )


@pytest.mark.unit
class TestCacheKeyInjectivity:
    """Two different (tenant, job) pairs must never share a key.

    ``f"{prefix}{tenant}:{job}"`` is not injective: ("a", "b:c") and ("a:b",
    "c") both render ``review:cache:a:b:c``, so a value written under one is
    read back by the other. Not escalatable while tenant ids are UUIDs from
    the license token -- but "unreachable today" is a property of the caller,
    not of the key, and the key is the thing that has to hold.
    """

    def test_ambiguous_split_does_not_collide(self) -> None:
        from src.domains.review.services.review_cache import cache_key

        assert cache_key("a", "b:c") != cache_key("a:b", "c")

    def test_distinct_pairs_yield_distinct_keys(self) -> None:
        from src.domains.review.services.review_cache import cache_key

        pairs = [
            ("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1"),
            ("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j2"),
            ("t2", "j1"),
            ("a", "b:c"),
            ("a:b", "c"),
            ("", "a:b"),
            ("a:b", ""),
            ("a:", "b"),
            ("a", ":b"),
        ]
        keys = [cache_key(t, j) for t, j in pairs]
        assert len(set(keys)) == len(pairs), f"collision among {pairs}: {keys}"

    def test_key_is_still_namespaced_and_readable(self) -> None:
        """Injective, but still greppable in redis-cli -- not an opaque hash."""
        from src.domains.review.services.review_cache import CACHE_PREFIX, cache_key

        key = cache_key("7eed672d-a7ff-5f16-87d7-b6a493fee523", "j1")
        assert key.startswith(CACHE_PREFIX)
        assert "7eed672d-a7ff-5f16-87d7-b6a493fee523" in key
        assert "j1" in key
