"""Tests for database/connection.py and database/session.py -- covers connection retry, ensure_database_exists, session management."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError


def _make_async_engine_mock(conn_mock=None):
    """Create a mock engine whose begin() returns a proper async context manager."""
    if conn_mock is None:
        conn_mock = AsyncMock()
        conn_mock.execute = AsyncMock()

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=conn_mock)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_engine = MagicMock()
    mock_engine.begin.return_value = mock_ctx
    mock_engine.dispose = AsyncMock()
    return mock_engine


class TestEnsureDatabaseExists:
    """Test _ensure_database_exists function."""

    @pytest.mark.asyncio
    async def test_database_already_exists(self) -> None:
        from w2v_common.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # database exists
        mock_conn.execute.return_value = mock_result

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            await _ensure_database_exists(max_retries=1)

        # Verify engine was used: connect was called, then disposed
        mock_engine.connect.assert_called_once()
        mock_engine.dispose.assert_called_once()
        # The check query was executed and scalar() returned 1 (exists), so only 1 execute call
        mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_database(self) -> None:
        import w2v_common.database.connection as conn_mod
        from w2v_common.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        check_result = MagicMock()
        check_result.scalar.return_value = None  # database does not exist

        executed: list[str] = []

        def _execute(statement, *_args, **_kwargs):
            # A side_effect FUNCTION, not a fixed-length list: the ONLY thing that
            # matters is that call 1 is the existence probe and must return a result
            # whose .scalar() is None; every later call is provisioning DDL whose
            # return value is unused. A list pinned the exact statement COUNT, so a
            # legitimate extra statement in w2v_common (REVOKE CONNECT ... FROM
            # PUBLIC, the born-LOCKED policy) exhausted the iterator and the
            # StopIteration surfaced as "RuntimeError: coroutine raised StopIteration".
            executed.append(str(statement))
            return check_result if len(executed) == 1 else None

        mock_conn.execute.side_effect = _execute

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            await _ensure_database_exists(max_retries=1)

        # The point of this test: an absent database gets CREATED. Deliberately does
        # NOT assert the REVOKE text -- that would re-pin the exact statement list.
        assert executed, "no statement was executed at all"
        assert "pg_database" in executed[0], executed
        assert any(f"CREATE DATABASE {conn_mod.DB_NAME}" in stmt for stmt in executed), executed
        mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_database_already_exists_race_condition(self) -> None:
        from w2v_common.database.connection import _ensure_database_exists

        mock_conn = MagicMock()
        check_result = MagicMock()
        check_result.scalar.return_value = None
        # CREATE DATABASE raises "already exists"
        mock_conn.execute.side_effect = [
            check_result,
            ProgrammingError("stmt", {}, Exception("database already exists")),
        ]

        mock_engine = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            await _ensure_database_exists(max_retries=1)

        # Two execute calls: SELECT check + CREATE DATABASE (which raised "already exists")
        assert mock_conn.execute.call_count == 2
        mock_engine.dispose.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_db_name_raises(self) -> None:
        from w2v_common.database.connection import _ensure_database_exists

        with patch("w2v_common.database.connection.DB_NAME", "invalid;name"):
            with pytest.raises(ValueError, match="invalid characters"):
                await _ensure_database_exists(max_retries=1)

    @pytest.mark.asyncio
    async def test_connection_failure_retries(self) -> None:
        from w2v_common.database.connection import _ensure_database_exists

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = OperationalError("stmt", {}, Exception("refused"))
        mock_engine.dispose = MagicMock()

        with (
            patch("w2v_common.database.connection.create_engine", return_value=mock_engine),
            patch("w2v_common.database.connection.asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await _ensure_database_exists(max_retries=2)

    @pytest.mark.asyncio
    async def test_unexpected_error_raises(self) -> None:
        from w2v_common.database.connection import _ensure_database_exists

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("Unexpected")
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            with pytest.raises(RuntimeError, match="Unexpected"):
                await _ensure_database_exists(max_retries=1)


class TestCreateEngineWithRetry:
    """Test create_engine_with_retry function."""

    @pytest.mark.asyncio
    async def test_success_first_attempt(self) -> None:
        from w2v_common.database.connection import create_engine_with_retry

        mock_engine = _make_async_engine_mock()

        with patch(
            "w2v_common.database.connection.create_async_engine",
            return_value=mock_engine,
        ):
            result = await create_engine_with_retry(max_retries=1)
            assert result is mock_engine

    @pytest.mark.asyncio
    async def test_database_not_exists_triggers_create(self) -> None:
        from w2v_common.database.connection import create_engine_with_retry

        err = OperationalError(
            "stmt", {}, Exception('database "brand_extractor_test" does not exist')
        )

        # First engine attempt fails with db not exists
        mock_conn_fail = AsyncMock()
        mock_conn_fail.execute.side_effect = err
        mock_engine_fail = _make_async_engine_mock(mock_conn_fail)

        # After DB creation, second engine succeeds
        mock_engine_ok = _make_async_engine_mock()

        call_count = 0

        def create_engine_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_engine_fail
            return mock_engine_ok

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                side_effect=create_engine_side_effect,
            ),
            patch(
                "w2v_common.database.connection._ensure_database_exists",
                new_callable=AsyncMock,
            ),
            patch(
                "w2v_common.database.connection.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            result = await create_engine_with_retry(max_retries=2)
            assert result is mock_engine_ok

    @pytest.mark.asyncio
    async def test_timeout_retries(self) -> None:
        from w2v_common.database.connection import create_engine_with_retry

        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = TimeoutError("Connection timed out")
        mock_engine = _make_async_engine_mock(mock_conn)

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                return_value=mock_engine,
            ),
            patch(
                "w2v_common.database.connection.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    @pytest.mark.asyncio
    async def test_generic_exception_retries(self) -> None:
        from w2v_common.database.connection import create_engine_with_retry

        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = RuntimeError("Something broke")
        mock_engine = _make_async_engine_mock(mock_conn)

        with (
            patch(
                "w2v_common.database.connection.create_async_engine",
                return_value=mock_engine,
            ),
            patch(
                "w2v_common.database.connection.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(ConnectionError):
                await create_engine_with_retry(max_retries=2, retry_delay=0.01)


class TestGetEngine:
    @pytest.mark.asyncio
    async def test_get_engine_caches(self) -> None:
        import w2v_common.database.connection as mod

        old_engine = mod._engine
        try:
            mod._engine = None
            mock_engine = AsyncMock()
            with patch(
                "w2v_common.database.connection.create_engine_with_retry",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ):
                e1 = await mod.get_engine()
                e2 = await mod.get_engine()
                assert e1 is e2
        finally:
            mod._engine = old_engine

    @pytest.mark.asyncio
    async def test_get_engine_returns_existing(self) -> None:
        import w2v_common.database.connection as mod

        mock_engine = AsyncMock()
        old_engine = mod._engine
        try:
            mod._engine = mock_engine
            result = await mod.get_engine()
            assert result is mock_engine
        finally:
            mod._engine = old_engine


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_success(self) -> None:
        from w2v_common.database.connection import health_check

        mock_engine = _make_async_engine_mock()

        with patch(
            "w2v_common.database.connection.get_engine",
            new_callable=AsyncMock,
            return_value=mock_engine,
        ):
            result = await health_check()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self) -> None:
        from w2v_common.database.connection import health_check

        with patch(
            "w2v_common.database.connection.get_engine",
            new_callable=AsyncMock,
            side_effect=OSError("Connection refused"),
        ):
            result = await health_check()
            assert result is False


class TestCloseEngine:
    @pytest.mark.asyncio
    async def test_close_engine(self) -> None:
        import w2v_common.database.connection as mod

        mock_engine = AsyncMock()
        old_engine = mod._engine
        try:
            mod._engine = mock_engine
            await mod.close_engine()
            mock_engine.dispose.assert_awaited_once_with()
            assert mod._engine is None
        finally:
            mod._engine = old_engine

    @pytest.mark.asyncio
    async def test_close_engine_noop(self) -> None:
        import w2v_common.database.connection as mod

        old_engine = mod._engine
        try:
            mod._engine = None
            await mod.close_engine()  # should not raise
            assert mod._engine is None  # still None after noop
        finally:
            mod._engine = old_engine


class TestSessionManagement:
    """Tests for database/session.py."""

    @pytest.mark.asyncio
    async def test_get_session_maker_creates(self) -> None:
        import w2v_common.database.session as mod

        old_maker = mod._async_session_maker
        try:
            mod._async_session_maker = None
            mock_engine = AsyncMock()

            with patch(
                "w2v_common.database.session.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ):
                maker = await mod.get_session_maker()
                assert maker is not None
        finally:
            mod._async_session_maker = old_maker

    @pytest.mark.asyncio
    async def test_get_session_maker_caches(self) -> None:
        import w2v_common.database.session as mod

        old_maker = mod._async_session_maker
        try:
            mod._async_session_maker = None
            mock_engine = AsyncMock()

            with patch(
                "w2v_common.database.session.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ):
                m1 = await mod.get_session_maker()
                m2 = await mod.get_session_maker()
                assert m1 is m2
        finally:
            mod._async_session_maker = old_maker

    @pytest.mark.asyncio
    async def test_get_db_yields_session(self) -> None:
        import w2v_common.database.session as mod

        mock_session = AsyncMock()
        mock_maker = MagicMock()
        mock_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "w2v_common.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_maker,
        ):
            gen = mod.get_db()
            session = await gen.__anext__()
            assert session is mock_session
            with contextlib.suppress(StopAsyncIteration):
                await gen.__anext__()

    @pytest.mark.asyncio
    async def test_get_db_session_context_manager(self) -> None:
        import w2v_common.database.session as mod

        mock_session = AsyncMock()
        mock_maker = MagicMock()
        mock_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "w2v_common.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_maker,
        ):
            async with mod.get_db_session() as session:
                assert session is mock_session

    @pytest.mark.asyncio
    async def test_close_session_maker(self) -> None:
        import w2v_common.database.session as mod

        old_maker = mod._async_session_maker
        try:
            mod._async_session_maker = MagicMock()
            await mod.close_session_maker()
            assert mod._async_session_maker is None
        finally:
            mod._async_session_maker = old_maker
