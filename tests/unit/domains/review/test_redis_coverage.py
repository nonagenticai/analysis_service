"""Targeted coverage tests for redis_client.py.

Covers lines not exercised by existing tests:
- __aenter__ / __aexit__ (57-58, 61)
- _serialize with model_dump (70)
- _deserialize None and parse errors (75, 78-79)
- delete success and error paths (104-110)
- exists success and error paths (113-119)
- ping with non-awaitable result (128-129)
- init_redis (147-149)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import RedisError

from src.shared.cache.redis_client import RedisClient, close_redis, init_redis

# ---------------------------------------------------------------------------
# __aenter__ / __aexit__ (lines 57-58, 61)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAsyncContextManager:
    async def test_aenter_connects_and_returns_self(self) -> None:
        """Cover __aenter__ (lines 57-58)."""
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
            result = await client.__aenter__()
            assert result is client
            assert client._client is mock_redis_instance
            # Cleanup
            await client.disconnect()

    async def test_aexit_disconnects(self) -> None:
        """Cover __aexit__ (line 61)."""
        client = RedisClient()
        client._client = AsyncMock()
        client._client.aclose = AsyncMock()
        client._pool = AsyncMock()
        client._pool.aclose = AsyncMock()

        await client.__aexit__(None, None, None)

        assert client._client is None
        assert client._pool is None

    async def test_async_with_statement(self) -> None:
        """Cover full async with usage."""
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
            async with RedisClient() as client:
                assert client._client is mock_redis_instance

            # After exiting, client should be disconnected
            assert client._client is None


# ---------------------------------------------------------------------------
# _serialize with model_dump (line 70)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSerialize:
    def test_serialize_pydantic_model(self) -> None:
        """Cover line 70: _serialize calls model_dump on Pydantic-like objects."""
        client = RedisClient()

        mock_model = MagicMock()
        mock_model.model_dump.return_value = {"name": "test", "value": 42}

        result = client._serialize(mock_model)
        assert result == '{"name": "test", "value": 42}'
        mock_model.model_dump.assert_called_once_with(mode="json")

    def test_serialize_plain_dict(self) -> None:
        """Verify _serialize works on plain dicts too."""
        client = RedisClient()

        result = client._serialize({"key": "val"})
        assert result == '{"key": "val"}'


# ---------------------------------------------------------------------------
# _deserialize None and parse errors (lines 75, 78-79)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeserialize:
    def test_deserialize_none_returns_none(self) -> None:
        """Cover line 75: _deserialize returns None for None input."""
        client = RedisClient()
        result = client._deserialize(None)
        assert result is None

    def test_deserialize_invalid_json_returns_none(self) -> None:
        """Cover lines 78-79: _deserialize returns None for invalid JSON."""
        client = RedisClient()
        result = client._deserialize("not valid json {{{}}")
        assert result is None

    def test_deserialize_valid_json(self) -> None:
        """Verify _deserialize works on valid JSON."""
        client = RedisClient()
        result = client._deserialize('{"hello": "world"}')
        assert result == {"hello": "world"}


# ---------------------------------------------------------------------------
# delete success and error paths (lines 104-110)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisDelete:
    async def test_delete_success(self) -> None:
        """Cover lines 104-107: successful delete returns True."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.delete = AsyncMock(return_value=1)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.delete("some-key")
        assert result is True
        mock_redis_instance.delete.assert_called_once_with("some-key")

    async def test_delete_key_not_found(self) -> None:
        """Delete returns False when key doesn't exist (result=0)."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.delete = AsyncMock(return_value=0)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.delete("nonexistent-key")
        assert result is False

    async def test_delete_error_returns_false(self) -> None:
        """Cover lines 108-110: delete returns False on error."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.delete = AsyncMock(side_effect=RedisError("Connection lost"))

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.delete("some-key")
        assert result is False


# ---------------------------------------------------------------------------
# exists success and error paths (lines 113-119)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisExists:
    async def test_exists_true(self) -> None:
        """Cover lines 113-116: exists returns True when key exists."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.exists = AsyncMock(return_value=1)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.exists("some-key")
        assert result is True

    async def test_exists_false(self) -> None:
        """exists returns False when key doesn't exist."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.exists = AsyncMock(return_value=0)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.exists("nonexistent-key")
        assert result is False

    async def test_exists_error_returns_false(self) -> None:
        """Cover lines 117-119: exists returns False on error."""
        mock_redis_instance = AsyncMock()
        mock_redis_instance.exists = AsyncMock(side_effect=RedisError("Connection lost"))

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.exists("some-key")
        assert result is False


# ---------------------------------------------------------------------------
# ping with non-awaitable result (lines 128-129)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRedisPingSync:
    async def test_ping_with_sync_result(self) -> None:
        """Cover lines 128-129: ping handles non-awaitable (sync) result."""
        mock_redis_instance = MagicMock()
        # Return a plain bool (not awaitable) from ping
        mock_redis_instance.ping = MagicMock(return_value=True)

        client = RedisClient()
        client._client = mock_redis_instance
        client._pool = MagicMock()

        result = await client.ping()
        assert result is True


# ---------------------------------------------------------------------------
# init_redis (lines 147-149)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitRedis:
    async def test_init_redis_creates_and_connects(self) -> None:
        """Cover lines 147-149: init_redis creates client and connects."""
        import w2v_common.cache.redis_client as common_redis_mod

        import src.shared.cache.redis_client as redis_module

        # Reset global state
        redis_module._redis_client = None
        redis_module.get_redis_client.cache_clear()

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
            client = await init_redis()
            assert client is not None
            assert client._client is mock_redis_instance

        # Cleanup
        old = common_redis_mod._redis_client
        try:
            common_redis_mod._redis_client = client
            await close_redis()
        finally:
            common_redis_mod._redis_client = old
        redis_module.get_redis_client.cache_clear()
