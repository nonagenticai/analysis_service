"""Tests for middleware modules (rate_limiter, request_id)."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.shared.middleware.rate_limiter import RATE_LIMIT_PREFIX, RateLimitMiddleware
from src.shared.middleware.request_id import RequestIDMiddleware


class TestRequestIDMiddleware:
    """Test RequestIDMiddleware."""

    async def test_generates_request_id(self):
        middleware = RequestIDMiddleware.__new__(RequestIDMiddleware)
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()

        mock_response = MagicMock()
        mock_response.headers = {}
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)
        # Should set request_id on state
        assert hasattr(request.state, "request_id")
        # Should add header to response
        assert "X-Request-Id" in response.headers

    async def test_passes_through_existing_request_id(self):
        middleware = RequestIDMiddleware.__new__(RequestIDMiddleware)
        request = MagicMock()
        request.headers = {"x-request-id": "custom-123"}
        request.state = MagicMock()

        mock_response = MagicMock()
        mock_response.headers = {}
        call_next = AsyncMock(return_value=mock_response)

        response = await middleware.dispatch(request, call_next)
        assert request.state.request_id == "custom-123"
        assert response.headers["X-Request-Id"] == "custom-123"


class TestRateLimitMiddleware:
    """Test RateLimitMiddleware."""

    async def test_disabled_passes_through(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/api/critique"

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings:
            mock_settings.return_value.rate_limit.enabled = False
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()

    async def test_non_post_passes_through(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "GET"
        request.url.path = "/api/critique"

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings:
            mock_settings.return_value.rate_limit.enabled = True
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()

    async def test_non_critique_path_passes_through(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/api/other"
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=0)
        mock_client.incr = AsyncMock(return_value=1)
        mock_client.expire = AsyncMock(return_value=True)

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with (
            patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings,
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            mock_settings.return_value.rate_limit.enabled = True
            mock_settings.return_value.rate_limit.hourly_limit = 60
            mock_settings.return_value.rate_limit.daily_limit = 500
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()

    async def test_under_limit_passes_through(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/api/critique"
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=5)  # Under limit
        mock_client.incr = AsyncMock(return_value=6)
        mock_client.expire = AsyncMock(return_value=True)

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with (
            patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings,
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            mock_settings.return_value.rate_limit.enabled = True
            mock_settings.return_value.rate_limit.hourly_limit = 60
            mock_settings.return_value.rate_limit.daily_limit = 500
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()

    async def test_hourly_limit_exceeded(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "POST"
        mock_url = MagicMock()
        mock_url.path = "/api/critique"
        request.url = mock_url
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=60)  # At limit

        call_next = AsyncMock()

        with (
            patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings,
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            mock_settings.return_value.rate_limit.enabled = True
            mock_settings.return_value.rate_limit.hourly_limit = 60
            mock_settings.return_value.rate_limit.daily_limit = 500
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 429
        call_next.assert_not_awaited()

    async def test_redis_error_falls_through(self):
        middleware = RateLimitMiddleware.__new__(RateLimitMiddleware)
        request = MagicMock()
        request.method = "POST"
        request.url.path = "/api/critique"
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=ConnectionError("Redis down"))

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with (
            patch("src.shared.middleware.rate_limiter.get_settings") as mock_settings,
            patch("src.shared.middleware.rate_limiter.get_redis_client", return_value=mock_client),
        ):
            mock_settings.return_value.rate_limit.enabled = True
            await middleware.dispatch(request, call_next)

        # Should pass through when Redis is down
        call_next.assert_awaited_once()

    def test_rate_limit_prefix(self):
        assert RATE_LIMIT_PREFIX == "analysis:ratelimit:"
