"""Tests for CritiqueService orchestrator."""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.critique.services.critique_service import ALL_CATEGORIES, CritiqueService


class TestCritiqueServiceInit:
    """Test CritiqueService initialization."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    def test_init_creates_scanners(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        assert svc._seo is not None
        assert svc._security is not None
        assert svc._a11y is not None
        assert svc._performance is not None
        assert svc._ux is not None
        assert svc._code_quality is not None

    @patch("src.domains.critique.services.critique_service.get_settings")
    def test_init_no_adapters_when_disabled(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        assert svc._adapter_registry is None


class TestCritiqueServiceCreateJob:
    """Test create_job method."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_create_job_returns_job_id(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        with patch(
            "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
        ):
            job_id = await svc.create_job(
                db,
                tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
                url="https://example.com",
                categories=["seo", "security"],
                max_pages=1,
            )

        assert isinstance(job_id, str)
        # Should be a valid UUID
        uuid.UUID(job_id)
        # db.add should have been called (for the CritiqueJob)
        assert db.add.called
        # db.commit should have been called at least once
        assert db.commit.await_count >= 1

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_create_job_with_callback(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        with patch(
            "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
        ):
            job_id = await svc.create_job(
                db,
                tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
                url="https://example.com",
                callback_url="https://hooks.example.com/webhook",
            )

        assert isinstance(job_id, str)

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_create_job_with_github_repo(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        with patch(
            "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
        ):
            job_id = await svc.create_job(
                db,
                tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
                url="https://example.com",
                github_repo="https://github.com/org/repo",
            )

        assert isinstance(job_id, str)


class TestCritiqueServiceGetJob:
    """Test get_job and list_jobs methods."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_get_job_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "test-job-id"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_job(
            db, "test-job-id", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result == mock_job

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_get_job_not_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_job(
            db, "nonexistent", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result is None

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_list_jobs(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        # Mock count query
        count_result = MagicMock()
        count_result.scalar.return_value = 5

        # Mock list query
        mock_jobs = [MagicMock() for _ in range(5)]
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = mock_jobs

        db.execute = AsyncMock(side_effect=[count_result, list_result])

        jobs, total = await svc.list_jobs(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", page=1, per_page=20
        )
        assert total == 5
        assert len(jobs) == 5

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_list_jobs_with_status_filter(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        count_result = MagicMock()
        count_result.scalar.return_value = 2

        mock_jobs = [MagicMock(), MagicMock()]
        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = mock_jobs

        db.execute = AsyncMock(side_effect=[count_result, list_result])

        jobs, total = await svc.list_jobs(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", page=1, per_page=20, status="completed"
        )
        assert total == 2
        assert len(jobs) == 2


class TestCritiqueServiceDeleteJob:
    """Test delete_job method."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_delete_job_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_job = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        result = await svc.delete_job(
            db, "test-job-id", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result is True
        db.delete.assert_awaited_once_with(mock_job)

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_delete_job_not_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.delete_job(
            db, "nonexistent", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result is False


class TestCritiqueServiceBatch:
    """Test batch operations."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_create_batch(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        with patch(
            "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
        ):
            batch_id, job_ids = await svc.create_batch(
                db,
                tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
                urls=["https://a.com", "https://b.com"],
                categories=["seo"],
            )

        assert isinstance(batch_id, str)
        assert len(job_ids) == 2
        for jid in job_ids:
            uuid.UUID(jid)

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_get_batch_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_batch = MagicMock()
        mock_batch.batch_id = "batch-123"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_batch
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_batch(
            db, "batch-123", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result == mock_batch

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_get_batch_not_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_batch(
            db, "nonexistent", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert result is None


class TestCritiqueServiceUpdateBatchStatus:
    """Test update_batch_status."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_update_batch_all_completed(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.commit = AsyncMock()

        # Mock get_batch
        mock_batch = MagicMock()
        mock_batch.job_ids = ["j1", "j2"]
        mock_batch.total = 2
        mock_batch.completed = 0
        mock_batch.failed = 0
        mock_batch.status = "pending"

        batch_result = MagicMock()
        batch_result.scalar_one_or_none.return_value = mock_batch

        # Mock _get_job for each job
        job1 = MagicMock()
        job1.status = "completed"
        job2 = MagicMock()
        job2.status = "completed"

        job1_result = MagicMock()
        job1_result.scalar_one_or_none.return_value = job1
        job2_result = MagicMock()
        job2_result.scalar_one_or_none.return_value = job2

        db.execute = AsyncMock(side_effect=[batch_result, job1_result, job2_result])

        await svc.update_batch_status(
            db, "batch-123", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert mock_batch.completed == 2
        assert mock_batch.status == "completed"

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_update_batch_partial(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.commit = AsyncMock()

        mock_batch = MagicMock()
        mock_batch.job_ids = ["j1", "j2", "j3"]
        mock_batch.total = 3
        mock_batch.completed = 0
        mock_batch.failed = 0
        mock_batch.status = "pending"

        batch_result = MagicMock()
        batch_result.scalar_one_or_none.return_value = mock_batch

        job1 = MagicMock()
        job1.status = "completed"
        job2 = MagicMock()
        job2.status = "running"
        job3 = MagicMock()
        job3.status = "failed"

        job_results = []
        for j in [job1, job2, job3]:
            r = MagicMock()
            r.scalar_one_or_none.return_value = j
            job_results.append(r)

        db.execute = AsyncMock(side_effect=[batch_result, *job_results])

        await svc.update_batch_status(
            db, "batch-123", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        assert mock_batch.completed == 1
        assert mock_batch.failed == 1
        assert mock_batch.status == "running"

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_update_batch_not_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        batch_result = MagicMock()
        batch_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=batch_result)

        # Should not raise
        await svc.update_batch_status(
            db, "nonexistent", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )


class TestCritiqueServicePrivateHelpers:
    """Test private helper methods."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_fetch_html(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.status_code = 200
        # ``_fetch_html`` now follows redirects MANUALLY (follow_redirects=False)
        # so it can re-run the SSRF guard on every hop. That makes
        # ``response.is_redirect`` load-bearing, and an unset AsyncMock attribute
        # is a truthy Mock -- which would send this happy-path fixture down the
        # redirect branch and blow up on a Mock ``location``. Pin it.
        mock_response.is_redirect = False

        async def aiter_bytes():
            yield b"<html><body>Hello</body></html>"

        mock_response.aiter_bytes = aiter_bytes

        mock_stream_ctx = AsyncMock()
        mock_stream_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.stream = MagicMock(return_value=mock_stream_ctx)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            html = await svc._fetch_html("https://example.com")

        assert html == "<html><body>Hello</body></html>"

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_scanner_with_timeout_success(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.max_retries = 2
        svc = CritiqueService()

        findings = [{"finding_id": "f1", "title": "Test"}]

        async def scanner_coro():
            return findings

        result, elapsed = await svc._run_scanner_with_timeout("seo", scanner_coro, timeout=10)
        assert result == findings
        assert elapsed >= 0

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_scanner_with_timeout_failure(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.max_retries = 1
        svc = CritiqueService()

        async def failing_scanner():
            raise ValueError("boom")

        result, elapsed = await svc._run_scanner_with_timeout("seo", failing_scanner, timeout=10)
        # Should return a list with a single error finding
        assert len(result) == 1
        assert "failed" in result[0]["title"].lower()
        assert result[0]["severity"] == "info"
        assert elapsed >= 0

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_scanner_with_timeout_timeout(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.max_retries = 1
        svc = CritiqueService()

        async def slow_scanner():
            await asyncio.sleep(100)
            return []

        result, elapsed = await svc._run_scanner_with_timeout("sec", slow_scanner, timeout=0.01)
        assert len(result) == 1
        assert "failed" in result[0]["title"].lower()
        assert elapsed >= 0

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_deliver_webhook_no_callback_url(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        job = MagicMock()
        job.callback_url = None

        # Should not raise
        await svc._deliver_webhook(job)

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_deliver_webhook_with_callback(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        job = MagicMock()
        job.callback_url = "https://hooks.example.com/callback"
        job.job_id = "job-123"
        job.status = "completed"
        job.url = "https://example.com"
        job.category_scores = {"seo": 85.0}
        job.overall_score = 85.0
        job.findings = [{"finding_id": "f1"}]
        job.summary = "Summary"

        with patch(
            "src.domains.critique.services.webhook.deliver_webhook", new_callable=AsyncMock
        ) as mock_deliver:
            await svc._deliver_webhook(job)
            mock_deliver.assert_awaited_once()

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_save_findings(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()

        findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "high",
                "title": "Test",
                "description": "desc",
                "url": "https://example.com",
            },
            {
                "finding_id": "f2",
                "category": "security",
                "severity": "critical",
                "title": "CSP",
                "description": "Missing CSP",
            },
        ]

        await svc._save_findings(
            db, "job-123", findings, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )
        assert db.add.call_count == 2
        assert [row.tenant_id for (row,), _ in db.add.call_args_list] == [
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
        ]

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_record_usage_new(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        await svc._record_usage(
            db,
            tenant_id="4be0643f-1d98-573b-97cd-ca98a65347dd",
            pages_scanned=1,
            findings_count=5,
            is_error=False,
        )
        # Should add a new usage record
        db.add.assert_called_once()

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_record_usage_existing(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        existing = MagicMock()
        existing.scan_count = 5
        existing.pages_scanned = 5
        existing.findings_count = 20
        existing.error_count = 0

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        db.execute = AsyncMock(return_value=mock_result)

        await svc._record_usage(
            db,
            tenant_id="4be0643f-1d98-573b-97cd-ca98a65347dd",
            pages_scanned=1,
            findings_count=3,
            is_error=True,
        )
        assert existing.scan_count == 6
        assert existing.pages_scanned == 6
        assert existing.findings_count == 23
        assert existing.error_count == 1

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_update_progress(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with patch("src.shared.cache.redis_client.get_redis_client", return_value=mock_client):
            await svc._update_progress(
                "job-123",
                status="scanning",
                completed=2,
                total=5,
                current_scanner="seo",
            )
        mock_client.set.assert_awaited_once()

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_update_progress_redis_failure(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        with patch(
            "src.shared.cache.redis_client.get_redis_client",
            side_effect=RuntimeError("Redis down"),
        ):
            # Should not raise -- it logs and continues
            await svc._update_progress("job-123", status="scanning")


class TestProcessJob:
    """Test the main process_job orchestration."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_not_found(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(ValueError, match="Job not found"):
            await svc.process_job(db, "nonexistent", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_cache_hit(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.status = "pending"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        cached_data = {
            "findings": [{"finding_id": "f1"}],
            "category_scores": {"seo": 90.0},
            "overall_score": 90.0,
            "confidence_scores": {"seo": 0.9},
            "summary": "Cached summary",
            "report_markdown": "# Cached Report",
            "pages_scanned": 1,
        }

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.get_cached_result",
                new_callable=AsyncMock,
                return_value=cached_data,
            ),
        ):
            await svc.process_job(db, "job-123", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "completed"
        assert mock_job.findings == cached_data["findings"]
        assert mock_job.overall_score == 90.0


class TestProcessJobFullExecution:
    """Test process_job main execution path (no cache, run scanners)."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_full_run(self, mock_settings):
        """Full process_job: fetch HTML, run scanners, produce report."""
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.seo_timeout = 30
        mock_settings.return_value.scanners.a11y_timeout = 30
        mock_settings.return_value.scanners.performance_timeout = 30
        mock_settings.return_value.scanners.security_timeout = 30
        mock_settings.return_value.scanners.ux_timeout = 30
        mock_settings.return_value.scanners.code_quality_timeout = 30
        mock_settings.return_value.scanners.overall_timeout = 120
        mock_settings.return_value.scanners.max_retries = 1
        mock_settings.return_value.scoring.critical_weight = 30
        mock_settings.return_value.scoring.high_weight = 15
        mock_settings.return_value.scoring.medium_weight = 7
        mock_settings.return_value.scoring.low_weight = 3
        mock_settings.return_value.scoring.info_weight = 0
        mock_settings.return_value.scoring.decay_k = 25
        mock_settings.return_value.scoring.a11y_weight = 0.25
        mock_settings.return_value.scoring.security_weight = 0.25
        mock_settings.return_value.scoring.performance_weight = 0.20
        mock_settings.return_value.scoring.seo_weight = 0.15
        mock_settings.return_value.scoring.ux_weight = 0.15

        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-456"
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        seo_findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "medium",
                "title": "Missing meta description",
                "description": "No meta desc",
                "url": "https://example.com",
            }
        ]

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.get_cached_result",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.domains.critique.services.scan_cache.set_cached_result",
                new_callable=AsyncMock,
            ),
            patch.object(
                svc, "_fetch_html", new_callable=AsyncMock, return_value="<html>Test</html>"
            ),
            patch.object(
                svc,
                "_run_scanner_with_timeout",
                new_callable=AsyncMock,
                return_value=(seo_findings, 0.1),
            ),
            patch.object(svc, "_update_progress", new_callable=AsyncMock),
            patch.object(svc, "_save_findings", new_callable=AsyncMock),
            patch.object(svc, "_record_usage", new_callable=AsyncMock),
            patch(
                "src.domains.critique.services.critique_service.detect_repo_url",
                return_value=MagicMock(url=None, confidence="low"),
            ),
            patch(
                "src.domains.critique.services.critique_service.generate_summary",
                new_callable=AsyncMock,
                return_value=("Summary text", None),
            ),
        ):
            await svc.process_job(db, "job-456", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "completed"
        assert mock_job.findings is not None
        assert mock_job.overall_score is not None
        assert mock_job.scan_duration is not None

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_scanner_exception(self, mock_settings):
        """Process job handles scanner failure gracefully."""
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.seo_timeout = 30
        mock_settings.return_value.scanners.a11y_timeout = 30
        mock_settings.return_value.scanners.performance_timeout = 30
        mock_settings.return_value.scanners.security_timeout = 30
        mock_settings.return_value.scanners.ux_timeout = 30
        mock_settings.return_value.scanners.code_quality_timeout = 30
        mock_settings.return_value.scanners.overall_timeout = 120
        mock_settings.return_value.scanners.max_retries = 1
        mock_settings.return_value.scoring.critical_weight = 30
        mock_settings.return_value.scoring.high_weight = 15
        mock_settings.return_value.scoring.medium_weight = 7
        mock_settings.return_value.scoring.low_weight = 3
        mock_settings.return_value.scoring.info_weight = 0
        mock_settings.return_value.scoring.decay_k = 25
        mock_settings.return_value.scoring.a11y_weight = 0.25
        mock_settings.return_value.scoring.security_weight = 0.25
        mock_settings.return_value.scoring.performance_weight = 0.20
        mock_settings.return_value.scoring.seo_weight = 0.15
        mock_settings.return_value.scoring.ux_weight = 0.15

        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-err"
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.progress = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.get_cached_result",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                svc,
                "_fetch_html",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Network error"),
            ),
            patch.object(svc, "_update_progress", new_callable=AsyncMock),
            patch.object(svc, "_record_usage", new_callable=AsyncMock),
        ):
            await svc.process_job(db, "job-err", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "failed"
        assert "Network error" in mock_job.error_message

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_with_callback(self, mock_settings):
        """Process job with callback URL creates webhook task."""
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.seo_timeout = 30
        mock_settings.return_value.scanners.a11y_timeout = 30
        mock_settings.return_value.scanners.performance_timeout = 30
        mock_settings.return_value.scanners.security_timeout = 30
        mock_settings.return_value.scanners.ux_timeout = 30
        mock_settings.return_value.scanners.code_quality_timeout = 30
        mock_settings.return_value.scanners.overall_timeout = 120
        mock_settings.return_value.scanners.max_retries = 1
        mock_settings.return_value.scoring.critical_weight = 30
        mock_settings.return_value.scoring.high_weight = 15
        mock_settings.return_value.scoring.medium_weight = 7
        mock_settings.return_value.scoring.low_weight = 3
        mock_settings.return_value.scoring.info_weight = 0
        mock_settings.return_value.scoring.decay_k = 25
        mock_settings.return_value.scoring.a11y_weight = 0.25
        mock_settings.return_value.scoring.security_weight = 0.25
        mock_settings.return_value.scoring.performance_weight = 0.20
        mock_settings.return_value.scoring.seo_weight = 0.15
        mock_settings.return_value.scoring.ux_weight = 0.15

        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-cb"
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = "https://hooks.example.com/cb"
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.get_cached_result",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.domains.critique.services.scan_cache.set_cached_result", new_callable=AsyncMock
            ),
            patch.object(svc, "_fetch_html", new_callable=AsyncMock, return_value="<html></html>"),
            patch.object(
                svc, "_run_scanner_with_timeout", new_callable=AsyncMock, return_value=([], 0.1)
            ),
            patch.object(svc, "_update_progress", new_callable=AsyncMock),
            patch.object(svc, "_save_findings", new_callable=AsyncMock),
            patch.object(svc, "_record_usage", new_callable=AsyncMock),
            patch(
                "src.domains.critique.services.critique_service.detect_repo_url",
                return_value=MagicMock(url=None, confidence="low"),
            ),
            patch(
                "src.domains.critique.services.critique_service.generate_summary",
                new_callable=AsyncMock,
                return_value=("Summary", None),
            ),
            patch("asyncio.create_task") as mock_create_task,
        ):
            await svc.process_job(db, "job-cb", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "completed"
        mock_create_task.assert_called_once()

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_force_rescan(self, mock_settings):
        """Force rescan skips cache check."""
        mock_settings.return_value.external_tools.enabled = False
        mock_settings.return_value.scanners.seo_timeout = 30
        mock_settings.return_value.scanners.a11y_timeout = 30
        mock_settings.return_value.scanners.performance_timeout = 30
        mock_settings.return_value.scanners.security_timeout = 30
        mock_settings.return_value.scanners.ux_timeout = 30
        mock_settings.return_value.scanners.code_quality_timeout = 30
        mock_settings.return_value.scanners.overall_timeout = 120
        mock_settings.return_value.scanners.max_retries = 1
        mock_settings.return_value.scoring.critical_weight = 30
        mock_settings.return_value.scoring.high_weight = 15
        mock_settings.return_value.scoring.medium_weight = 7
        mock_settings.return_value.scoring.low_weight = 3
        mock_settings.return_value.scoring.info_weight = 0
        mock_settings.return_value.scoring.decay_k = 25
        mock_settings.return_value.scoring.a11y_weight = 0.25
        mock_settings.return_value.scoring.security_weight = 0.25
        mock_settings.return_value.scoring.performance_weight = 0.20
        mock_settings.return_value.scoring.seo_weight = 0.15
        mock_settings.return_value.scoring.ux_weight = 0.15

        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-force"
        mock_job.url = "https://example.com"
        mock_job.categories = None  # all categories
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_job
        db.execute = AsyncMock(return_value=mock_result)

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.set_cached_result", new_callable=AsyncMock
            ),
            patch.object(svc, "_fetch_html", new_callable=AsyncMock, return_value="<html></html>"),
            patch.object(
                svc, "_run_scanner_with_timeout", new_callable=AsyncMock, return_value=([], 0.1)
            ),
            patch.object(svc, "_update_progress", new_callable=AsyncMock),
            patch.object(svc, "_save_findings", new_callable=AsyncMock),
            patch.object(svc, "_record_usage", new_callable=AsyncMock),
            patch(
                "src.domains.critique.services.critique_service.detect_repo_url",
                return_value=MagicMock(url="https://github.com/org/repo", confidence="high"),
            ),
            patch(
                "src.domains.critique.services.critique_service.generate_summary",
                new_callable=AsyncMock,
                return_value=("Summary", None),
            ),
        ):
            await svc.process_job(
                db, "job-force", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", force_rescan=True
            )

        assert mock_job.status == "completed"
        assert mock_job.github_repo == "https://github.com/org/repo"


class TestCritiqueServiceRunnerWrappers:
    """Test the scanner wrapper methods."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_seo(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._seo = MagicMock()
        svc._seo.scan.return_value = [{"title": "SEO issue"}]
        result = await svc._run_seo("<html></html>", "https://example.com")
        assert len(result) == 1

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_seo_live(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._seo = AsyncMock()
        svc._seo.scan_live = AsyncMock(return_value=[])
        result = await svc._run_seo_live("https://example.com")
        assert result == []

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_a11y(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._a11y = MagicMock()
        svc._a11y.scan.return_value = []
        result = await svc._run_a11y("<html></html>", "https://example.com")
        assert result == []

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_performance(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._performance = MagicMock()
        svc._performance.scan.return_value = [{"title": "Slow"}]
        result = await svc._run_performance("<html></html>", "https://example.com")
        assert len(result) == 1

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_security(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._security = AsyncMock()
        svc._security.scan = AsyncMock(return_value=[])
        result = await svc._run_security("https://example.com", "<html></html>")
        assert result == []

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_ux(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._ux = AsyncMock()
        svc._ux.scan = AsyncMock(return_value=[])
        result = await svc._run_ux("<html></html>", "https://example.com", None)
        assert result == []

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_run_code_quality(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        svc._code_quality = AsyncMock()
        svc._code_quality.scan = AsyncMock(return_value=[])
        result = await svc._run_code_quality("https://github.com/org/repo")
        assert result == []


class TestAllCategories:
    def test_all_categories_set(self):
        assert "seo" in ALL_CATEGORIES
        assert "security" in ALL_CATEGORIES
        assert "accessibility" in ALL_CATEGORIES
        assert "performance" in ALL_CATEGORIES
        assert "ux" in ALL_CATEGORIES
        assert "code_quality" in ALL_CATEGORIES
        assert len(ALL_CATEGORIES) == 6
