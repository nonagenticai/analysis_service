"""ICP-specific caching layer.

Mirrors :class:`~src.domains.brand.services.brand_cache_service.BrandCacheService`:
a tenant-scoped, domain-keyed Redis cache over the shared ``RedisClient``. Keyed by
domain (not raw URL) so ``https://x.io`` and ``https://x.io/pricing`` share one entry;
every operation is a no-op when caching is disabled or Redis is unavailable, and the
caller treats read/write failures as best-effort.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import urlparse

from src.config import get_settings
from src.domains.icp.schemas import ICPResponse
from src.shared.cache.redis_client import RedisClient

logger = logging.getLogger(__name__)


class IcpCacheService:
    """ICP-specific caching using RedisClient (tenant + domain scoped)."""

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client
        settings = get_settings()
        self._ttl = settings.cache.icp_ttl
        self._enabled = settings.cache.enabled
        self._prefix = settings.cache.key_prefix

    def _make_key(self, tenant_id: str, domain: str) -> str:
        """Generate cache key: '{prefix}:icp:{tenant_id}:{sha256(domain)[:16]}'."""
        domain_hash = hashlib.sha256(domain.encode()).hexdigest()[:16]
        return f"{self._prefix}:icp:{tenant_id}:{domain_hash}"

    def _domain_from_url(self, url: str) -> str:
        """Extract clean domain from URL."""
        parsed = urlparse(url)
        return parsed.netloc.removeprefix("www.")

    async def get(self, tenant_id: str, domain: str) -> ICPResponse | None:
        """Get a cached ICP record by tenant and domain (None if absent/disabled)."""
        if not self._enabled:
            return None
        key = self._make_key(tenant_id, domain)
        data = await self._redis.get(key)
        if data is None:
            logger.debug("ICP cache miss, key=%s, domain=%s", key, domain)
            return None
        try:
            result = ICPResponse.model_validate(data)
            logger.debug("ICP cache hit, key=%s, domain=%s", key, domain)
            return result
        except (OSError, ConnectionError, TimeoutError, ValueError) as e:
            logger.warning("Failed to deserialize cached ICP for %s: %s", domain, e)
            return None

    async def get_by_url(self, tenant_id: str, url: str) -> ICPResponse | None:
        """Get a cached ICP record by URL (extracts the domain first)."""
        domain = self._domain_from_url(url)
        if not domain:
            return None
        return await self.get(tenant_id, domain)

    async def set(
        self, tenant_id: str, domain: str, icp: ICPResponse, ttl: int | None = None
    ) -> bool:
        """Cache an ICP response with a configurable TTL (no-op if disabled)."""
        if not self._enabled:
            return False
        key = self._make_key(tenant_id, domain)
        effective_ttl = ttl if ttl is not None else self._ttl
        result = await self._redis.set(key, icp, effective_ttl)
        logger.debug(
            "ICP cache set, key=%s, domain=%s, ttl=%d, success=%s",
            key,
            domain,
            effective_ttl,
            result,
        )
        return result

    async def invalidate(self, tenant_id: str, domain: str) -> bool:
        """Invalidate the cached ICP record for a domain."""
        key = self._make_key(tenant_id, domain)
        result = await self._redis.delete(key)
        logger.debug("ICP cache invalidated, key=%s, domain=%s, deleted=%s", key, domain, result)
        return result
