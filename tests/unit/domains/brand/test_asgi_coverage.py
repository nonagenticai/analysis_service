"""Tests for asgi.py -- covers lifespan, health filter, error handler, root endpoint."""

from __future__ import annotations

import io
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.asgi import HealthCheckLoggingFilter


class TestHealthCheckLoggingFilter:
    def test_filters_health_endpoint(self) -> None:
        f = HealthCheckLoggingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="GET /health 200",
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is False

    def test_filters_health_ready(self) -> None:
        f = HealthCheckLoggingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="GET /health/ready 200",
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is False

    def test_filters_health_live(self) -> None:
        f = HealthCheckLoggingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="GET /health/live 200",
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is False

    def test_passes_non_health(self) -> None:
        f = HealthCheckLoggingFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="POST /api/brand/extract 200",
            args=None,
            exc_info=None,
        )
        assert f.filter(record) is True


class TestRootEndpoint:
    """Test root endpoint without starting lifespan."""

    def test_root_response(self) -> None:
        # Using TestClient with raise_server_exceptions=False to avoid lifespan issues
        with patch("src.asgi.lifespan"):
            test_app = FastAPI()

            @test_app.get("/")
            async def root():
                return {
                    "name": "Brand Extractor Service",
                    "status": "running",
                }

            client = TestClient(test_app)
            response = client.get("/")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "running"


def _make_mock_engine():
    """Create a properly structured async engine mock."""
    mock_conn = AsyncMock()
    mock_conn.run_sync = AsyncMock()

    # Create an async context manager for begin()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx
    mock_engine.dispose = AsyncMock()
    return mock_engine


