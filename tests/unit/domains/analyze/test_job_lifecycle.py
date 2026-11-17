"""Unit tests for job lifecycle management: TTLCache, cancellation, repo methods."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from cachetools import TTLCache

from src.domains.analyze.routes import _check_cancelled

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# TTLCache behavior
# ---------------------------------------------------------------------------


class TestTTLCache:
    def test_ttl_cache_maxsize(self) -> None:
        cache: TTLCache = TTLCache(maxsize=3, ttl=3600)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        cache["d"] = 4  # evicts oldest
        assert len(cache) == 3
        assert "a" not in cache

    def test_ttl_cache_get_set(self) -> None:
        cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
        cache["job-1"] = {"status": "processing"}
        assert cache["job-1"]["status"] == "processing"

    def test_ttl_cache_missing_key(self) -> None:
        cache: TTLCache = TTLCache(maxsize=100, ttl=3600)
        assert cache.get("nonexistent") is None


# ---------------------------------------------------------------------------
# Cancellation check
# ---------------------------------------------------------------------------


class TestCheckCancelled:
    def test_not_cancelled(self) -> None:
        app = MagicMock()
        app.state.cancellation_tokens = {}
        # Should not raise
        _check_cancelled(app, "job-1")

    def test_cancelled_raises(self) -> None:
        app = MagicMock()
        app.state.cancellation_tokens = {"job-1": True}
        with pytest.raises(RuntimeError, match="cancelled"):
            _check_cancelled(app, "job-1")

    def test_missing_tokens_attr(self) -> None:
        app = MagicMock()
        # state exists (MagicMock auto-creates) but no cancellation_tokens
        app.state = MagicMock(spec=[])
        # getattr(app.state, "cancellation_tokens", {}) returns {} -> should not raise
        _check_cancelled(app, "job-1")


# ---------------------------------------------------------------------------
# Repository methods (cancel, list_by_tenant_filtered)
# ---------------------------------------------------------------------------


# ``db_session`` / ``db_session_factory`` come from the root conftest (NullPool +
# per-xdist-worker schema isolation). These two used to build their own engine inline,
# which skipped the ``_isolate_worker_schema`` connect listener and raced sibling
# TRUNCATEs under ``-n 4`` — see test_usage_repository.py for the deadlock detail.


async def test_repo_cancel_nonexistent(db_session: AsyncSession) -> None:
    """cancel() returns None for missing job."""
    from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository

    result = await AnalysisJobRepository.cancel(
        db_session, "nonexistent", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
    )
    assert result is None


async def test_repo_list_by_tenant_filtered_with_status(db_session_factory) -> None:
    """list_by_tenant_filtered with status filter returns matching jobs."""
    from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository

    async with db_session_factory() as session:
        await AnalysisJobRepository.create(session, "j1", "7eed672d-a7ff-5f16-87d7-b6a493fee523", 1)
        await AnalysisJobRepository.create(session, "j2", "7eed672d-a7ff-5f16-87d7-b6a493fee523", 2)
        await AnalysisJobRepository.update_status(
            session, "j1", "completed", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        await session.commit()

    async with db_session_factory() as session:
        jobs, total = await AnalysisJobRepository.list_by_tenant_filtered(
            session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", status="processing"
        )
        assert total == 1
        assert jobs[0].job_id == "j2"

        _all_jobs, all_total = await AnalysisJobRepository.list_by_tenant_filtered(
            session, "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert all_total == 2
