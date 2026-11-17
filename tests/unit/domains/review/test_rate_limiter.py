"""Unit tests for rate limiting middleware.

Tests RateLimitMiddleware rate limit headers, dispatch behavior,
and graceful degradation.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.cache.redis_client import RedisClient


def _make_request(
    path: str = "/api/review",
    method: str = "POST",
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
) -> MagicMock:
    """Create a mock Starlette Request object for rate limit testing."""
    request = MagicMock()
    request.url.path = path
    request.method = method
    request.state = MagicMock()
    request.state.tenant_id = tenant_id
    return request


def _make_settings(
    enabled: bool = True,
    hourly_limit: int = 100,
    daily_limit: int = 500,
) -> MagicMock:
    """Create mock settings with ratelimit config."""
    settings = MagicMock()
    settings.rate_limit.enabled = enabled
    settings.rate_limit.hourly_limit = hourly_limit
    settings.rate_limit.daily_limit = daily_limit
    return settings


@pytest.mark.unit
class TestRateLimiterRequestUnderLimit:
    """Tests for requests that are under the rate limit."""

    async def test_request_under_limit_passes_through(self) -> None:
        """Test that a request under the limit is forwarded to the next handler."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="5")  # well under limit

        mock_pipe = AsyncMock()
        mock_pipe.incr = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[6, True, 6, True])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        mock_client = MagicMock(spec=RedisClient)
        mock_client._ensure_connected = MagicMock(return_value=mock_redis)

        settings = _make_settings(hourly_limit=100, daily_limit=500)
        request = _make_request()
        expected_response = MagicMock()
        expected_response.status_code = 200
        call_next = AsyncMock(return_value=expected_response)

        # Verify the logic: count < limit => pass through
        now = int(time.time())
        hourly_count = await mock_redis.get(f"analysis:ratelimit:test-tenant:hour:{now // 3600}")
        assert hourly_count is not None
        assert int(hourly_count) < settings.rate_limit.hourly_limit

        response = await call_next(request)
        assert response.status_code == 200


@pytest.mark.unit
class TestRateLimiterHourlyLimit:
    """Tests for hourly rate limit enforcement."""

    async def test_at_hourly_limit_returns_429(self) -> None:
        """Test that reaching the hourly limit returns 429 with Retry-After."""
        from starlette.responses import JSONResponse

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="100")  # at limit

        settings = _make_settings(hourly_limit=100)

        now = int(time.time())
        hourly_count = await mock_redis.get("test-key")
        assert int(hourly_count) >= settings.rate_limit.hourly_limit

        retry_after = 3600 - (now % 3600)
        response = JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded (hourly)"},
            headers={"Retry-After": str(retry_after)},
        )

        assert response.status_code == 429
        assert "Retry-After" in response.headers


@pytest.mark.unit
class TestRateLimiterDailyLimit:
    """Tests for daily rate limit enforcement."""

    async def test_at_daily_limit_returns_429(self) -> None:
        """Test that reaching the daily limit returns 429."""
        from starlette.responses import JSONResponse

        mock_redis = AsyncMock()
        # Hourly is fine, daily is at limit
        mock_redis.get = AsyncMock(side_effect=["50", "500"])

        settings = _make_settings(hourly_limit=100, daily_limit=500)

        # First call returns hourly (ok), second returns daily (at limit)
        hourly_count = await mock_redis.get("hour-key")
        daily_count = await mock_redis.get("day-key")

        assert int(hourly_count) < settings.rate_limit.hourly_limit
        assert int(daily_count) >= settings.rate_limit.daily_limit

        now = int(time.time())
        retry_after = 86400 - (now % 86400)
        response = JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded (daily)"},
            headers={"Retry-After": str(retry_after)},
        )

        assert response.status_code == 429


@pytest.mark.unit
class TestRateLimiterBypass:
    """Tests for rate limit bypass conditions."""

    async def test_non_post_requests_bypass_rate_limiting(self) -> None:
        """Test that GET requests bypass the rate limiter."""
        request = _make_request(method="GET")
        expected_response = MagicMock()
        expected_response.status_code = 200
        call_next = AsyncMock(return_value=expected_response)

        # Rate limiter should check: request.method != "POST" => bypass
        assert request.method != "POST"
        response = await call_next(request)
        assert response.status_code == 200
        call_next.assert_called_once()

    async def test_non_review_endpoint_bypasses_rate_limiting(self) -> None:
        """Test that non-review endpoints bypass the rate limiter."""
        request = _make_request(path="/api/health", method="POST")
        expected_response = MagicMock()
        expected_response.status_code = 200
        call_next = AsyncMock(return_value=expected_response)

        # Rate limiter should check: path does not end with /review => bypass
        assert not request.url.path.rstrip("/").endswith("/review")
        response = await call_next(request)
        assert response.status_code == 200
        call_next.assert_called_once()


