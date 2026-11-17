"""Unit tests for database connection module.

Tests get_engine, close_engine, health_check, and
_ensure_database_exists edge cases not covered by test_database.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import w2v_common.database.connection as conn_module
from sqlalchemy.exc import OperationalError, ProgrammingError


@pytest.fixture(autouse=True)
def reset_engine():
    """Reset the global engine before each test."""
    original = conn_module._engine
    conn_module._engine = None
    yield
    conn_module._engine = original


@pytest.mark.unit
class TestGetEngine:
    """Tests for get_engine function."""

    async def test_creates_engine_on_first_call(self) -> None:
        """First call to get_engine creates a new engine."""
        mock_engine = AsyncMock()

        with patch.object(
            conn_module,
            "create_engine_with_retry",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            result = await conn_module.get_engine()

        assert result is mock_engine

    async def test_returns_cached_engine_on_second_call(self) -> None:
        """Second call returns cached engine without creating a new one."""
        mock_engine = AsyncMock()

        with patch.object(
            conn_module,
            "create_engine_with_retry",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ) as mock_create:
            result1 = await conn_module.get_engine()
            result2 = await conn_module.get_engine()

        assert result1 is result2
        mock_create.assert_awaited_once()


@pytest.mark.unit
class TestCloseEngine:
    """Tests for close_engine function."""

    async def test_close_disposes_and_resets(self) -> None:
        """close_engine disposes the engine and sets it to None."""
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()
        conn_module._engine = mock_engine

        await conn_module.close_engine()

        mock_engine.dispose.assert_awaited_once()
        assert conn_module._engine is None

    async def test_close_when_none(self) -> None:
        """close_engine is safe when engine is already None."""
        conn_module._engine = None

        await conn_module.close_engine()

        assert conn_module._engine is None


@pytest.mark.unit
class TestHealthCheck:
    """Tests for health_check function."""

    async def test_health_check_returns_true(self) -> None:
        """health_check returns True when DB responds."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with patch.object(
            conn_module,
            "get_engine",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            result = await conn_module.health_check()

        assert result is True

    async def test_health_check_returns_false_on_error(self) -> None:
        """health_check returns False when DB fails."""
        with patch.object(
            conn_module,
            "get_engine",
            new_callable=AsyncMock,
            side_effect=ConnectionError("DB down"),
        ):
            result = await conn_module.health_check()

        assert result is False


@pytest.mark.unit
class TestEnsureDatabaseExists:
    """Tests for _ensure_database_exists edge cases."""

    async def test_db_already_exists(self) -> None:
        """Skips CREATE when database already exists."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists
        mock_conn.execute = MagicMock(return_value=mock_result)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            await conn_module._ensure_database_exists(max_retries=1)

        # Only one execute call (the SELECT check), no CREATE
        assert mock_conn.execute.call_count == 1

    async def test_connection_refused_retries(self) -> None:
        """Retries on OperationalError and raises ConnectionError after max retries."""
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(
            side_effect=OperationalError("connection refused", None, Exception("refused"))
        )
        mock_engine.dispose = MagicMock()

        with (
            patch("w2v_common.database.connection.create_engine", return_value=mock_engine),
            pytest.raises(ConnectionError, match="Failed to connect"),
        ):
            await conn_module._ensure_database_exists(max_retries=1)

    async def test_invalid_db_name_raises(self) -> None:
        """Invalid database name characters raise ValueError."""
        original_name = conn_module.DB_NAME
        try:
            conn_module.DB_NAME = "my-invalid-db-name"

            with pytest.raises(ValueError, match="invalid characters"):
                await conn_module._ensure_database_exists(max_retries=1)
        finally:
            conn_module.DB_NAME = original_name

    async def test_unexpected_error_raises(self) -> None:
        """Unexpected errors are re-raised immediately."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.execute = MagicMock(side_effect=RuntimeError("Unexpected"))
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)
        mock_engine.dispose = MagicMock()

        with (
            patch("w2v_common.database.connection.create_engine", return_value=mock_engine),
            pytest.raises(RuntimeError, match="Unexpected"),
        ):
            await conn_module._ensure_database_exists(max_retries=1)


@pytest.mark.unit
class TestCreateEngineWithRetryDatabaseNotExist:
    """Tests for engine creation when database does not exist."""

    async def test_database_not_exist_triggers_ensure(self) -> None:
        """ProgrammingError with 'does not exist' triggers _ensure_database_exists."""
        # First engine: fails with "database does not exist"
        mock_engine_bad = AsyncMock()
        mock_conn_bad = AsyncMock()
        mock_conn_bad.execute = AsyncMock(
            side_effect=ProgrammingError(
                'database "test" does not exist',
                None,
                Exception('database "test" does not exist'),
            )
        )
        mock_conn_bad.__aenter__ = AsyncMock(return_value=mock_conn_bad)
        mock_conn_bad.__aexit__ = AsyncMock(return_value=None)
        mock_engine_bad.begin = MagicMock(return_value=mock_conn_bad)

        # After ensure: engine succeeds
        mock_engine_good = AsyncMock()
        mock_conn_good = AsyncMock()
        mock_conn_good.execute = AsyncMock()
        mock_conn_good.__aenter__ = AsyncMock(return_value=mock_conn_good)
        mock_conn_good.__aexit__ = AsyncMock(return_value=None)
        mock_engine_good.begin = MagicMock(return_value=mock_conn_good)

        call_count = 0

        def _create_engine_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return mock_engine_bad
            return mock_engine_good

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                side_effect=_create_engine_side,
            ),
            patch(
                "w2v_common.database.connection._ensure_database_exists",
                new_callable=AsyncMock,
            ) as mock_ensure,
        ):
            engine = await conn_module.create_engine_with_retry(max_retries=2, retry_delay=0.01)

        assert engine is mock_engine_good
        mock_ensure.assert_awaited_once()

    async def test_connection_refused_retries_and_fails(self) -> None:
        """ConnectionRefusedError retries and eventually raises ConnectionError."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                return_value=mock_engine,
            ),
            pytest.raises(ConnectionError, match="Failed to connect"),
        ):
            await conn_module.create_engine_with_retry(max_retries=1, retry_delay=0.01)

    async def test_general_exception_retries(self) -> None:
        """General exceptions retry and eventually raise ConnectionError."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=RuntimeError("something weird"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                return_value=mock_engine,
            ),
            pytest.raises(ConnectionError, match="Failed to connect"),
        ):
            await conn_module.create_engine_with_retry(max_retries=1, retry_delay=0.01)
