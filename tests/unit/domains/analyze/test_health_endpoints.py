"""Unit tests for health check endpoints (src/api/health.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from src.shared.health import (
    _check_database,
    _check_llm,
    _check_redis,
    _check_storage,
    health_check,
    health_live,
    health_ready,
    health_startup,
)


def _make_request(
    db_engine=None,
    redis_client=None,
) -> MagicMock:
    """Create a mock Request with app.state attributes."""
    request = MagicMock()
    request.app.state.db_engine = db_engine
    request.app.state.redis_client = redis_client
    return request


# ---------------------------------------------------------------------------
# _check_database
# ---------------------------------------------------------------------------


class TestCheckDatabase:
    async def test_healthy_database(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await _check_database(request)
        assert result["status"] == "healthy"
        assert "latency_ms" in result

    async def test_database_not_configured(self):
        request = _make_request(db_engine=None)
        result = await _check_database(request)
        assert result["status"] == "not configured"

    async def test_database_unhealthy(self):
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(side_effect=RuntimeError("connection failed"))

        request = _make_request(db_engine=mock_engine)
        result = await _check_database(request)
        assert result["status"] == "unhealthy"
        assert "error" in result
        assert "latency_ms" in result

    async def test_sqlalchemy_operational_error_is_caught(self) -> None:
        """SQLAlchemy's OperationalError is not an OSError — it must still be caught."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(
            side_effect=OperationalError("SELECT 1", None, Exception("db down"))
        )
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await _check_database(request)
        assert result["status"] == "unhealthy"
        assert "db down" in result["error"]


# ---------------------------------------------------------------------------
# _check_redis
# ---------------------------------------------------------------------------


class TestCheckRedis:
    """The three states must stay distinct: absent, answering, not answering."""

    async def test_healthy_redis(self):
        """State 2: configured AND a real ping() answered."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(redis_client=mock_redis)
        result = await _check_redis(request)
        assert result["status"] == "healthy"
        assert "latency_ms" in result
        mock_redis.ping.assert_awaited_once()

    async def test_redis_not_configured(self, monkeypatch: pytest.MonkeyPatch):
        """State 1: no client and no REDIS_URL -> genuinely cacheless."""
        monkeypatch.setenv("REDIS_URL", "")
        request = _make_request(redis_client=None)
        result = await _check_redis(request)
        assert result["status"] == "not configured"

    async def test_configured_but_client_missing_is_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """State 3a: REDIS_URL set but startup left app.state.redis_client None."""
        monkeypatch.setenv("REDIS_URL", "redis://redis.test:6379/0")
        request = _make_request(redis_client=None)
        result = await _check_redis(request)
        assert result["status"] == "unhealthy"
        assert "error" in result

    async def test_redis_not_available_is_unhealthy_not_unconfigured(self):
        """State 3b: a client that WAS configured and went down.

        Regression: this used to report "not configured", which the aggregator
        folded into healthy 200 — a dead cache looked like no cache.
        """
        mock_redis = MagicMock()
        mock_redis.is_available = False

        request = _make_request(redis_client=mock_redis)
        result = await _check_redis(request)
        assert result["status"] == "unhealthy"
        assert "error" in result

    async def test_redis_unhealthy(self):
        """State 3c: connected client whose ping() raises."""
        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_redis.is_available = True

        request = _make_request(redis_client=mock_redis)
        result = await _check_redis(request)
        assert result["status"] == "unhealthy"
        assert "error" in result

    async def test_redis_error_is_caught(self):
        """redis-py's RedisError is not an OSError — it must still be caught."""
        from redis.exceptions import RedisError

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=RedisError("connection reset"))
        mock_redis.is_available = True

        request = _make_request(redis_client=mock_redis)
        result = await _check_redis(request)
        assert result["status"] == "unhealthy"
        assert "connection reset" in result["error"]


# ---------------------------------------------------------------------------
# _check_llm
# ---------------------------------------------------------------------------


class TestCheckLlm:
    def test_llm_key_present_does_not_claim_configured(self):
        """A present key is only "key present" — the check never verifies it."""
        with patch.dict(
            "os.environ",
            {"LLM_API_KEY": "sk-test-key"},  # pragma: allowlist secret
        ):
            result = _check_llm()
            assert result["status"] == "key present"
            assert result["verified"] is False

    def test_llm_not_configured(self):
        with patch.dict("os.environ", {"LLM_API_KEY": ""}, clear=False):
            result = _check_llm()
            assert result["status"] == "not configured"


# ---------------------------------------------------------------------------
# _check_storage  — regression guard for the readOnlyRootFilesystem class of bug
# ---------------------------------------------------------------------------


