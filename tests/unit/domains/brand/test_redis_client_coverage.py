"""Extended tests for redis_client.py -- covers connect, disconnect, context manager, ping, publish, error handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import RedisError

from src.shared.cache.redis_client import RedisClient, close_redis, get_redis_client, init_redis


class TestRedisClientConnect:
    @pytest.mark.asyncio
    async def test_connect_creates_pool_and_client(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._redis_url = "redis://localhost:6379/0"
        client._max_connections = 10
        client._socket_timeout = 5
        client._socket_connect_timeout = 5
        client._decode_responses = True
        client._health_check_interval = 0
        client._pool = None
        client._client = None

        mock_pool = MagicMock()
        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url",
                return_value=mock_pool,
            ),
            patch(
                "w2v_common.cache.redis_client.redis.Redis",
                return_value=mock_redis,
            ),
        ):
            await client.connect()
            assert client._pool is mock_pool
            assert client._client is mock_redis

    @pytest.mark.asyncio
    async def test_connect_already_connected_noop(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._pool = MagicMock()
        client._client = AsyncMock()

        # Should not create new pool
        await client.connect()
        assert client._pool is not None

    @pytest.mark.asyncio
    async def test_connect_ping_failure_raises(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._redis_url = "redis://localhost:6379/0"
        client._max_connections = 10
        client._socket_timeout = 5
        client._socket_connect_timeout = 5
        client._decode_responses = True
        client._health_check_interval = 0
        client._pool = None
        client._client = None

        mock_pool = MagicMock()
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionError("Connection refused")

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url",
                return_value=mock_pool,
            ),
            patch(
                "w2v_common.cache.redis_client.redis.Redis",
                return_value=mock_redis,
            ),
        ):
            with pytest.raises(ConnectionError):
                await client.connect()


class TestRedisClientDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_closes_client_and_pool(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_pool = AsyncMock()
        client._client = mock_redis
        client._pool = mock_pool

        await client.disconnect()
        mock_redis.aclose.assert_called_once()
        mock_pool.aclose.assert_called_once()
        assert client._client is None
        assert client._pool is None

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._client = None
        client._pool = None

        # Should not raise
        await client.disconnect()


class TestRedisClientContextManager:
    @pytest.mark.asyncio
    async def test_aenter_connects(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._redis_url = "redis://localhost:6379/0"
        client._max_connections = 10
        client._socket_timeout = 5
        client._socket_connect_timeout = 5
        client._decode_responses = True
        client._pool = None
        client._client = None

        with (
            patch.object(client, "connect", new_callable=AsyncMock) as mock_connect,
            patch.object(client, "disconnect", new_callable=AsyncMock),
        ):
            result = await client.__aenter__()
            mock_connect.assert_called_once()
            assert result is client

    @pytest.mark.asyncio
    async def test_aexit_disconnects(self) -> None:
        client = RedisClient.__new__(RedisClient)
        with patch.object(client, "disconnect", new_callable=AsyncMock) as mock_disconnect:
            await client.__aexit__(None, None, None)
            mock_disconnect.assert_called_once()


class TestRedisClientPing:
    @pytest.mark.asyncio
    async def test_ping_success_awaitable(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        client._client = mock_redis

        result = await client.ping()
        assert result is True

    @pytest.mark.asyncio
    async def test_ping_failure_returns_false(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = RedisError("Connection lost")
        client._client = mock_redis

        result = await client.ping()
        assert result is False

    @pytest.mark.asyncio
    async def test_ping_non_awaitable(self) -> None:
        """Test when ping returns a non-awaitable result."""
        client = RedisClient.__new__(RedisClient)
        mock_redis = MagicMock()
        # Return a regular bool, not a coroutine
        mock_redis.ping.return_value = True
        client._client = mock_redis

        result = await client.ping()
        assert result is True


class TestRedisClientPublish:
    @pytest.mark.asyncio
    async def test_publish_success(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.publish.return_value = 3
        client._client = mock_redis

        result = await client.publish("channel", "message")
        assert result == 3
        mock_redis.publish.assert_called_once_with("channel", "message")

    @pytest.mark.asyncio
    async def test_publish_failure_returns_zero(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.publish.side_effect = RedisError("Publish failed")
        client._client = mock_redis

        result = await client.publish("channel", "message")
        assert result == 0


class TestRedisClientErrorHandling:
    @pytest.mark.asyncio
    async def test_get_error_returns_none(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = RedisError("Redis error")
        client._client = mock_redis

        result = await client.get("key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_error_returns_false(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.set.side_effect = RedisError("Redis error")
        client._client = mock_redis

        result = await client.set("key", "value")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_error_returns_false(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.delete.side_effect = RedisError("Redis error")
        client._client = mock_redis

        result = await client.delete("key")
        assert result is False

    @pytest.mark.asyncio
    async def test_exists_error_returns_false(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.exists.side_effect = RedisError("Redis error")
        client._client = mock_redis

        result = await client.exists("key")
        assert result is False


class TestModuleLevelFunctions:
    @pytest.mark.asyncio
    async def test_init_redis(self) -> None:
        import src.shared.cache.redis_client as mod

        old_client = mod._redis_client
        try:
            mod._redis_client = None
            mod.get_redis_client.cache_clear()
            mock_client = MagicMock(spec=RedisClient)
            mock_client.connect = AsyncMock()
            with patch("src.shared.cache.redis_client.RedisClient", return_value=mock_client):
                result = await init_redis()
                mock_client.connect.assert_called_once()
                assert result is mock_client
        finally:
            mod._redis_client = old_client
            mod.get_redis_client.cache_clear()

    @pytest.mark.asyncio
    async def test_close_redis(self) -> None:
        import w2v_common.cache.redis_client as common_mod

        mock_client = AsyncMock()
        old_client = common_mod._redis_client
        try:
            common_mod._redis_client = mock_client
            await close_redis()
            mock_client.disconnect.assert_called_once()
            assert common_mod._redis_client is None
        finally:
            common_mod._redis_client = old_client

    @pytest.mark.asyncio
    async def test_close_redis_noop_when_none(self) -> None:
        import w2v_common.cache.redis_client as common_mod

        old_client = common_mod._redis_client
        try:
            common_mod._redis_client = None
            await close_redis()  # should not raise
        finally:
            common_mod._redis_client = old_client

    def test_get_redis_client_caches(self) -> None:
        import src.shared.cache.redis_client as mod

        old_client = mod._redis_client
        try:
            mod._redis_client = None
            mod.get_redis_client.cache_clear()
            with patch(
                "src.shared.cache.redis_client.RedisClient",
                return_value=MagicMock(spec=RedisClient),
            ):
                c1 = get_redis_client()
                c2 = get_redis_client()
                assert c1 is c2
        finally:
            mod._redis_client = old_client
            mod.get_redis_client.cache_clear()
