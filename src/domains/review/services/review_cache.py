"""Cache layer for completed review results.

Wraps the shared RedisClient with a domain-specific prefix and
graceful degradation -- callers never see Redis exceptions.

The key is TENANT-scoped. ``GET /api/review/{job_id}`` reads this cache
*before* it touches the database, and Redis has no row-level-security
backstop the way Postgres does, so a key of ``review:cache:{job_id}`` would
hand one tenant's completed review to any caller who guessed the id. The
tenant belongs in the key rather than in a post-read check: a check is
something a future caller can forget, a key is not.
"""

from __future__ import annotations

import logging

from src.shared.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

CACHE_PREFIX = "review:cache:"


def cache_key(tenant_id: str, job_id: str) -> str:
    """Build the tenant-scoped cache key for a review result.

    The tenant segment is LENGTH-PREFIXED because plain ``{tenant}:{job}`` is
    not injective: ``("a", "b:c")`` and ``("a:b", "c")`` both render
    ``review:cache:a:b:c``, so a value written under one pair is read back by
    the other. Tenant ids are UUIDs from the license token today, so no caller
    can reach the ambiguity — but that is a property of the callers, not of the
    key, and the key is the thing that has to hold. A length prefix keeps the
    result greppable in ``redis-cli`` (unlike a hash) while making the split
    unambiguous.
    """
    return f"{CACHE_PREFIX}{len(tenant_id)}:{tenant_id}:{job_id}"


async def get_cached_result(tenant_id: str, job_id: str) -> dict | None:
    """Retrieve a cached review result for one tenant's job.

    Returns:
        The cached result dict, or ``None`` if the key is missing (including
        the case where the job belongs to a different tenant) or Redis is
        unavailable.
    """
    try:
        client = get_redis_client()
        result = await client.get(cache_key(tenant_id, job_id))
        if isinstance(result, dict):
            logger.debug("Cache hit: tenant=%s job_id=%s", tenant_id, job_id)
            return result
        logger.debug("Cache miss: tenant=%s job_id=%s", tenant_id, job_id)
        return None
    except Exception:
        logger.exception("Failed to get cached result for job %s", job_id)
        return None


async def set_cached_result(tenant_id: str, job_id: str, result: dict, ttl: int = 3600) -> bool:
    """Store a review result in the cache under the owning tenant's key.

    Args:
        tenant_id: The tenant that owns the job.
        job_id: The review job identifier.
        result: The result dict to cache.
        ttl: Time-to-live in seconds (default 1 hour).

    Returns:
        ``True`` if the value was stored successfully, ``False`` otherwise.
    """
    try:
        client = get_redis_client()
        success = await client.set(cache_key(tenant_id, job_id), result, ttl=ttl)
        logger.debug(
            "Cache set: tenant=%s job_id=%s, ttl=%d, success=%s", tenant_id, job_id, ttl, success
        )
        return success
    except Exception:
        logger.exception("Failed to cache result for job %s", job_id)
        return False