class TestCheckStorage:
    def test_writable_path_returns_healthy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STORAGE_LOCAL_PATH", str(tmp_path))
        from src.config import clear_settings_cache

        clear_settings_cache()
        result = _check_storage()
        assert result["status"] == "healthy"
        assert result["path"] == str(tmp_path)

    def test_missing_path_returns_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("STORAGE_LOCAL_PATH", str(missing))
        from src.config import clear_settings_cache

        clear_settings_cache()
        result = _check_storage()
        assert result["status"] == "path not found"

    def test_unwritable_path_returns_not_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        monkeypatch.setenv("STORAGE_LOCAL_PATH", str(locked))
        from src.config import clear_settings_cache

        clear_settings_cache()
        try:
            result = _check_storage()
            assert result["status"] == "not writable"
        finally:
            locked.chmod(0o755)  # allow tmp_path cleanup


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheckEndpoint:
    async def test_all_healthy(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "healthy"

    async def test_db_healthy_redis_not_configured(self, monkeypatch: pytest.MonkeyPatch):
        """A genuinely cacheless deployment is healthy."""
        # Blank, not deleted: RedisSettings.url is required, so removing the var
        # makes get_settings() (used by _check_storage) raise.
        monkeypatch.setenv("REDIS_URL", "")
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["components"]["redis"]["status"] == "not configured"
        assert data["status"] == "healthy"

    async def test_configured_but_down_redis_is_degraded_not_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The core bug: a dead cache must not be laundered into healthy 200."""
        monkeypatch.setenv("REDIS_URL", "redis://redis.test:6379/0")
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        mock_redis = MagicMock()
        mock_redis.is_available = False  # was configured, then went down

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["components"]["redis"]["status"] == "unhealthy"
        assert data["status"] == "degraded"

    async def test_llm_key_absence_does_not_change_overall(self, monkeypatch: pytest.MonkeyPatch):
        """`llm` is reported but excluded from `overall` — unverified either way."""
        monkeypatch.setenv("REDIS_URL", "")
        monkeypatch.setenv("LLM_API_KEY", "")
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["components"]["llm"]["status"] == "not configured"
        assert data["status"] == "healthy"

    async def test_db_healthy_redis_unhealthy_is_degraded(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_redis.is_available = True

        request = _make_request(db_engine=mock_engine, redis_client=mock_redis)
        result = await health_check(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["status"] == "degraded"

    async def test_db_unhealthy_returns_503(self):
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(side_effect=RuntimeError("db down"))

        request = _make_request(db_engine=mock_engine, redis_client=None)
        result = await health_check(request)
        # Returns JSONResponse with 503
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    async def test_exception_returns_503(self):
        """When _check_database itself throws, the outer handler returns 503."""
        request = _make_request(db_engine=None)
        with patch(
            "src.shared.health._check_database",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected boom"),
        ):
            result = await health_check(request)
            assert isinstance(result, JSONResponse)
            assert result.status_code == 503


# ---------------------------------------------------------------------------
# health_live
# ---------------------------------------------------------------------------


class TestHealthLive:
    async def test_returns_ok(self):
        result = await health_live()
        assert result == {"status": "ok"}


# ---------------------------------------------------------------------------
# health_ready
# ---------------------------------------------------------------------------


class TestHealthReady:
    async def test_ready_with_healthy_db(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["ready"] is True
        assert data["status"] == "healthy"

    async def test_not_ready_without_db(self):
        request = _make_request(db_engine=None)
        result = await health_ready(request)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    async def test_not_ready_on_db_error(self):
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(side_effect=RuntimeError("db down"))

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    async def test_not_ready_on_sqlalchemy_operational_error_is_503_not_500(self) -> None:
        """A real dead Postgres raises OperationalError, not OSError.

        Regression: OperationalError inherits SQLAlchemyError, which was absent
        from the except tuple, so it escaped health_ready entirely and FastAPI
        rendered a 500. A 500 reads as a service bug; readiness must say 503.
        """
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(
            side_effect=OperationalError(
                "SELECT 1",
                None,
                Exception("connection to server at postgres.test failed"),
            )
        )
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await health_ready(request)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503


# ---------------------------------------------------------------------------
# health_startup
# ---------------------------------------------------------------------------


class TestHealthStartup:
    async def test_started_with_healthy_db(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(return_value=mock_conn)

        request = _make_request(db_engine=mock_engine)
        result = await health_startup(request)
        assert isinstance(result, dict)
        data = cast(dict[str, Any], result)
        assert data["started"] is True

    async def test_not_started_without_db(self):
        request = _make_request(db_engine=None)
        result = await health_startup(request)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    async def test_not_started_on_exception(self):
        """When _check_database throws, startup returns 503."""
        request = _make_request(db_engine=None)
        with patch(
            "src.shared.health._check_database",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = await health_startup(request)
            assert isinstance(result, JSONResponse)
            assert result.status_code == 503
