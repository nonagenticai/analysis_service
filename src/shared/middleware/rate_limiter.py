"""Rate limiting middleware for analysis_service.

Per-tenant sliding window rate limiting using Redis counters.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.config import get_settings
from src.shared.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

RATE_LIMIT_PREFIX = "analysis:ratelimit:"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Rate limiting middleware using Redis sliding window counters."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Check per-tenant rate limits before allowing requests.

        Args:
            request: Incoming HTTP request.
            call_next: Next handler in the middleware chain.

        Returns:
            429 JSONResponse if rate limited, otherwise downstream response.
        """
        settings = get_settings()
        if not settings.rate_limit.enabled:
            return await call_next(request)

        # Only rate limit mutation endpoints
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return await call_next(request)

        tenant_id = getattr(request.state, "tenant_id", "anonymous")

        try:
            client = get_redis_client()

            now = int(time.time())
            hour_key = f"{RATE_LIMIT_PREFIX}{tenant_id}:hour:{now // 3600}"
            day_key = f"{RATE_LIMIT_PREFIX}{tenant_id}:day:{now // 86400}"

            # Check hourly limit
            hourly_raw = await client.get(hour_key)
            hourly_count = int(hourly_raw) if hourly_raw is not None else 0
            if hourly_count >= settings.rate_limit.hourly_limit:
                retry_after = 3600 - (now % 3600)
                logger.warning(
                    "Rate limit exceeded (hourly): tenant=%s count=%d",
                    tenant_id,
                    hourly_count,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded (hourly)"},
                    headers={"Retry-After": str(retry_after)},
                )

            # Check daily limit
            daily_raw = await client.get(day_key)
            daily_count = int(daily_raw) if daily_raw is not None else 0
            if daily_count >= settings.rate_limit.daily_limit:
                retry_after = 86400 - (now % 86400)
                logger.warning(
                    "Rate limit exceeded (daily): tenant=%s count=%d",
                    tenant_id,
                    daily_count,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded (daily)"},
                    headers={"Retry-After": str(retry_after)},
                )

            # Increment counters
            await client.incr(hour_key)
            await client.expire(hour_key, 3600)
            await client.incr(day_key)
            await client.expire(day_key, 86400)

        except (OSError, ConnectionError, TimeoutError, RuntimeError) as e:
            # Don't block requests if Redis is down
            logger.warning("Rate limiting check failed: %s", e)

        return await call_next(request)
