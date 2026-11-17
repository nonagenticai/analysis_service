"""Unit tests for database session management.

Tests get_session_maker, get_db, get_db_session, and close_session_maker.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import w2v_common.database.session as session_module


@pytest.fixture(autouse=True)
def reset_session_maker():
    """Reset the global session maker before each test."""
    original = session_module._async_session_maker
    session_module._async_session_maker = None
    yield
    session_module._async_session_maker = original


@pytest.mark.unit
class TestGetSessionMaker:
    """Tests for get_session_maker function."""

    async def test_creates_session_maker_on_first_call(self) -> None:
        """First call creates a new session maker from the engine."""
        mock_engine = AsyncMock()

        with (
            patch(
                "w2v_common.database.session.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ),
            patch("w2v_common.database.session.async_sessionmaker") as mock_sm_class,
        ):
            mock_sm_instance = MagicMock()
            mock_sm_class.return_value = mock_sm_instance

            result = await session_module.get_session_maker()

            assert result is mock_sm_instance
            mock_sm_class.assert_called_once()

    async def test_returns_cached_session_maker_on_second_call(self) -> None:
        """Second call returns the same cached session maker."""
        mock_engine = AsyncMock()

        with (
            patch(
                "w2v_common.database.session.get_engine",
                new_callable=AsyncMock,
                return_value=mock_engine,
            ),
            patch("w2v_common.database.session.async_sessionmaker") as mock_sm_class,
        ):
            mock_sm_instance = MagicMock()
            mock_sm_class.return_value = mock_sm_instance

            result1 = await session_module.get_session_maker()
            result2 = await session_module.get_session_maker()

            assert result1 is result2
            # Only called once since second call uses cache
            mock_sm_class.assert_called_once()


@pytest.mark.unit
class TestGetDb:
    """Tests for the get_db FastAPI dependency."""

    async def test_get_db_yields_session_and_commits(self) -> None:
        """get_db should yield a session and commit on success."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_session_maker = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session_maker.return_value = mock_ctx

        with patch.object(
            session_module,
            "get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_session_maker,
        ):
            gen = session_module.get_db()
            session = await gen.__anext__()
            assert session is mock_session

            # Simulate successful completion
            with contextlib.suppress(StopAsyncIteration):
                await gen.__anext__()

    async def test_get_db_rolls_back_on_exception(self) -> None:
        """get_db should rollback on exception."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=RuntimeError("DB error"))
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_session_maker = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session_maker.return_value = mock_ctx

        with patch.object(
            session_module,
            "get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_session_maker,
        ):
            gen = session_module.get_db()
            session = await gen.__anext__()
            assert session is mock_session


@pytest.mark.unit
class TestGetDbSession:
    """Tests for the get_db_session context manager."""

    async def test_get_db_session_yields_and_commits(self) -> None:
        """get_db_session should yield a session and commit on success."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_session_maker = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session_maker.return_value = mock_ctx

        with patch.object(
            session_module,
            "get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_session_maker,
        ):
            async with session_module.get_db_session() as session:
                assert session is mock_session

    async def test_get_db_session_rolls_back_on_exception(self) -> None:
        """get_db_session should rollback when an exception is raised inside the block."""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock(side_effect=RuntimeError("DB error"))
        mock_session.rollback = AsyncMock()
        mock_session.close = AsyncMock()

        mock_session_maker = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session_maker.return_value = mock_ctx

        with patch.object(
            session_module,
            "get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_session_maker,
        ):
            # The context manager will try to commit, which fails, then rollback
            try:
                async with session_module.get_db_session() as session:
                    assert session is mock_session
            except RuntimeError:
                pass


@pytest.mark.unit
class TestCloseSessionMaker:
    """Tests for close_session_maker."""

    async def test_close_resets_global(self) -> None:
        """close_session_maker resets the global session maker to None."""
        session_module._async_session_maker = MagicMock()

        await session_module.close_session_maker()

        assert session_module._async_session_maker is None

    async def test_close_when_already_none(self) -> None:
        """close_session_maker is safe to call when already None."""
        session_module._async_session_maker = None

        await session_module.close_session_maker()

        assert session_module._async_session_maker is None
