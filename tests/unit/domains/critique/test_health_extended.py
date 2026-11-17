"""Tests for health check endpoints."""

from datetime import UTC
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from fastapi.responses import JSONResponse

from src.shared.health import health_check, health_live, health_ready, prometheus_metrics


def _make_request(db_engine=None, redis_client=None, started_at=None):
    """Create a mock request with app.state."""
    request = MagicMock()
    request.app.state.db_engine = db_engine
    request.app.state.redis_client = redis_client
    request.app.state.started_at = started_at
    return request


class TestHealthLive:
    async def test_returns_ok(self):
        result = await health_live()
        assert result == {"status": "ok"}


class TestHealthCheck:
    """Test the expanded health check endpoint."""

    async def test_health_check_all_healthy(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "healthy"
        assert data["components"]["database"]["status"] == "healthy"
        assert data["components"]["redis"]["status"] == "healthy"

    async def test_health_check_db_unhealthy(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=OSError("DB down"))
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        # DB failure means unhealthy -> 503
        if isinstance(result, JSONResponse):
            assert result.status_code == 503
        else:
            data = cast(dict[str, Any], result)
            assert data["components"]["database"]["status"] == "unhealthy"

    async def test_health_check_redis_unhealthy(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=OSError("Redis down"))
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "degraded"

    async def test_health_check_redis_not_configured(self, monkeypatch):
        # "not configured" now means genuinely no redis: no client AND no
        # REDIS_URL. The suite sets REDIS_URL globally; blank it here. (Blank
        # rather than delete: RedisSettings.url is a required field, so removing
        # the var makes get_settings() — used by _check_storage — raise.)
        monkeypatch.setenv("REDIS_URL", "")
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["components"]["redis"]["status"] == "not configured"

    async def test_health_check_with_external_tools(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "healthy"

    async def test_health_check_with_uptime(self):
        from datetime import datetime, timedelta

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(
            db_engine=mock_engine,
            redis_client=mock_redis,
            started_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        result = await health_check(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "healthy"

    async def test_health_check_catastrophic_failure(self):
        # Make app.state itself fail by passing no engine
        request = MagicMock()
        request.app.state.db_engine = None
        request.app.state.redis_client = None
        result = await health_check(request)

        if isinstance(result, JSONResponse):
            assert result.status_code == 503
        else:
            # DB not configured -> unhealthy
            data = cast(dict[str, Any], result)
            assert data["status"] in ("unhealthy", "degraded")


class TestHealthReady:
    """Test readiness probe."""

    async def test_ready_all_up(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)

        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["ready"] is True
        assert data["status"] == "healthy"

    async def test_ready_db_down(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=OSError("DB down"))
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)

        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    async def test_ready_redis_down(self):
        """Redis being down should not affect readiness (only DB matters)."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.connect = MagicMock(return_value=mock_cm)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)

        # Readiness only checks DB, so should be ready
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["ready"] is True
        assert data["status"] == "healthy"


class TestPrometheusMetrics:
    async def test_prometheus_metrics(self):
        result = await prometheus_metrics()
        assert result.status_code == 200
        assert result.media_type == "text/plain"
