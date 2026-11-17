"""Redis client for analysis_service — extends w2v_common with publish support."""

from __future__ import annotations

import logging
from functools import lru_cache

from redis.exceptions import RedisError
from w2v_common.cache.redis_client import RedisClient as _BaseRedisClient
from w2v_common.cache.redis_client import close_redis

logger = logging.getLogger(__name__)

# Re-export close_redis so existing imports continue to work
__all__ = ["RedisClient", "close_redis", "get_redis_client", "init_redis"]


class RedisClient(_BaseRedisClient):
    """RedisClient with publish support for event broadcasting."""

    async def publish(self, channel: str, message: str) -> int:
        """Publish a message to a Redis channel.

        Args:
            channel: The channel name.
            message: The message to publish.

        Returns:
            Number of subscribers that received the message, 0 on error.
        """
        try:
            client = self._ensure_connected()
        except RuntimeError:
            return 0
        try:
            return await client.publish(channel, message)
        except (OSError, RedisError) as e:
            logger.error("Error publishing to channel '%s': %s", channel, e)
            return 0


_redis_client: RedisClient | None = None


@lru_cache
def get_redis_client() -> RedisClient:
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisClient()
    return _redis_client


async def init_redis() -> RedisClient:
    """Initialize the global Redis client and connect."""
    client = get_redis_client()
    await client.connect()
    return client
