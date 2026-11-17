"""Brand-specific caching layer.

Uses RedisClient with brand-specific key strategy.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

from pydantic import ValidationError
from redis.exceptions import RedisError

from src.config import get_settings
from src.domains.brand.schemas import BrandResponse
from src.shared.cache.redis_client import RedisClient

logger = logging.getLogger(__name__)

# Everything a cache round-trip can fail with that must NOT reach the caller.
#
# The cache is an optimisation, so every failure mode below has exactly one
# correct answer: behave as a miss and let the route recompute. The tuple is
# spelled out rather than caught as a bare ``Exception`` so that a genuine
# programming error (AttributeError on a renamed field, for instance) still
# surfaces in CI instead of being silently reported as a cache miss forever.
#
# * ``RedisError`` — the redis-py hierarchy (connection refused, timeout,
#   read-only replica, auth). Note this is *not* the builtin ``ConnectionError``:
#   redis-py's ``ConnectionError`` is a ``RedisError``, so catching the builtin
#   caught nothing a real outage raises.
# * ``OSError`` — socket-level failures that escape below redis-py, same shape
#   as ``src/shared/cache/redis_client.py`` already catches.
# * ``ValidationError`` / ``TypeError`` / ``ValueError`` — deserialisation of an
#   entry written by an older build of ``BrandResponse``. One schema-drifted key
#   used to 500 the route for the whole TTL; a stale entry must expire, not
#   become an outage.
_CACHE_FAILURES = (RedisError, OSError, ValidationError, TypeError, ValueError)


class BrandCacheService:
    """Brand-specific caching using RedisClient."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client
        settings = get_settings()
        self._ttl = settings.cache.brand_ttl
        self._enabled = settings.cache.enabled
        self._prefix = settings.cache.key_prefix

    def _make_key(self, tenant_id: str, domain: str) -> str:
        """Generate cache key: '{prefix}:{tenant_id}:{sha256(domain)[:16]}'."""
        domain_hash = hashlib.sha256(domain.encode()).hexdigest()[:16]
        return f"{self._prefix}:{tenant_id}:{domain_hash}"

    def _domain_from_url(self, url: str) -> str:
        """Extract clean domain from URL."""
        parsed = urlparse(url)
        return parsed.netloc.removeprefix("www.")

    async def get(self, tenant_id: str, domain: str) -> BrandResponse | None:
        """Get cached brand by tenant and domain.

        Args:
            tenant_id: Tenant identifier.
            domain: Domain to look up.

        Returns:
            Cached BrandResponse, or None on a miss, a disabled cache, an
            unreachable Redis, or an entry this build can no longer parse.
        """
        if not self._enabled:
            return None
        key = self._make_key(tenant_id, domain)
        try:
            data = await self._redis.get(key)
            if data is None:
                logger.debug("Cache miss, key=%s, domain=%s", key, domain)
                return None
            result = BrandResponse.model_validate(data)
        except _CACHE_FAILURES as e:
            # The read itself is inside the try on purpose: it used to sit
            # outside it, so a Redis outage propagated out of a best-effort
            # lookup and 500'd the extraction routes.
            logger.warning("Cache lookup failed for %s: %s", domain, e)
            return None
        logger.debug("Cache hit, key=%s, domain=%s", key, domain)
        return result

    async def get_by_url(self, tenant_id: str, url: str) -> BrandResponse | None:
        """Get cached brand by URL (extracts domain first).

        ``urlparse`` raises ``ValueError`` on some malformed authorities (an
        unbracketed IPv6 literal, for one), and the URL here comes off a request
        body. A lookup helper must not be the thing that turns a bad URL into a
        500 — the extraction path downstream owns rejecting it.
        """
        try:
            domain = self._domain_from_url(url)
        except ValueError as e:
            logger.debug("Cache lookup skipped, unparseable url=%s: %s", url, e)
            return None
        if not domain:
            return None
        return await self.get(tenant_id, domain)

    async def set(
        self, tenant_id: str, domain: str, brand: BrandResponse, ttl: int | None = None
    ) -> bool:
        """Cache a brand response with configurable TTL.

        Args:
            tenant_id: Tenant identifier.
            domain: Domain to cache.
            brand: Brand data to store.
            ttl: Optional TTL override in seconds.

        Returns:
            True if cached successfully, False otherwise (including when Redis
            is unreachable — failing to *populate* a cache is never a reason to
            fail the request that produced the value).
        """
        if not self._enabled:
            return False
        key = self._make_key(tenant_id, domain)
        effective_ttl = ttl if ttl is not None else self._ttl
        try:
            result = await self._redis.set(key, brand, effective_ttl)
        except _CACHE_FAILURES as e:
            logger.warning("Cache write failed for %s: %s", domain, e)
            return False
        logger.debug(
            "Cache set, key=%s, domain=%s, ttl=%d, success=%s", key, domain, effective_ttl, result
        )
        return result

    async def invalidate(self, tenant_id: str, domain: str) -> bool:
        """Invalidate cached brand for a domain.

        Args:
            tenant_id: Tenant identifier.
            domain: Domain to invalidate.

        Returns:
            True if the cache entry was deleted, False if it was not found or
            Redis was unreachable. Reported rather than raised: the write that
            triggered the invalidation has already been committed, so raising
            here would tell the caller its write failed when it did not. The
            entry still expires on its own TTL.
        """
        key = self._make_key(tenant_id, domain)
        try:
            result = await self._redis.delete(key)
        except _CACHE_FAILURES as e:
            logger.warning("Cache invalidation failed for %s: %s", domain, e)
            return False
        logger.debug("Cache invalidated, key=%s, domain=%s, deleted=%s", key, domain, result)
        return result

    async def exists(self, tenant_id: str, domain: str) -> bool:
        """Check if brand is cached.

        Degrades to False when Redis is unreachable — "we cannot tell" and "not
        cached" lead callers to the same (correct) place: recompute.
        """
        if not self._enabled:
            return False
        key = self._make_key(tenant_id, domain)
        try:
            return await self._redis.exists(key)
        except _CACHE_FAILURES as e:
            logger.warning("Cache existence check failed for %s: %s", domain, e)
            return False