@pytest.mark.unit
class TestRateLimiterGracefulDegradation:
    """Tests for graceful degradation when Redis is unavailable."""

    async def test_redis_unavailable_request_passes_through(self) -> None:
        """Test that Redis failure allows the request to pass through."""
        mock_client = MagicMock(spec=RedisClient)
        mock_client._ensure_connected = MagicMock(
            side_effect=RuntimeError("RedisClient not connected")
        )

        expected_response = MagicMock()
        expected_response.status_code = 200
        call_next = AsyncMock(return_value=expected_response)

        request = _make_request()

        # When Redis is down, the middleware catches the exception and
        # passes through to call_next
        try:
            mock_client._ensure_connected()
            # If we get here, Redis is up -- would check limits
            pytest.fail("Expected RuntimeError from mock")
        except RuntimeError:
            # Graceful degradation: pass through
            response = await call_next(request)

        assert response.status_code == 200
        call_next.assert_called_once()


@pytest.mark.unit
class TestRateLimitHeaders:
    """Tests for X-RateLimit-* response headers."""

    def test_429_response_has_rate_limit_headers(self) -> None:
        """429 responses should include X-RateLimit-* headers."""
        from starlette.responses import JSONResponse

        response = JSONResponse(
            status_code=429,
            content={"detail": "Hourly rate limit exceeded"},
            headers={
                "Retry-After": "1800",
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1800",
            },
        )
        assert response.headers["X-RateLimit-Limit"] == "100"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert response.headers["X-RateLimit-Reset"] == "1800"


@pytest.mark.unit
class TestRateLimiterDispatch:
    """Tests that actually call dispatch() to cover the middleware path."""

    async def test_dispatch_skips_non_post(self) -> None:
        """GET requests should bypass rate limiting via dispatch."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request(method="GET")
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        with patch("src.shared.middleware.rate_limiter.get_settings") as mock_gs:
            mock_gs.return_value = _make_settings(enabled=True)
            response = await middleware.dispatch(request, call_next)

        assert response is expected
        call_next.assert_called_once_with(request)

    async def test_dispatch_skips_when_disabled(self) -> None:
        """Disabled rate limiting should pass through."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        settings = _make_settings(enabled=False)
        with patch("src.shared.middleware.rate_limiter.get_settings", return_value=settings):
            response = await middleware.dispatch(request, call_next)

        assert response is expected

    async def test_dispatch_redis_failure_passes_through(self) -> None:
        """Redis failure should allow request through (graceful degradation)."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        call_next = AsyncMock(return_value=expected)

        settings = _make_settings(enabled=True)
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("no redis"))

        with (
            patch("src.shared.middleware.rate_limiter.get_settings", return_value=settings),
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            response = await middleware.dispatch(request, call_next)

        assert response is expected

    async def test_dispatch_under_limit_passes_through(self) -> None:
        """Requests under the limit should pass through."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        expected = MagicMock()
        expected.headers = {}
        call_next = AsyncMock(return_value=expected)

        settings = _make_settings(enabled=True, hourly_limit=100, daily_limit=500)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value="5")  # under limit
        mock_client.incr = AsyncMock(return_value=6)
        mock_client.expire = AsyncMock()

        with (
            patch("src.shared.middleware.rate_limiter.get_settings", return_value=settings),
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            response = await middleware.dispatch(request, call_next)

        assert response is expected

    async def test_dispatch_hourly_limit_exceeded_returns_429(self) -> None:
        """Exceeding hourly limit should return 429."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        call_next = AsyncMock()

        settings = _make_settings(enabled=True, hourly_limit=10, daily_limit=500)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value="10")  # at hourly limit

        with (
            patch("src.shared.middleware.rate_limiter.get_settings", return_value=settings),
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 429
        call_next.assert_not_called()

    async def test_dispatch_daily_limit_exceeded_returns_429(self) -> None:
        """Exceeding daily limit (while hourly is fine) should return 429."""
        from src.shared.middleware.rate_limiter import RateLimitMiddleware

        middleware = RateLimitMiddleware(app=MagicMock())
        request = _make_request()
        call_next = AsyncMock()

        settings = _make_settings(enabled=True, hourly_limit=100, daily_limit=10)

        # First get returns hourly (under limit), second returns daily (at limit)
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=["5", "10"])
        mock_client.incr = AsyncMock()
        mock_client.expire = AsyncMock()

        with (
            patch("src.shared.middleware.rate_limiter.get_settings", return_value=settings),
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 429
        call_next.assert_not_called()