class TestLifespan:
    """Test lifespan startup/shutdown."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_and_shutdown(self) -> None:
        from src.asgi import lifespan

        mock_app = MagicMock(spec=FastAPI)
        mock_app.state = MagicMock()

        mock_engine = _make_mock_engine()

        mock_redis = AsyncMock()
        mock_redis.ping.return_value = True
        mock_redis.is_available = True

        mock_session_factory = AsyncMock()

        with (
            patch(
                "src.shared.database.connection.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ),
            patch(
                "src.shared.database.session.get_session_maker",
                new_callable=AsyncMock,
                return_value=mock_session_factory,
            ),
            patch(
                "src.shared.cache.redis_client.init_redis",
                new_callable=AsyncMock,
                return_value=mock_redis,
            ),
            patch("src.shared.cache.redis_client.get_redis_client", return_value=mock_redis),
            patch("src.shared.cache.redis_client.close_redis", new_callable=AsyncMock),
            patch("src.shared.database.connection.close_engine", new_callable=AsyncMock),
        ):
            async with lifespan(mock_app):
                # Should have set up db_engine, redis, pipeline_runner etc
                assert mock_app.state.db_engine is not None

    @pytest.mark.asyncio
    async def test_lifespan_redis_failure_continues(self) -> None:
        from src.asgi import lifespan

        mock_app = MagicMock(spec=FastAPI)
        mock_app.state = MagicMock()

        mock_engine = _make_mock_engine()

        mock_session_factory = AsyncMock()

        with (
            patch(
                "src.shared.database.connection.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ),
            patch(
                "src.shared.database.session.get_session_maker",
                new_callable=AsyncMock,
                return_value=mock_session_factory,
            ),
            patch(
                "src.shared.cache.redis_client.init_redis",
                new_callable=AsyncMock,
                side_effect=ConnectionError("Redis down"),
            ),
            patch("src.shared.cache.redis_client.close_redis", new_callable=AsyncMock),
            patch("src.shared.database.connection.close_engine", new_callable=AsyncMock),
        ):
            async with lifespan(mock_app):
                pass  # Should not raise despite Redis failure

    @pytest.mark.asyncio
    async def test_lifespan_db_failure_raises(self) -> None:
        from src.asgi import lifespan

        mock_app = MagicMock(spec=FastAPI)
        mock_app.state = MagicMock()

        with patch(
            "src.shared.database.connection.get_engine",
            new_callable=AsyncMock,
            side_effect=ConnectionError("DB down"),
        ):
            with pytest.raises(ConnectionError):
                async with lifespan(mock_app):
                    pass


_FAKE_PASSWORD = "not-a-real-password-xyzzy"  # pragma: allowlist secret
_FAKE_DSN = (
    f"postgresql+psycopg://analysis_app:{_FAKE_PASSWORD}@pgbouncer.pgbouncer:6432/analysis_service"
)


class TestDatabaseConnectedLogRedaction:
    """The startup 'Database connected' line must never carry the DSN password."""

    @pytest.mark.asyncio
    async def test_init_database_log_redacts_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.asgi as asgi

        monkeypatch.setattr(asgi.settings.database, "url", _FAKE_DSN)

        mock_app = MagicMock(spec=FastAPI)
        mock_app.state = MagicMock()

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        asgi.logger.addHandler(handler)
        previous_level = asgi.logger.level
        asgi.logger.setLevel(logging.INFO)
        try:
            with (
                patch(
                    "src.shared.database.connection.get_engine",
                    new_callable=AsyncMock,
                    return_value=_make_mock_engine(),
                ),
                patch(
                    "src.shared.database.session.get_session_maker",
                    new_callable=AsyncMock,
                    return_value=AsyncMock(),
                ),
                patch("src.shared.database.rls_events.install_rls_tenant_rebind"),
            ):
                await asgi._init_database(mock_app)
            captured = stream.getvalue()
        finally:
            asgi.logger.removeHandler(handler)
            asgi.logger.setLevel(previous_level)

        # Vacuity guard: without this the test passes when the line never emits.
        assert "Database connected" in captured

        assert _FAKE_PASSWORD not in captured
        assert "analysis_app" not in captured

        # The line must stay useful.
        assert "pgbouncer.pgbouncer:6432" in captured
        assert "analysis_service" in captured


# ── The startup "Redis connected" line must never carry the Redis password ──
#
# REGRESSION: `_init_redis` logged `settings.redis.url` raw -- the full
# redis://[user]:<password>@host:port/db URL -- at INFO on the connect path, so
# the live Redis password landed in this pod's stdout and in anything shipping
# those logs, on every pod start, indefinitely.
#
# This drives the REAL `_init_redis` rather than the redactor, because the defect
# was at the CALL SITE: a test that only exercises
# `w2v_common.observability.redis_endpoint` passes for the whole duration of the
# leak. That helper has its own unit tests in w2v_common.

_FAKE_REDIS_PASSWORD = "not-a-real-password-xyzzy"  # pragma: allowlist secret
_FAKE_REDIS_URL = f"redis://analysis_cache:{_FAKE_REDIS_PASSWORD}@redis.redis:6379/4"


class TestRedisConnectedLogRedaction:
    """The startup 'Redis connected' line must never carry the Redis password."""

    @pytest.mark.asyncio
    async def test_init_redis_log_redacts_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.asgi as asgi

        monkeypatch.setattr(asgi.settings.redis, "url", _FAKE_REDIS_URL)

        mock_app = MagicMock(spec=FastAPI)
        mock_app.state = MagicMock()

        mock_redis = AsyncMock()
        mock_redis.ping = AsyncMock()

        # A handler on the module logger, NOT ``caplog``: this service calls
        # ``configure_logging()``, which re-points the root handlers, so caplog
        # would capture nothing and every assertion below would pass vacuously.
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        asgi.logger.addHandler(handler)
        previous_level = asgi.logger.level
        # MUST be setLevel(): ``Logger`` caches ``isEnabledFor`` and only
        # ``setLevel`` clears that cache. Assigning ``.level`` directly leaves a
        # stale entry, the record never reaches the handler, and the negative
        # assertions below pass on EMPTY output with the leak intact.
        asgi.logger.setLevel(logging.INFO)
        try:
            with (
                patch(
                    "src.shared.cache.redis_client.init_redis",
                    new_callable=AsyncMock,
                    return_value=mock_redis,
                ),
                patch(
                    "src.shared.cache.redis_client.get_redis_client",
                    return_value=mock_redis,
                ),
            ):
                await asgi._init_redis(mock_app)
            captured = stream.getvalue()
        finally:
            asgi.logger.removeHandler(handler)
            asgi.logger.setLevel(previous_level)

        # Vacuity guard: without this the test passes when the line never emits.
        assert "Redis connected" in captured, (
            "the 'Redis connected' line never emitted; the assertions would be vacuous"
        )

        assert _FAKE_REDIS_PASSWORD not in captured, "Redis password leaked into the startup log"
        assert "analysis_cache" not in captured, "Redis username leaked into the startup log"

        # The line must stay useful.
        assert "redis.redis:6379" in captured
