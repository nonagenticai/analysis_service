"""Unit tests for health check endpoints.

Tests /health, /health/live, and /health/ready with various component states.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.responses import JSONResponse


def _make_request(db_engine=None, redis_client=None):
    """Create a mock request with app.state."""
    request = MagicMock()
    request.app.state.db_engine = db_engine
    request.app.state.redis_client = redis_client
    return request


@pytest.mark.unit
class TestHealthCheck:
    """Tests for the /health endpoint logic."""

    async def test_health_all_healthy(self) -> None:
        """All components healthy returns healthy status."""
        from src.shared.health import health_check

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = dict(result)
        assert data["status"] == "healthy"
        components: dict[str, Any] = data["components"]
        assert components["database"]["status"] == "healthy"
        assert components["redis"]["status"] == "healthy"

    async def test_health_db_unhealthy(self) -> None:
        """Database failure returns unhealthy status."""
        from src.shared.health import health_check

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=ConnectionError("DB down"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)

        assert isinstance(result, dict | JSONResponse)
        if isinstance(result, JSONResponse):
            assert result.status_code == 503
        else:
            assert result["status"] in ("degraded", "unhealthy")

    async def test_health_redis_not_configured(self, monkeypatch) -> None:
        """Redis not configured returns appropriate status."""
        from src.shared.health import health_check

        # "not configured" now means genuinely no redis: no client AND no
        # REDIS_URL. The suite sets REDIS_URL globally; blank it here. (Blank
        # rather than delete: RedisSettings.url is a required field, so removing
        # the var makes get_settings() — used by _check_storage — raise.)
        monkeypatch.setenv("REDIS_URL", "")
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = dict(result)
        components: dict[str, Any] = data["components"]
        assert components["redis"]["status"] == "not configured"

    async def test_health_redis_ping_fails(self) -> None:
        """Redis ping failure returns degraded status."""
        from src.shared.health import health_check

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis down"))
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = dict(result)
        components: dict[str, Any] = data["components"]
        assert components["redis"]["status"] == "unhealthy"


@pytest.mark.unit
class TestHealthLive:
    """Tests for the /health/live endpoint."""

    async def test_liveness_probe(self) -> None:
        """Liveness probe always returns ok."""
        from src.shared.health import health_live

        result = await health_live()
        assert result == {"status": "ok"}


@pytest.mark.unit
class TestHealthReady:
    """Tests for the /health/ready endpoint."""

    async def test_readiness_probe_healthy(self) -> None:
        """Readiness probe returns ready when DB is up."""
        from src.shared.health import health_ready

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)

        assert isinstance(result, dict)
        data = dict(result)
        assert data["ready"] is True
        assert data["status"] == "healthy"

    async def test_readiness_probe_db_down(self) -> None:
        """Readiness probe returns 503 when DB is down."""
        from src.shared.health import health_ready

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=ConnectionError("DB down"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)

        # Returns JSONResponse with 503
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503
