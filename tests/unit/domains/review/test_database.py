"""Unit tests for database connection handling.

Tests create_engine_with_retry, _ensure_database_exists, and connection lifecycle.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError


@pytest.mark.unit
class TestCreateEngineWithRetry:
    """Tests for create_engine_with_retry function."""

    async def test_engine_creation_with_valid_url(self) -> None:
        """Test successful engine creation on first attempt."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_engine.begin = MagicMock(return_value=mock_conn)

        with patch(
            "w2v_common.database.connection.create_async_engine",
            return_value=mock_engine,
        ):
            from w2v_common.database.connection import create_engine_with_retry

            engine = await create_engine_with_retry(max_retries=1, retry_delay=0.01)
            assert engine is mock_engine

    async def test_connection_retry_on_operational_error(self) -> None:
        """Test that OperationalError triggers retry with exponential backoff."""
        mock_engine_bad = AsyncMock()
        mock_conn_bad = AsyncMock()
        mock_conn_bad.execute = AsyncMock(
            side_effect=OperationalError("connection refused", None, Exception("refused"))
        )
        mock_conn_bad.__aenter__ = AsyncMock(return_value=mock_conn_bad)
        mock_conn_bad.__aexit__ = AsyncMock(return_value=None)
        mock_engine_bad.begin = MagicMock(return_value=mock_conn_bad)

        mock_engine_good = AsyncMock()
        mock_conn_good = AsyncMock()
        mock_conn_good.execute = AsyncMock()
        mock_conn_good.__aenter__ = AsyncMock(return_value=mock_conn_good)
        mock_conn_good.__aexit__ = AsyncMock(return_value=None)
        mock_engine_good.begin = MagicMock(return_value=mock_conn_good)

        call_count = 0

        def _create_engine_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_engine_bad
            return mock_engine_good

        with patch(
            "w2v_common.database.connection.create_async_engine",
            side_effect=_create_engine_side_effect,
        ):
            from w2v_common.database.connection import create_engine_with_retry

            engine = await create_engine_with_retry(max_retries=3, retry_delay=0.01)
            assert engine is mock_engine_good
            assert call_count == 2

    async def test_ensure_database_exists_creates_missing_db(self) -> None:
        """Test _ensure_database_exists creates database when it does not exist."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        # First query: SELECT 1 FROM pg_database returns None (DB doesn't exist)
        mock_result = MagicMock()
        mock_result.scalar.return_value = None
        mock_conn.execute = MagicMock(return_value=mock_result)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            from w2v_common.database.connection import _ensure_database_exists

            await _ensure_database_exists(max_retries=1)

            # Should have called execute at least twice (SELECT check + CREATE DATABASE)
            assert mock_conn.execute.call_count >= 2

    async def test_connection_pool_timeout_raises_after_max_retries(self) -> None:
        """Test that exhausting retries raises ConnectionError."""
        mock_engine = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=TimeoutError("pool timeout"))
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
            from w2v_common.database.connection import create_engine_with_retry

            await create_engine_with_retry(max_retries=2, retry_delay=0.01)

    async def test_ensure_database_exists_handles_already_exists(self) -> None:
        """Test _ensure_database_exists handles race condition where DB already exists."""
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_result = MagicMock()
        # SELECT returns None (not found), but CREATE raises "already exists"
        mock_result.scalar.return_value = None

        call_count = 0

        def execute_side_effect(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_result
            raise ProgrammingError(
                "database already exists",
                None,
                Exception("database already exists"),
            )

        mock_conn.execute = MagicMock(side_effect=execute_side_effect)
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=None)
        mock_engine.connect = MagicMock(return_value=mock_conn)
        mock_engine.dispose = MagicMock()

        with patch("w2v_common.database.connection.create_engine", return_value=mock_engine):
            from w2v_common.database.connection import _ensure_database_exists

            # Should not raise -- "already exists" is handled gracefully
            await _ensure_database_exists(max_retries=1)
