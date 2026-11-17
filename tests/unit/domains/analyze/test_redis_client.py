"""Unit tests for RedisClient and module-level helper functions."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.shared.cache.redis_client import RedisClient, close_redis, get_redis_client, init_redis

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_fakeredis(client: RedisClient) -> fakeredis.aioredis.FakeRedis:
    """Inject a fakeredis async client into a RedisClient, simulating connect().

    Sets both _client and _pool so that is_available returns True and
    disconnect() can clean up without errors.
    """
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    client._client = fake
    client._pool = fake.connection_pool
    return fake


# ---------------------------------------------------------------------------
# TestRedisClient -- init, connect, disconnect
# ---------------------------------------------------------------------------


class TestRedisClientLifecycle:
    """Tests for __init__, connect, disconnect and context manager."""

    def test_init_uses_settings_defaults(self) -> None:
        # conftest autouse fixture seeds REDIS_URL=redis://localhost:6393/1
        # (w2v_common 0.15.0+ strips the base localhost fallback).
        client = RedisClient()
        assert client._redis_url == "redis://localhost:6393/1"
        assert client._max_connections == 10
        assert client._decode_responses is True
        assert client._pool is None
        assert client._client is None

    def test_init_accepts_explicit_url(self) -> None:
        client = RedisClient(redis_url="redis://override:6379/1")
        assert client._redis_url == "redis://override:6379/1"

    async def test_connect_creates_pool_and_client(self) -> None:
        """Patch ConnectionPool.from_url and the ping to verify connect logic."""
        mock_pool = AsyncMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.return_value = True

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url", return_value=mock_pool
            ) as mock_from_url,
            patch("w2v_common.cache.redis_client.redis.Redis", return_value=mock_redis_instance),
        ):
            client = RedisClient()
            await client.connect()

            mock_from_url.assert_called_once()
            assert client._pool is mock_pool
            assert client._client is mock_redis_instance

            await client.disconnect()

    async def test_connect_is_idempotent(self) -> None:
        """Calling connect() twice should not create a second pool."""
        mock_pool = AsyncMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.return_value = True

        with (
            patch(
                "w2v_common.cache.redis_client.ConnectionPool.from_url", return_value=mock_pool
            ) as mock_from_url,
            patch("w2v_common.cache.redis_client.redis.Redis", return_value=mock_redis_instance),
        ):
            client = RedisClient()
            await client.connect()
            await client.connect()

            # from_url should only be called once because pool already exists
            mock_from_url.assert_called_once()
            await client.disconnect()

    async def test_connect_raises_on_ping_failure(self) -> None:
        """If the initial ping fails, connect should raise."""
        mock_pool = AsyncMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.side_effect = ConnectionError("refused")

        with (
            patch("w2v_common.cache.redis_client.ConnectionPool.from_url", return_value=mock_pool),
            patch("w2v_common.cache.redis_client.redis.Redis", return_value=mock_redis_instance),
        ):
            client = RedisClient()
            with pytest.raises(ConnectionError, match="refused"):
                await client.connect()

    async def test_disconnect_clears_pool_and_client(self) -> None:
        client = RedisClient()
        _inject_fakeredis(client)
        assert client._pool is not None
        assert client._client is not None

        await client.disconnect()
        assert client._pool is None
        assert client._client is None

    async def test_disconnect_when_not_connected_is_safe(self) -> None:
        client = RedisClient()
        await client.disconnect()  # should not raise

    async def test_context_manager(self) -> None:
        """__aenter__ calls connect; __aexit__ calls disconnect."""
        mock_pool = AsyncMock()
        mock_redis_instance = AsyncMock()
        mock_redis_instance.ping.return_value = True

        with (
            patch("w2v_common.cache.redis_client.ConnectionPool.from_url", return_value=mock_pool),
            patch("w2v_common.cache.redis_client.redis.Redis", return_value=mock_redis_instance),
        ):
            async with RedisClient() as client:
                assert client.is_available is True
            # After exiting the context, client should be None
            assert client._client is None
            assert client._pool is None


# ---------------------------------------------------------------------------
# TestEnsureConnected
# ---------------------------------------------------------------------------


class TestEnsureConnected:
    def test_raises_when_not_connected(self) -> None:
        client = RedisClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()

    def test_returns_client_when_connected(self) -> None:
        client = RedisClient()
        fake = _inject_fakeredis(client)
        result = client._ensure_connected()
        assert result is fake


# ---------------------------------------------------------------------------
# TestIsAvailable
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_false_before_connect(self) -> None:
        client = RedisClient()
        assert client.is_available is False

    def test_true_when_client_set(self) -> None:
        client = RedisClient()
        _inject_fakeredis(client)
        assert client.is_available is True

    async def test_false_after_disconnect(self) -> None:
        client = RedisClient()
        _inject_fakeredis(client)
        await client.disconnect()
        assert client.is_available is False


# ---------------------------------------------------------------------------
# TestSerializeDeserialize
# ---------------------------------------------------------------------------


class TestSerializeDeserialize:
    def test_serialize_dict(self) -> None:
        client = RedisClient()
        result = client._serialize({"key": "value"})
        assert json.loads(result) == {"key": "value"}

    def test_serialize_list(self) -> None:
        client = RedisClient()
        result = client._serialize([1, 2, 3])
        assert json.loads(result) == [1, 2, 3]

    def test_serialize_string(self) -> None:
        client = RedisClient()
        result = client._serialize("hello")
        assert json.loads(result) == "hello"

    def test_serialize_int(self) -> None:
        client = RedisClient()
        result = client._serialize(42)
        assert json.loads(result) == 42

    def test_serialize_pydantic_model(self) -> None:
        """Objects with .model_dump() should be serialised via that method."""
        client = RedisClient()
        obj = MagicMock()
        obj.model_dump.return_value = {"field": "val"}
        result = client._serialize(obj)
        obj.model_dump.assert_called_once_with(mode="json")
        assert json.loads(result) == {"field": "val"}

    def test_deserialize_valid_json(self) -> None:
        client = RedisClient()
        assert client._deserialize('{"a": 1}') == {"a": 1}

    def test_deserialize_list(self) -> None:
        client = RedisClient()
        assert client._deserialize("[1, 2, 3]") == [1, 2, 3]

    def test_deserialize_none_returns_none(self) -> None:
        client = RedisClient()
        assert client._deserialize(None) is None

    def test_deserialize_invalid_json_returns_none(self) -> None:
        client = RedisClient()
        assert client._deserialize("not-valid-json{") is None


# ---------------------------------------------------------------------------
# TestCRUDOperations -- get / set / delete / exists / ping (with fakeredis)
# ---------------------------------------------------------------------------


class TestCRUDOperations:
    """Tests using a fakeredis backend for actual Redis command execution."""

    def _connected_client(self) -> RedisClient:
        """Return a RedisClient wired to a fresh fakeredis instance."""
        client = RedisClient()
        _inject_fakeredis(client)
        return client

    # -- SET / GET --

    async def test_set_and_get_dict(self) -> None:
        client = self._connected_client()
        ok = await client.set("k1", {"hello": "world"})
        assert ok is True
        result = await client.get("k1")
        assert result == {"hello": "world"}

    async def test_set_with_ttl(self) -> None:
        client = self._connected_client()
        ok = await client.set("k_ttl", "value", ttl=60)
        assert ok is True
        result = await client.get("k_ttl")
        assert result == "value"

    async def test_set_without_ttl(self) -> None:
        client = self._connected_client()
        ok = await client.set("k_no_ttl", {"data": 123})
        assert ok is True
        result = await client.get("k_no_ttl")
        assert result == {"data": 123}

    async def test_get_missing_key_returns_none(self) -> None:
        client = self._connected_client()
        result = await client.get("nonexistent")
        assert result is None

    # -- DELETE --

    async def test_delete_existing_key(self) -> None:
        client = self._connected_client()
        await client.set("del_me", "data")
        deleted = await client.delete("del_me")
        assert deleted is True
        assert await client.get("del_me") is None

    async def test_delete_missing_key(self) -> None:
        client = self._connected_client()
        deleted = await client.delete("no_such_key")
        assert deleted is False

    # -- EXISTS --

    async def test_exists_true(self) -> None:
        client = self._connected_client()
        await client.set("ex", 42)
        assert await client.exists("ex") is True

    async def test_exists_false(self) -> None:
        client = self._connected_client()
        assert await client.exists("nope") is False

    # -- PING --

    async def test_ping_success(self) -> None:
        client = self._connected_client()
        assert await client.ping() is True


# ---------------------------------------------------------------------------
# TestOperationsWhenDisconnected
# ---------------------------------------------------------------------------


class TestOperationsWhenDisconnected:
    """Operations on a disconnected client should return safe defaults."""

    async def test_get_returns_none(self) -> None:
        client = RedisClient()
        assert await client.get("k") is None

    async def test_set_returns_false(self) -> None:
        client = RedisClient()
        assert await client.set("k", "v") is False

    async def test_delete_returns_false(self) -> None:
        client = RedisClient()
        assert await client.delete("k") is False

    async def test_exists_returns_false(self) -> None:
        client = RedisClient()
        assert await client.exists("k") is False

    async def test_ping_returns_false(self) -> None:
        client = RedisClient()
        assert await client.ping() is False


# ---------------------------------------------------------------------------
# TestOperationFailures -- underlying redis raises
# ---------------------------------------------------------------------------


class TestOperationFailures:
    """When the underlying Redis client raises, operations log and return defaults."""

    def _make_failing_client(self) -> RedisClient:
        """Return a RedisClient whose internal _client always raises."""
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.get.side_effect = RedisConnectionError("boom")
        mock_redis.set.side_effect = RedisConnectionError("boom")
        mock_redis.setex.side_effect = RedisConnectionError("boom")
        mock_redis.delete.side_effect = RedisConnectionError("boom")
        mock_redis.exists.side_effect = RedisConnectionError("boom")
        mock_redis.ping.side_effect = RedisConnectionError("boom")
        client._client = mock_redis
        return client

    async def test_get_returns_none_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.get("k") is None

    async def test_set_returns_false_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.set("k", "v") is False

    async def test_set_with_ttl_returns_false_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.set("k", "v", ttl=60) is False

    async def test_delete_returns_false_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.delete("k") is False

    async def test_exists_returns_false_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.exists("k") is False

    async def test_ping_returns_false_on_error(self) -> None:
        client = self._make_failing_client()
        assert await client.ping() is False


# ---------------------------------------------------------------------------
# TestModuleFunctions -- get_redis_client, init_redis, close_redis
# ---------------------------------------------------------------------------


class TestModuleFunctions:
    """Tests for module-level singleton helpers."""

    def teardown_method(self) -> None:
        # Reset module-level state between tests
        import src.shared.cache.redis_client as mod

        mod._redis_client = None
        get_redis_client.cache_clear()

    def test_get_redis_client_returns_instance(self) -> None:
        client = get_redis_client()
        assert isinstance(client, RedisClient)

    def test_get_redis_client_is_singleton(self) -> None:
        a = get_redis_client()
        b = get_redis_client()
        assert a is b

    async def test_init_redis_connects_and_returns_client(self) -> None:
        with patch.object(RedisClient, "connect", new_callable=AsyncMock) as mock_connect:
            client = await init_redis()
            assert isinstance(client, RedisClient)
            mock_connect.assert_awaited_once()

    async def test_close_redis_disconnects_and_clears(self) -> None:
        import w2v_common.cache.redis_client as common_mod

        mock_client = AsyncMock(spec=RedisClient)
        common_mod._redis_client = mock_client

        await close_redis()

        mock_client.disconnect.assert_awaited_once()
        assert common_mod._redis_client is None

    async def test_close_redis_noop_when_no_client(self) -> None:
        import w2v_common.cache.redis_client as common_mod

        common_mod._redis_client = None
        await close_redis()  # should not raise
