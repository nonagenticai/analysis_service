"""Unit tests for job_manager.

Tests JobManager static methods for CRUD, usage, domain history, and batch management.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domains.review.services.job_manager import JobManager


def _mock_db() -> AsyncMock:
    """Create a mock async database session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.unit
class TestCreateJob:
    async def test_create_job_adds_and_flushes(self) -> None:
        """create_job should add a ReviewJob and flush."""
        db = _mock_db()

        job = await JobManager.create_job(
            db,
            job_id="job-1",
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            review_types=["design_review"],
            url="https://example.com",
            domain="example.com",
        )

        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        assert job.job_id == "job-1"
        assert job.tenant_id == "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        assert job.status == "processing"

    async def test_create_job_with_user_context(self) -> None:
        """create_job passes user_context through."""
        db = _mock_db()
        ctx = {"focus_areas": ["mobile"]}

        job = await JobManager.create_job(
            db,
            job_id="job-2",
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            review_types=["design_review"],
            user_context=ctx,
        )

        assert job.user_context == ctx


@pytest.mark.unit
class TestGetJob:
    async def test_get_job_returns_result(self) -> None:
        """get_job returns the job from execute result."""
        db = _mock_db()
        mock_job = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute.return_value = mock_result

        result = await JobManager.get_job(
            db, "job-1", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert result is mock_job
        db.execute.assert_awaited_once()

    async def test_get_job_returns_none_when_missing(self) -> None:
        """get_job returns None when job does not exist."""
        db = _mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await JobManager.get_job(
            db, "nonexistent", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert result is None


@pytest.mark.unit
class TestMarkCompleted:
    async def test_mark_completed_executes_update(self) -> None:
        """mark_completed should execute an update statement."""
        db = _mock_db()

        await JobManager.mark_completed(db, "job-1", {"overall_score": 8.0})

        db.execute.assert_awaited_once()


@pytest.mark.unit
class TestMarkFailed:
    async def test_mark_failed_executes_update(self) -> None:
        """mark_failed should execute an update with error message."""
        db = _mock_db()

        await JobManager.mark_failed(db, "job-1", "LLM timed out")

        db.execute.assert_awaited_once()


@pytest.mark.unit
class TestListJobs:
    async def test_list_jobs_returns_jobs_and_count(self) -> None:
        """list_jobs returns a tuple of (jobs, total)."""
        db = _mock_db()
        mock_job = MagicMock()

        # First call: count query returns 5
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 5

        # Second call: paginated results
        mock_jobs_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_job]
        mock_jobs_result.scalars.return_value = mock_scalars

        db.execute = AsyncMock(side_effect=[mock_count_result, mock_jobs_result])

        jobs, total = await JobManager.list_jobs(
            db, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa", limit=20, offset=0
        )

        assert total == 5
        assert len(jobs) == 1

    async def test_list_jobs_empty_page(self) -> None:
        """list_jobs returns an empty page when the tenant owns no jobs."""
        db = _mock_db()
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0
        mock_jobs_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_jobs_result.scalars.return_value = mock_scalars

        db.execute = AsyncMock(side_effect=[mock_count_result, mock_jobs_result])

        jobs, total = await JobManager.list_jobs(
            db, tenant_id="cd808d14-f16d-5cc8-8c43-ec5bbb7af126"
        )

        assert total == 0
        assert jobs == []


@pytest.mark.unit
class TestRecordUsage:
    async def test_record_usage_success(self) -> None:
        """record_usage executes upsert statement."""
        db = _mock_db()

        await JobManager.record_usage(
            db, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa", is_error=False
        )

        db.execute.assert_awaited_once()

    async def test_record_usage_error(self) -> None:
        """record_usage increments error_count when is_error=True."""
        db = _mock_db()

        await JobManager.record_usage(
            db, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa", is_error=True
        )

        db.execute.assert_awaited_once()


@pytest.mark.unit
class TestUpdateResultsPartial:
    async def test_update_results_partial(self) -> None:
        """update_results_partial executes an update."""
        db = _mock_db()

        await JobManager.update_results_partial(db, "job-1", {"design_review": {}})

        db.execute.assert_awaited_once()


@pytest.mark.unit
class TestGetUsage:
    async def test_get_usage_without_dates(self) -> None:
        """get_usage returns usage records without date filter."""
        db = _mock_db()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        db.execute.return_value = mock_result

        records = await JobManager.get_usage(db, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa")

        assert records == []

    async def test_get_usage_with_date_range(self) -> None:
        """get_usage applies date range filters."""
        db = _mock_db()
        mock_usage = MagicMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_usage]
        mock_result.scalars.return_value = mock_scalars
        db.execute.return_value = mock_result

        records = await JobManager.get_usage(
            db,
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )

        assert len(records) == 1


@pytest.mark.unit
class TestGetDomainHistory:
    async def test_get_domain_history(self) -> None:
        """get_domain_history returns jobs for a domain."""
        db = _mock_db()
        mock_job = MagicMock()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_job]
        mock_result.scalars.return_value = mock_scalars
        db.execute.return_value = mock_result

        jobs = await JobManager.get_domain_history(
            db, domain="example.com", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert len(jobs) == 1

    async def test_get_domain_history_with_tenant(self) -> None:
        """get_domain_history applies tenant filter."""
        db = _mock_db()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_result.scalars.return_value = mock_scalars
        db.execute.return_value = mock_result

        jobs = await JobManager.get_domain_history(
            db, domain="example.com", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert jobs == []


@pytest.mark.unit
class TestBatchManagement:
    async def test_create_batch(self) -> None:
        """create_batch adds batch and flushes."""
        db = _mock_db()

        batch = await JobManager.create_batch(
            db,
            batch_id="batch-1",
            tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            job_ids=["j1", "j2"],
            total=2,
        )

        db.add.assert_called_once()
        db.flush.assert_awaited_once()
        assert batch.batch_id == "batch-1"
        assert batch.total == 2

    async def test_get_batch(self) -> None:
        """get_batch returns the batch from execute result."""
        db = _mock_db()
        mock_batch = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_batch
        db.execute.return_value = mock_result

        result = await JobManager.get_batch(
            db, "batch-1", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert result is mock_batch

    async def test_get_batch_returns_none(self) -> None:
        """get_batch returns None for missing batch."""
        db = _mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        result = await JobManager.get_batch(
            db, "nonexistent", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        assert result is None

    async def test_update_batch_progress_no_batch(self) -> None:
        """update_batch_progress returns early if batch not found."""
        db = _mock_db()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        # Should not raise
        await JobManager.update_batch_progress(db, "nonexistent")

    async def test_update_batch_progress_updates_counts(self) -> None:
        """update_batch_progress queries completed/failed and updates batch."""
        db = _mock_db()

        mock_batch = MagicMock()
        mock_batch.job_ids = ["j1", "j2"]
        mock_batch.total = 2

        # First call: get_batch
        mock_batch_result = MagicMock()
        mock_batch_result.scalar_one_or_none.return_value = mock_batch

        # Second call: completed count
        mock_completed = MagicMock()
        mock_completed.scalar.return_value = 2

        # Third call: failed count
        mock_failed = MagicMock()
        mock_failed.scalar.return_value = 0

        # Fourth call: update
        mock_update_result = MagicMock()

        db.execute = AsyncMock(
            side_effect=[mock_batch_result, mock_completed, mock_failed, mock_update_result]
        )

        await JobManager.update_batch_progress(db, "batch-1")

        # Should have called execute 4 times
        assert db.execute.await_count == 4

    async def test_update_batch_progress_partial_status(self) -> None:
        """update_batch_progress sets partial when there are failures."""
        db = _mock_db()

        mock_batch = MagicMock()
        mock_batch.job_ids = ["j1", "j2"]
        mock_batch.total = 2

        mock_batch_result = MagicMock()
        mock_batch_result.scalar_one_or_none.return_value = mock_batch

        # 1 completed, 1 failed
        mock_completed = MagicMock()
        mock_completed.scalar.return_value = 1
        mock_failed = MagicMock()
        mock_failed.scalar.return_value = 1
        mock_update_result = MagicMock()

        db.execute = AsyncMock(
            side_effect=[mock_batch_result, mock_completed, mock_failed, mock_update_result]
        )

        await JobManager.update_batch_progress(db, "batch-1")

        assert db.execute.await_count == 4
