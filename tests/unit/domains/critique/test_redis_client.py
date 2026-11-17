"""Tests for RedisClient."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from redis.exceptions import RedisError

from src.shared.cache.redis_client import RedisClient, close_redis


class TestRedisClientSerialization:
    """Test RedisClient serialization helpers."""

    @patch("src.config.get_settings")
    def test_serialize_dict(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        result = client._serialize({"key": "value"})
        assert result == '{"key": "value"}'

    @patch("src.config.get_settings")
    def test_serialize_pydantic_model(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        model = MagicMock()
        model.model_dump.return_value = {"field": 42}
        result = client._serialize(model)
        assert json.loads(result) == {"field": 42}

    @patch("src.config.get_settings")
    def test_deserialize_valid_json(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        result = client._deserialize('{"key": "value"}')
        assert result == {"key": "value"}

    @patch("src.config.get_settings")
    def test_deserialize_none(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        assert client._deserialize(None) is None

    @patch("src.config.get_settings")
    def test_deserialize_invalid_json(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        assert client._deserialize("not valid json") is None


class TestRedisClientEnsureConnected:
    """Test _ensure_connected."""

    @patch("src.config.get_settings")
    def test_ensure_connected_raises_when_not_connected(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        with pytest.raises(RuntimeError, match="not connected"):
            client._ensure_connected()

    @patch("src.config.get_settings")
    def test_ensure_connected_returns_client(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        client._client = mock_redis
        assert client._ensure_connected() is mock_redis


class TestRedisClientOperations:
    """Test Redis client async operations with mocked connection."""

    @patch("src.config.get_settings")
    async def test_get_success(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value='{"data": 1}')
        client._client = mock_redis

        result = await client.get("test-key")
        assert result == {"data": 1}

    @patch("src.config.get_settings")
    async def test_get_none(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        client._client = mock_redis

        result = await client.get("missing-key")
        assert result is None

    @patch("src.config.get_settings")
    async def test_get_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(side_effect=RedisError("Connection lost"))
        client._client = mock_redis

        result = await client.get("error-key")
        assert result is None

    @patch("src.config.get_settings")
    async def test_set_with_ttl(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.set("key", {"data": 1}, ttl=60)
        assert result is True
        mock_redis.setex.assert_awaited_once()

    @patch("src.config.get_settings")
    async def test_set_without_ttl(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.set("key", {"data": 1})
        assert result is True

    @patch("src.config.get_settings")
    async def test_set_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.setex = AsyncMock(side_effect=RedisError("fail"))
        client._client = mock_redis

        result = await client.set("key", "value", ttl=60)
        assert result is False

    @patch("src.config.get_settings")
    async def test_delete(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(return_value=1)
        client._client = mock_redis

        result = await client.delete("key")
        assert result is True

    @patch("src.config.get_settings")
    async def test_delete_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock(side_effect=RedisError("fail"))
        client._client = mock_redis

        result = await client.delete("key")
        assert result is False

    @patch("src.config.get_settings")
    async def test_exists(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=1)
        client._client = mock_redis

        result = await client.exists("key")
        assert result is True

    @patch("src.config.get_settings")
    async def test_exists_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(side_effect=RedisError("fail"))
        client._client = mock_redis

        result = await client.exists("key")
        assert result is False

    @patch("src.config.get_settings")
    async def test_ping_success(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        # ping() returns an awaitable
        mock_redis.ping = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.ping()
        assert result is True

    @patch("src.config.get_settings")
    async def test_ping_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.ping = MagicMock(side_effect=RedisError("fail"))
        client._client = mock_redis

        result = await client.ping()
        assert result is False

    @patch("src.config.get_settings")
    async def test_hset(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hset = AsyncMock(return_value=1)
        client._client = mock_redis

        result = await client.hset("key", "field", "value")
        assert result is True

    @patch("src.config.get_settings")
    async def test_hset_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hset = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.hset("key", "field", "value")
        assert result is False

    @patch("src.config.get_settings")
    async def test_hgetall(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hgetall = AsyncMock(return_value={"field": "value"})
        client._client = mock_redis

        result = await client.hgetall("key")
        assert result == {"field": "value"}

    @patch("src.config.get_settings")
    async def test_hgetall_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hgetall = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.hgetall("key")
        assert result == {}

    @patch("src.config.get_settings")
    async def test_incr(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(return_value=5)
        client._client = mock_redis

        result = await client.incr("counter")
        assert result == 5

    @patch("src.config.get_settings")
    async def test_incr_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.incr = AsyncMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.incr("counter")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_expire(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.expire = AsyncMock(return_value=True)
        client._client = mock_redis

        result = await client.expire("key", 60)
        assert result is True

    @patch("src.config.get_settings")
    async def test_expire_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_redis.expire = AsyncMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.expire("key", 60)
        assert result is False

    @patch("src.config.get_settings")
    async def test_hget(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hget = AsyncMock(return_value="value")
        client._client = mock_redis

        result = await client.hget("key", "field")
        assert result == "value"

    @patch("src.config.get_settings")
    async def test_hget_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hget = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.hget("key", "field")
        assert result is None

    @patch("src.config.get_settings")
    async def test_hdel(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hdel = AsyncMock(return_value=1)
        client._client = mock_redis

        result = await client.hdel("key", "field1", "field2")
        assert result == 1

    @patch("src.config.get_settings")
    async def test_hdel_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.hdel = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.hdel("key", "field")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_lpush(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.lpush = AsyncMock(return_value=3)
        client._client = mock_redis

        result = await client.lpush("list", "v1", "v2")
        assert result == 3

    @patch("src.config.get_settings")
    async def test_lpush_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.lpush = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.lpush("list", "v1")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_rpop(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.rpop = AsyncMock(return_value="value")
        client._client = mock_redis

        result = await client.rpop("list")
        assert result == "value"

    @patch("src.config.get_settings")
    async def test_rpop_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.rpop = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.rpop("list")
        assert result is None

    @patch("src.config.get_settings")
    async def test_llen(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.llen = AsyncMock(return_value=5)
        client._client = mock_redis

        result = await client.llen("list")
        assert result == 5

    @patch("src.config.get_settings")
    async def test_llen_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.llen = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.llen("list")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_sadd(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.sadd = AsyncMock(return_value=2)
        client._client = mock_redis

        result = await client.sadd("set", "m1", "m2")
        assert result == 2

    @patch("src.config.get_settings")
    async def test_sadd_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.sadd = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.sadd("set", "m1")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_srem(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.srem = AsyncMock(return_value=1)
        client._client = mock_redis

        result = await client.srem("set", "m1")
        assert result == 1

    @patch("src.config.get_settings")
    async def test_srem_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.srem = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.srem("set", "m1")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_scard(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.scard = AsyncMock(return_value=3)
        client._client = mock_redis

        result = await client.scard("set")
        assert result == 3

    @patch("src.config.get_settings")
    async def test_scard_error(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_redis.scard = MagicMock(side_effect=OSError("fail"))
        client._client = mock_redis

        result = await client.scard("set")
        assert result == 0

    @patch("src.config.get_settings")
    async def test_pipeline(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)
        client._client = mock_redis

        result = await client.pipeline()
        assert result is mock_pipe

    @patch("src.config.get_settings")
    async def test_disconnect(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()
        mock_redis = AsyncMock()
        mock_pool = AsyncMock()
        client._client = mock_redis
        client._pool = mock_pool

        await client.disconnect()
        mock_redis.aclose.assert_awaited_once()
        mock_pool.aclose.assert_awaited_once()
        assert client._client is None
        assert client._pool is None

    @patch("src.config.get_settings")
    async def test_disconnect_no_client(self, mock_settings):
        mock_settings.return_value.redis.url = "redis://localhost:6379/0"
        mock_settings.return_value.redis.max_connections = 10
        mock_settings.return_value.redis.socket_timeout = 5
        mock_settings.return_value.redis.socket_connect_timeout = 5
        mock_settings.return_value.redis.decode_responses = True
        client = RedisClient()

        # Should not raise
        await client.disconnect()


class TestModuleFunctions:
    """Test module-level functions."""

    async def test_close_redis_with_client(self):
        import w2v_common.cache.redis_client as common_mod

        mock_client = AsyncMock()
        common_mod._redis_client = mock_client

        await close_redis()
        mock_client.disconnect.assert_awaited_once()
        assert common_mod._redis_client is None

    async def test_close_redis_no_client(self):
        import w2v_common.cache.redis_client as common_mod

        common_mod._redis_client = None
        # Should not raise
        await close_redis()
