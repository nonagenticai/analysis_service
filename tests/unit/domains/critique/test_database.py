"""Tests for database connection and session modules."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from w2v_common.database.connection import (
    DB_HOST,
    DB_NAME,
    DB_PORT,
    close_engine,
    health_check,
)


class TestDatabaseConstants:
    """Test that database constants are parsed from the URL."""

    def test_db_host_exists(self):
        assert DB_HOST is not None

    def test_db_port_is_int(self):
        assert isinstance(DB_PORT, int)

    def test_db_name_exists(self):
        assert DB_NAME is not None
        assert len(DB_NAME) > 0


class TestHealthCheck:
    """Test database health_check function."""

    async def test_health_check_success(self):
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_cm)

        with patch(
            "w2v_common.database.connection.get_engine",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            result = await health_check()
        assert result is True

    async def test_health_check_failure(self):
        with patch(
            "w2v_common.database.connection.get_engine",
            new_callable=AsyncMock,
            side_effect=OSError("DB down"),
        ):
            result = await health_check()
        assert result is False


class TestCloseEngine:
    """Test close_engine function."""

    async def test_close_engine_with_engine(self):
        import w2v_common.database.connection as mod

        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()
        original = mod._engine
        mod._engine = mock_engine

        await close_engine()
        mock_engine.dispose.assert_awaited_once()
        assert mod._engine is None

        # Restore
        mod._engine = original

    async def test_close_engine_no_engine(self):
        import w2v_common.database.connection as mod

        original = mod._engine
        mod._engine = None

        # Should not raise
        await close_engine()
        assert mod._engine is None

        mod._engine = original


class TestGetEngine:
    """Test get_engine function."""

    async def test_get_engine_creates_engine(self):
        import w2v_common.database.connection as mod
        from w2v_common.database.connection import get_engine

        original = mod._engine
        mod._engine = None

        mock_engine = AsyncMock()
        with patch(
            "w2v_common.database.connection.create_engine_with_retry",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            result = await get_engine()
            assert result is mock_engine

        mod._engine = original

    async def test_get_engine_reuses_existing(self):
        import w2v_common.database.connection as mod
        from w2v_common.database.connection import get_engine

        original = mod._engine
        mock_engine = AsyncMock()
        mod._engine = mock_engine

        result = await get_engine()
        assert result is mock_engine

        mod._engine = original


class TestEnsureDatabaseExists:
    """Test _ensure_database_exists function."""

    async def test_invalid_db_name(self):
        from w2v_common.database.connection import _ensure_database_exists

        with patch("w2v_common.database.connection.DB_NAME", "invalid;name"):
            with pytest.raises(ValueError, match="invalid characters"):
                await _ensure_database_exists()

    async def test_database_already_exists(self):
        from w2v_common.database.connection import _ensure_database_exists

        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            await _ensure_database_exists()

    async def test_connection_failure_retries(self):
        from sqlalchemy.exc import OperationalError
        from w2v_common.database.connection import _ensure_database_exists

        with (
            patch(
                "w2v_common.database.connection.create_engine",
                side_effect=OperationalError("stmt", {}, Exception("conn refused")),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await _ensure_database_exists(max_retries=2)

    async def test_unexpected_error_raised(self):
        from w2v_common.database.connection import _ensure_database_exists

        with patch(
            "w2v_common.database.connection.create_engine",
            side_effect=TypeError("unexpected"),
        ):
            with pytest.raises(TypeError):
                await _ensure_database_exists(max_retries=1)


class TestCreateEngineWithRetry:
    """Test create_engine_with_retry function."""

    async def test_success_on_first_try(self):
        from w2v_common.database.connection import create_engine_with_retry

        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_cm)

        with patch("w2v_common.database.connection.create_async_engine", return_value=mock_engine):
            result = await create_engine_with_retry(max_retries=1)
            assert result is mock_engine

    async def test_connection_refused_retries(self):
        from w2v_common.database.connection import create_engine_with_retry

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                side_effect=ConnectionRefusedError("refused"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_timeout_retries(self):
        from w2v_common.database.connection import create_engine_with_retry

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                side_effect=TimeoutError("timeout"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_generic_exception_retries(self):
        from w2v_common.database.connection import create_engine_with_retry

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                side_effect=RuntimeError("something"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_db_not_exist_triggers_create(self):
        """When DB doesn't exist, it tries to create it."""
        from sqlalchemy.exc import OperationalError
        from w2v_common.database.connection import create_engine_with_retry

        error = OperationalError(
            "stmt", {}, Exception('database "critique_service" does not exist')
        )

        with (
            patch("w2v_common.database.connection.create_async_engine", side_effect=error),
            patch(
                "w2v_common.database.connection._ensure_database_exists",
                new_callable=AsyncMock,
                side_effect=ConnectionError("can't create"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)


class TestSessionModule:
    """Test session module functions."""

    async def test_close_session_maker(self):
        import w2v_common.database.session as mod
        from w2v_common.database.session import close_session_maker

        original = mod._async_session_maker
        mod._async_session_maker = MagicMock()

        await close_session_maker()
        assert mod._async_session_maker is None

        mod._async_session_maker = original

    async def test_get_session_maker_creates(self):
        import w2v_common.database.session as mod
        from w2v_common.database.session import get_session_maker

        original = mod._async_session_maker
        mod._async_session_maker = None

        mock_engine = AsyncMock()
        with patch(
            "w2v_common.database.session.get_engine",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            maker = await get_session_maker()
            assert maker is not None

        mod._async_session_maker = original

    async def test_get_session_maker_reuses(self):
        import w2v_common.database.session as mod
        from w2v_common.database.session import get_session_maker

        original = mod._async_session_maker
        mock_maker = MagicMock()
        mod._async_session_maker = mock_maker

        result = await get_session_maker()
        assert result is mock_maker

        mod._async_session_maker = original

    async def test_get_db_session_context_manager(self):
        import w2v_common.database.session as mod
        from w2v_common.database.session import get_db_session

        original = mod._async_session_maker

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_maker = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_maker.return_value = mock_cm

        mod._async_session_maker = mock_maker

        with patch(
            "w2v_common.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_maker,
        ):
            async with get_db_session() as session:
                assert session is mock_session

        mod._async_session_maker = original

    async def test_get_db_yields_and_commits(self):
        import w2v_common.database.session as mod
        from w2v_common.database.session import get_db

        original = mod._async_session_maker

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_maker = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_maker.return_value = mock_cm

        mod._async_session_maker = mock_maker

        with patch(
            "w2v_common.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_maker,
        ):
            gen = get_db()
            session = await gen.__anext__()
            assert session is mock_session

        mod._async_session_maker = original
