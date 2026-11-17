"""Tests for Redis client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.shared.cache.redis_client import RedisClient


class TestSerialization:
    def test_serialize_dict(self) -> None:
        client = RedisClient.__new__(RedisClient)
        result = client._serialize({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_serialize_pydantic_model(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_model = MagicMock()
        mock_model.model_dump.return_value = {"name": "test"}
        result = client._serialize(mock_model)
        assert '"name"' in result

    def test_deserialize_valid_json(self) -> None:
        client = RedisClient.__new__(RedisClient)
        result = client._deserialize('{"key": "value"}')
        assert result == {"key": "value"}

    def test_deserialize_none(self) -> None:
        client = RedisClient.__new__(RedisClient)
        result = client._deserialize(None)
        assert result is None

    def test_deserialize_invalid_json(self) -> None:
        client = RedisClient.__new__(RedisClient)
        result = client._deserialize("not-json")
        assert result is None


class TestRedisOperations:
    @pytest.mark.asyncio
    async def test_get_returns_none_on_miss(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        client._client = mock_redis
        result = await client.get("missing-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_deserialized_value(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.get.return_value = '{"name": "test"}'
        client._client = mock_redis
        result = await client.get("test-key")
        assert result == {"name": "test"}

    @pytest.mark.asyncio
    async def test_set_without_ttl(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        client._client = mock_redis
        result = await client.set("key", {"data": 1})
        assert result is True
        mock_redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_with_ttl(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.setex.return_value = True
        client._client = mock_redis
        result = await client.set("key", {"data": 1}, ttl=60)
        assert result is True
        mock_redis.setex.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.delete.return_value = 1
        client._client = mock_redis
        result = await client.delete("key")
        assert result is True

    @pytest.mark.asyncio
    async def test_exists(self) -> None:
        client = RedisClient.__new__(RedisClient)
        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 1
        client._client = mock_redis
        result = await client.exists("key")
        assert result is True

    @pytest.mark.asyncio
    async def test_ensure_connected_raises(self) -> None:
        client = RedisClient.__new__(RedisClient)
        client._client = None
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()
