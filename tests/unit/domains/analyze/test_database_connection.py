"""Unit tests for database connection module (src/database/connection.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError
from w2v_common.database import connection as conn_mod


@pytest.fixture(autouse=True)
def _reset_engine():
    """Ensure the module-level engine singleton is reset before/after each test."""
    original = conn_mod._engine
    conn_mod._engine = None
    yield
    conn_mod._engine = original


# ---------------------------------------------------------------------------
# _ensure_database_exists
# ---------------------------------------------------------------------------


class TestEnsureDatabaseExists:
    async def test_rejects_invalid_db_name(self):
        """Database names with invalid characters should raise ValueError."""
        with patch.object(conn_mod, "DB_NAME", "my-bad-name!"):
            with pytest.raises(ValueError, match="invalid characters"):
                await conn_mod._ensure_database_exists(max_retries=1)

    async def test_returns_when_db_already_exists(self):
        """Should return early if the database already exists."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1  # DB exists
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "valid_db"),
            patch.object(conn_mod, "create_engine", return_value=mock_engine),
        ):
            await conn_mod._ensure_database_exists(max_retries=1)
            # Should have queried pg_database
            mock_conn.execute.assert_called_once()

    async def test_creates_db_when_not_exists(self):
        """Should CREATE DATABASE when it does not exist."""
        mock_conn = MagicMock()

        check_result = MagicMock()
        check_result.scalar.return_value = None  # DB does not exist

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

        mock_conn.execute = MagicMock(side_effect=_execute)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "new_db"),
            patch.object(conn_mod, "create_engine", return_value=mock_engine),
        ):
            await conn_mod._ensure_database_exists(max_retries=1)

        # The point of this test: an absent database gets CREATED. Deliberately does
        # NOT assert the REVOKE text -- that would re-pin the exact statement list.
        assert executed, "no statement was executed at all"
        assert "pg_database" in executed[0], executed
        assert any("CREATE DATABASE new_db" in stmt for stmt in executed), executed

    async def test_handles_already_exists_race_condition(self):
        """ProgrammingError 'already exists' should not raise."""
        mock_conn = MagicMock()
        check_result = MagicMock()
        check_result.scalar.return_value = None

        mock_conn.execute = MagicMock(
            side_effect=[
                check_result,
                ProgrammingError("", [], Exception("database already exists")),
            ]
        )
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "new_db"),
            patch.object(conn_mod, "create_engine", return_value=mock_engine),
        ):
            # Should not raise
            await conn_mod._ensure_database_exists(max_retries=1)

    async def test_retries_on_operational_error(self):
        """Should retry on OperationalError and eventually raise ConnectionError."""
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(
            side_effect=OperationalError("", [], Exception("connection refused"))
        )
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "valid_db"),
            patch.object(conn_mod, "create_engine", return_value=mock_engine),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await conn_mod._ensure_database_exists(max_retries=2)

    async def test_unexpected_error_propagates(self):
        """Unexpected errors should propagate immediately."""
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock(side_effect=TypeError("unexpected"))
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "valid_db"),
            patch.object(conn_mod, "create_engine", return_value=mock_engine),
        ):
            with pytest.raises(TypeError, match="unexpected"):
                await conn_mod._ensure_database_exists(max_retries=1)

    async def test_handles_psycopg_url_prefix(self):
        """Should use a psycopg-compatible URL (not asyncpg) for the sync admin engine."""
        mock_conn = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 1
        mock_conn.execute.return_value = mock_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_conn
        mock_engine.dispose = MagicMock()

        with (
            patch.object(conn_mod, "DB_NAME", "valid_db"),
            patch.object(
                conn_mod,
                "DATABASE_URL",
                "postgresql+psycopg://user:pass@host:5432/valid_db",  # pragma: allowlist secret
            ),
            patch.object(conn_mod, "create_engine", return_value=mock_engine) as mock_create,
        ):
            await conn_mod._ensure_database_exists(max_retries=1)
            # psycopg v3 works with SQLAlchemy's sync create_engine using the
            # postgresql+psycopg:// dialect URL. Verify the async-only asyncpg
            # driver is NOT used for the sync admin connection, and that the
            # admin URL targets the DB_ADMIN_DB instead of the tenant DB.
            call_args = mock_create.call_args[0][0]
            assert not call_args.startswith("postgresql+asyncpg://")
            assert call_args.startswith(("postgresql://", "postgresql+psycopg://"))
            assert "/valid_db" not in call_args


