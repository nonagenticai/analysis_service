"""Unit tests for Redis client.

Tests RedisClient connect/disconnect, get/set, ping, and graceful degradation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import RedisError

from src.shared.cache.redis_client import RedisClient, close_redis


@pytest.mark.unit
class TestRedisClientInit:
    """Tests for Redis client initialization."""

    async def test_init_redis_success(self) -> None:
        """Test successful Redis initialization and ping."""
        mock_pool = AsyncMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping = AsyncMock(return_value=True)
        mock_redis_instance.aclose = AsyncMock()

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url",
                return_value=mock_pool,
            ),
            patch(
                "w2v_common.cache.redis_client.redis.Redis",
                return_value=mock_redis_instance,
            ),
        ):
            client = RedisClient()
            await client.connect()

            assert client._client is mock_redis_instance
            assert client._pool is mock_pool

            await client.disconnect()

    async def test_init_redis_connection_failure_graceful(self) -> None:
        """Test that connection failure raises but does not crash the process."""
        mock_pool = MagicMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping = AsyncMock(side_effect=ConnectionError("Connection refused"))
        mock_redis_instance.aclose = AsyncMock()

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url",
                return_value=mock_pool,
            ),
            patch(
                "w2v_common.cache.redis_client.redis.Redis",
                return_value=mock_redis_instance,
            ),
            pytest.raises(ConnectionError, match="Connection refused"),
        ):
            client = RedisClient()
            await client.connect()


@pytest.mark.unit
class TestRedisClientPing:
    """Tests for Redis ping behavior."""

    async def test_ping_failure_returns_false(self) -> None:
        """Test that ping failure returns False instead of raising."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping = AsyncMock(side_effect=RedisError("Connection lost"))

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.ping()
        assert result is False


@pytest.mark.unit
class TestRedisClientGetSet:
    """Tests for Redis get/set operations."""

    async def test_get_returns_none_on_error(self) -> None:
        """Test that get returns None when Redis raises an error."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.get = AsyncMock(side_effect=RedisError("Connection lost"))

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.get("some-key")
        assert result is None

    async def test_set_returns_false_on_error(self) -> None:
        """Test that set returns False when Redis raises an error."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.set = AsyncMock(side_effect=RedisError("Connection lost"))

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.set("some-key", {"data": "value"})
        assert result is False

    async def test_get_returns_deserialized_value(self) -> None:
        """Test that get deserializes JSON values correctly."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.get = AsyncMock(return_value='{"name": "test"}')

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.get("some-key")
        assert result == {"name": "test"}

    async def test_set_with_ttl(self) -> None:
        """Test that set with TTL calls setex."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.setex = AsyncMock(return_value=True)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.set("some-key", "value", ttl=300)
        assert result is True
        mock_redis_instance.setex.assert_called_once()


@pytest.mark.unit
class TestRedisClientCleanup:
    """Tests for Redis client cleanup."""

    async def test_close_redis_cleanup(self) -> None:
        """Test that close_redis properly disconnects and resets global state."""
        import w2v_common.cache.redis_client as common_redis_mod

        mock_client_instance = RedisClient.__new__(RedisClient)
        mock_client_instance._client = AsyncMock()
        mock_client_instance._client.aclose = AsyncMock()
        mock_client_instance._pool = AsyncMock()
        mock_client_instance._pool.aclose = AsyncMock()

        old = common_redis_mod._redis_client
        try:
            common_redis_mod._redis_client = mock_client_instance
            await close_redis()
            assert common_redis_mod._redis_client is None
        finally:
            common_redis_mod._redis_client = old

    async def test_disconnect_when_not_connected(self) -> None:
        """Test that disconnect is safe to call when not connected."""
        client = RedisClient()
        # Should not raise
        await client.disconnect()
        assert client._client is None
        assert client._pool is None

    async def test_ensure_connected_raises_when_not_connected(self) -> None:
        """Test that _ensure_connected raises RuntimeError when not connected."""
        client = RedisClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()
