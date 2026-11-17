"""Tests for src.domains.review.services.cleanup -- background job cleanup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.domains.review.services.cleanup import cleanup_old_jobs


def _db_session(execute: AsyncMock) -> AsyncMock:
    db = AsyncMock()
    db.execute = execute
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=None)
    return db


def _scalar(value: int) -> MagicMock:
    result = MagicMock()
    result.scalar = MagicMock(return_value=value)
    return result


class TestCleanupOldJobs:
    async def test_prunes_before_the_first_sleep(self):
        """The first pass must run BEFORE the first sleep.

        Regression: the loop used to ``await asyncio.sleep(...)`` at the top of
        the body, so a pod restarting more often than ``interval_minutes`` never
        completed a single prune. Cancelling at the first sleep must therefore
        still leave one executed prune behind.
        """
        execute = AsyncMock(return_value=_scalar(5))
        with (
            patch(
                "src.domains.review.services.cleanup.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
            patch(
                "src.domains.review.services.cleanup.get_db_session",
                return_value=_db_session(execute),
            ),
        ):
            await cleanup_old_jobs(retention_hours=72, interval_minutes=60)

        execute.assert_awaited_once()
        assert execute.await_args is not None
        sql = str(execute.await_args.args[0])
        assert "prune_review_jobs" in sql

    async def test_passes_the_retention_cutoff(self):
        """The cutoff handed to the resolver must reflect ``retention_hours``."""
        execute = AsyncMock(return_value=_scalar(0))
        with (
            patch(
                "src.domains.review.services.cleanup.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
            patch(
                "src.domains.review.services.cleanup.get_db_session",
                return_value=_db_session(execute),
            ),
        ):
            await cleanup_old_jobs(retention_hours=72, interval_minutes=60)

        from datetime import UTC, datetime, timedelta

        assert execute.await_args is not None
        cutoff = execute.await_args.args[1]["cutoff"]
        expected = datetime.now(UTC) - timedelta(hours=72)
        assert abs((cutoff - expected).total_seconds()) < 60

    async def test_cleanup_handles_cancelled_error(self):
        """CancelledError should break the loop gracefully, not propagate."""
        execute = AsyncMock(return_value=_scalar(0))
        with (
            patch(
                "src.domains.review.services.cleanup.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
            patch(
                "src.domains.review.services.cleanup.get_db_session",
                return_value=_db_session(execute),
            ),
        ):
            await cleanup_old_jobs(retention_hours=72, interval_minutes=60)

    async def test_cleanup_handles_db_error(self):
        """A failing pass is logged, and the loop still reaches its sleep."""
        call_count = 0

        async def mock_sleep(_seconds):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        execute = AsyncMock(side_effect=RuntimeError("DB error"))
        with (
            patch("src.domains.review.services.cleanup.asyncio.sleep", side_effect=mock_sleep),
            patch(
                "src.domains.review.services.cleanup.get_db_session",
                return_value=_db_session(execute),
            ),
        ):
            await cleanup_old_jobs(retention_hours=72, interval_minutes=60)

        execute.assert_awaited_once()
        assert call_count == 1