# ---------------------------------------------------------------------------
# create_engine_with_retry
# ---------------------------------------------------------------------------


class TestCreateEngineWithRetry:
    async def test_success_on_first_attempt(self):
        """Should return engine on successful first attempt."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with patch.object(conn_mod, "create_async_engine", return_value=mock_engine):
            result = await conn_mod.create_engine_with_retry(max_retries=1, retry_delay=0.01)
            assert result is mock_engine

    async def test_retries_on_timeout_error(self):
        """Should retry on TimeoutError and eventually raise ConnectionError."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=TimeoutError("timed out"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with (
            patch.object(conn_mod, "create_async_engine", return_value=mock_engine),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await conn_mod.create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_retries_on_generic_exception(self):
        """Should retry on unexpected exceptions and eventually raise ConnectionError."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=RuntimeError("unknown"))
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with (
            patch.object(conn_mod, "create_async_engine", return_value=mock_engine),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            with pytest.raises(ConnectionError, match="Failed to connect"):
                await conn_mod.create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_database_not_exist_triggers_ensure_and_retry(self):
        """When database doesn't exist, should try to create it then connect."""
        # First engine: raises 'database does not exist'
        # _ensure_database_exists succeeds
        # Second engine: connection succeeds
        call_count = 0

        mock_conn_fail = AsyncMock()
        mock_conn_fail.execute = AsyncMock(
            side_effect=ProgrammingError("", [], Exception('database "x" does not exist'))
        )
        mock_conn_fail.__aenter__ = AsyncMock(return_value=mock_conn_fail)
        mock_conn_fail.__aexit__ = AsyncMock(return_value=False)

        mock_conn_ok = AsyncMock()
        mock_conn_ok.execute = AsyncMock()
        mock_conn_ok.__aenter__ = AsyncMock(return_value=mock_conn_ok)
        mock_conn_ok.__aexit__ = AsyncMock(return_value=False)

        mock_engine_fail = AsyncMock()
        mock_engine_fail.begin = MagicMock(return_value=mock_conn_fail)

        mock_engine_ok = AsyncMock()
        mock_engine_ok.begin = MagicMock(return_value=mock_conn_ok)

        engines = [mock_engine_fail, mock_engine_ok]

        def create_engine_side_effect(*args, **kwargs):
            nonlocal call_count
            e = engines[min(call_count, len(engines) - 1)]
            call_count += 1
            return e

        with (
            patch.object(conn_mod, "create_async_engine", side_effect=create_engine_side_effect),
            patch.object(conn_mod, "_ensure_database_exists", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await conn_mod.create_engine_with_retry(max_retries=3, retry_delay=0.01)
            assert result is mock_engine_ok


# ---------------------------------------------------------------------------
# get_engine / close_engine / health_check
# ---------------------------------------------------------------------------


class TestGetEngine:
    async def test_creates_and_caches_engine(self):
        mock_engine = AsyncMock()
        with patch.object(
            conn_mod, "create_engine_with_retry", new_callable=AsyncMock, return_value=mock_engine
        ):
            engine = await conn_mod.get_engine()
            assert engine is mock_engine
            assert conn_mod._engine is mock_engine

    async def test_returns_cached_engine(self):
        mock_engine = AsyncMock()
        conn_mod._engine = mock_engine
        result = await conn_mod.get_engine()
        assert result is mock_engine


class TestCloseEngine:
    async def test_disposes_and_resets(self):
        mock_engine = AsyncMock()
        mock_engine.dispose = AsyncMock()
        conn_mod._engine = mock_engine

        await conn_mod.close_engine()
        mock_engine.dispose.assert_awaited_once()
        assert conn_mod._engine is None

    async def test_noop_when_no_engine(self):
        conn_mod._engine = None
        await conn_mod.close_engine()  # Should not raise


class TestHealthCheck:
    async def test_returns_true_on_success(self):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=False)

        mock_engine = AsyncMock()
        mock_engine.begin = MagicMock(return_value=mock_conn)
        conn_mod._engine = mock_engine

        with patch.object(conn_mod, "get_engine", new_callable=AsyncMock, return_value=mock_engine):
            result = await conn_mod.health_check()
            assert result is True

    async def test_returns_false_on_failure(self):
        with patch.object(
            conn_mod, "get_engine", new_callable=AsyncMock, side_effect=RuntimeError("down")
        ):
            result = await conn_mod.health_check()
            assert result is False
