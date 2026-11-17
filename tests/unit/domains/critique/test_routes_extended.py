"""Extended tests for API routes -- covers more endpoint paths."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.domains.critique.routes import (
    compare_critiques,
    compare_history,
    create_batch_critique,
    create_schedule,
    create_suppression,
    delete_schedule,
    delete_suppression,
    export_csv,
    export_sarif,
    get_batch_status,
    get_critique_report,
    get_critique_report_pdf,
    get_schedule,
    get_unified_findings,
    list_schedules,
    list_suppressions,
    stream_critique_progress,
    update_schedule,
)
from src.domains.critique.schemas import CritiqueJSONReportResponse


def _fake_create_task(coro, **kwargs):
    """Close the coroutine so it doesn't trigger 'coroutine was never awaited' warnings."""
    coro.close()
    return MagicMock()


class TestCompareCritiquesSuccess:
    """Test compare_critiques when both jobs are found and completed."""

    async def test_compare_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()

        job1 = MagicMock()
        job1.status = "completed"
        job1.url = "https://a.com"
        job1.findings = [
            {"check_id": "seo-1", "title": "SEO Finding", "category": "seo", "severity": "low"}
        ]
        job1.category_scores = {"seo": 80.0}
        job1.overall_score = 80.0

        job2 = MagicMock()
        job2.status = "completed"
        job2.url = "https://a.com"
        job2.findings = [
            {"check_id": "seo-1", "title": "SEO Finding", "category": "seo", "severity": "low"},
            {"check_id": "seo-2", "title": "New SEO", "category": "seo", "severity": "medium"},
        ]
        job2.category_scores = {"seo": 75.0}
        job2.overall_score = 75.0

        mock_service.get_job = AsyncMock(side_effect=[job1, job2])
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await compare_critiques(request, "j1", "j2", db)
        assert result.job_id_1 == "j1"
        assert result.job_id_2 == "j2"
        assert result.overall_delta == -5.0


class TestCompareHistory:
    """Test compare_history endpoint."""

    async def test_compare_history_insufficient_jobs(self):
        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_service = AsyncMock()

        # Only 1 job for domain
        job1 = MagicMock()
        job1.domain = "example.com"
        mock_service.list_jobs = AsyncMock(return_value=([job1], 1))
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await compare_history(request, "example.com", 10, db)
        assert exc.value.status_code == 409

    async def test_compare_history_success(self):
        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_service = AsyncMock()

        job1 = MagicMock()
        job1.domain = "example.com"
        job1.findings = [
            {"check_id": "seo-1", "title": "Finding 1", "category": "seo", "severity": "low"},
        ]

        job2 = MagicMock()
        job2.domain = "example.com"
        job2.findings = [
            {"check_id": "seo-1", "title": "Finding 1", "category": "seo", "severity": "low"},
            {"check_id": "seo-2", "title": "New Finding", "category": "seo", "severity": "medium"},
        ]

        mock_service.list_jobs = AsyncMock(return_value=([job2, job1], 2))
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await compare_history(request, "example.com", 10, db)
        assert result.domain == "example.com"
        assert result.jobs_compared == 2


class TestSuppressionRoutes:
    """Test suppression create and list endpoints."""

    async def test_create_suppression_success(self):
        from src.domains.critique.schemas import CreateSuppressionRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        body = CreateSuppressionRequest(domain="example.com", check_id="sec-csp", reason="Known FP")

        with patch("src.domains.critique.models.FindingSuppression") as mock_cls:
            mock_suppression = MagicMock()
            mock_suppression.created_at = datetime(2024, 1, 1, tzinfo=UTC)
            mock_cls.return_value = mock_suppression

            result = await create_suppression(body, request, db)

        assert result.tenant_id == "4be0643f-1d98-573b-97cd-ca98a65347dd"
        assert result.domain == "example.com"
        assert result.check_id == "sec-csp"
        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_create_suppression_with_expires_at(self):
        from src.domains.critique.schemas import CreateSuppressionRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        body = CreateSuppressionRequest(
            check_id="sec-csp",
            expires_at="2025-12-31T23:59:59",
        )

        with patch("src.domains.critique.models.FindingSuppression") as mock_cls:
            mock_suppression = MagicMock()
            mock_suppression.created_at = datetime(2024, 1, 1, tzinfo=UTC)
            mock_cls.return_value = mock_suppression

            result = await create_suppression(body, request, db)

        assert result is not None
        assert result.tenant_id == "4be0643f-1d98-573b-97cd-ca98a65347dd"
        assert result.check_id == "sec-csp"
        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_create_suppression_invalid_expires(self):
        from src.domains.critique.schemas import CreateSuppressionRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()

        body = CreateSuppressionRequest(
            check_id="sec-csp",
            expires_at="not-a-date",
        )

        with pytest.raises(HTTPException) as exc:
            await create_suppression(body, request, db)
        assert exc.value.status_code == 400

    async def test_list_suppressions(self):
        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()

        mock_suppression = MagicMock()
        mock_suppression.suppression_id = "s1"
        mock_suppression.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_suppression.domain = "example.com"
        mock_suppression.check_id = "sec-csp"
        mock_suppression.category = "security"
        mock_suppression.reason = "FP"
        mock_suppression.status = "suppressed"
        mock_suppression.expires_at = None
        mock_suppression.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_suppression]
        db.execute = AsyncMock(return_value=mock_result)

        result = await list_suppressions(request, db)
        assert result.total == 1
        assert result.items[0].id == "s1"

    async def test_delete_suppression_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_suppression = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_suppression
        db.execute = AsyncMock(return_value=mock_result)
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        result = await delete_suppression("sup-1", request, db)
        assert result is None
        db.delete.assert_awaited_once_with(mock_suppression)
        db.commit.assert_awaited_once()


class TestScheduleRoutes:
    """Test schedule CRUD endpoints."""

    async def test_create_schedule_success(self):
        from src.domains.critique.schemas import CreateScheduleRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        body = CreateScheduleRequest(url="https://example.com", cron_expression="@daily")

        with (
            patch("src.config.get_settings") as mock_settings,
            patch("src.domains.critique.models.CritiqueSchedule") as mock_cls,
            patch("src.domains.critique.services.scheduler.next_run_from_cron") as mock_next,
        ):
            mock_settings.return_value.features.scheduled_scans = True
            mock_next.return_value = datetime(2024, 1, 2, tzinfo=UTC)
            mock_schedule = MagicMock()
            mock_schedule.created_at = datetime(2024, 1, 1, tzinfo=UTC)
            mock_cls.return_value = mock_schedule

            result = await create_schedule(body, request, db)

        assert result.url == "https://example.com"
        assert result.enabled is True
        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_get_schedule_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_schedule = MagicMock()
        mock_schedule.schedule_id = "sch-1"
        mock_schedule.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_schedule.url = "https://example.com"
        mock_schedule.categories = ["seo"]
        mock_schedule.cron_expression = "@daily"
        mock_schedule.enabled = True
        mock_schedule.last_run_at = None
        mock_schedule.next_run_at = datetime(2024, 1, 2, tzinfo=UTC)
        mock_schedule.last_job_id = None
        mock_schedule.run_count = 0
        mock_schedule.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_schedule
        db.execute = AsyncMock(return_value=mock_result)

        result = await get_schedule("sch-1", request, db)
        assert result.schedule_id == "sch-1"

    async def test_get_schedule_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc:
            await get_schedule("sch-x", request, db)
        assert exc.value.status_code == 404

    async def test_update_schedule_success(self):
        from src.domains.critique.schemas import UpdateScheduleRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()
        db.commit = AsyncMock()

        mock_schedule = MagicMock()
        mock_schedule.schedule_id = "sch-1"
        mock_schedule.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_schedule.url = "https://example.com"
        mock_schedule.categories = ["seo"]
        mock_schedule.cron_expression = "@daily"
        mock_schedule.enabled = True
        mock_schedule.last_run_at = None
        mock_schedule.next_run_at = datetime(2024, 1, 2, tzinfo=UTC)
        mock_schedule.last_job_id = None
        mock_schedule.run_count = 0
        mock_schedule.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_schedule
        db.execute = AsyncMock(return_value=mock_result)

        body = UpdateScheduleRequest(enabled=False)
        await update_schedule("sch-1", body, request, db)
        assert mock_schedule.enabled is False
        db.commit.assert_awaited_once()

    async def test_update_schedule_not_found(self):
        from src.domains.critique.schemas import UpdateScheduleRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc:
            await update_schedule("sch-x", UpdateScheduleRequest(enabled=False), request, db)
        assert exc.value.status_code == 404

    async def test_delete_schedule_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()
        db.delete = AsyncMock()
        db.commit = AsyncMock()

        mock_schedule = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_schedule
        db.execute = AsyncMock(return_value=mock_result)

        result = await delete_schedule("sch-1", request, db)
        assert result is None
        db.delete.assert_awaited_once_with(mock_schedule)
        db.commit.assert_awaited_once()

    async def test_delete_schedule_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc:
            await delete_schedule("sch-x", request, db)
        assert exc.value.status_code == 404

    async def test_list_schedules_success(self):
        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()

        mock_schedule = MagicMock()
        mock_schedule.schedule_id = "sch-1"
        mock_schedule.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_schedule.url = "https://example.com"
        mock_schedule.categories = ["seo"]
        mock_schedule.cron_expression = "@daily"
        mock_schedule.enabled = True
        mock_schedule.last_run_at = None
        mock_schedule.next_run_at = datetime(2024, 1, 2, tzinfo=UTC)
        mock_schedule.last_job_id = None
        mock_schedule.run_count = 0
        mock_schedule.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_schedule]
        db.execute = AsyncMock(return_value=mock_result)

        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value.features.scheduled_scans = True
            result = await list_schedules(request, db)

        assert result.total == 1


class TestExportRoutesExtended:
    """Test export endpoints with success paths."""

    async def test_export_csv_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "low",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_job.url = "https://example.com"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value.features.export_csv = True
            result = await export_csv("job-1", request, db)

        assert result.media_type == "text/csv"

    async def test_export_sarif_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "low",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_job.url = "https://example.com"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value.features.export_sarif = True
            result = await export_sarif("job-1", request, db)

        assert result["version"] == "2.1.0"

    async def test_export_sarif_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.export_sarif = True
            await export_sarif("job-x", request, db)
        assert exc.value.status_code == 404

    async def test_export_sarif_not_completed(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "running"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.export_sarif = True
            await export_sarif("job-x", request, db)
        assert exc.value.status_code == 409


class TestUnifiedFindings:
    """Test get_unified_findings endpoint."""

    async def test_unified_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_unified_findings("job-x", request, db)
        assert exc.value.status_code == 404

    async def test_unified_not_completed(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "pending"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_unified_findings("job-x", request, db)
        assert exc.value.status_code == 409

    async def test_unified_success(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "low",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_unified_findings("job-1", request, db)
        assert len(result) == 1
        assert result[0]["source_service"] == "critique_service"


class TestGetCritiqueReportJson:
    """Test get_critique_report with json format."""

    async def test_json_report(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-1"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "high",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_job.category_scores = {"seo": 80.0}
        mock_job.overall_score = 80.0
        mock_job.summary = "Summary"
        mock_job.scan_duration = 5.0
        mock_job.pages_scanned = 1
        mock_job.schema_version = "2.0"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique_report("job-1", request, "json", db)
        assert isinstance(result, CritiqueJSONReportResponse)
        assert result.url == "https://example.com"
        assert result.overall_score == 80.0
        assert len(result.top_issues) == 1


class TestGetCritiqueReportPdf:
    """Test PDF report endpoint."""

    async def test_pdf_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_report_pdf("job-x", request, db)
        assert exc.value.status_code == 404

    async def test_pdf_not_completed(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "running"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_report_pdf("job-x", request, db)
        assert exc.value.status_code == 409


class TestBatchRoutesExtended:
    """Test batch create endpoint."""

    async def test_create_batch_success(self):
        from src.domains.critique.schemas import BatchCritiqueRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        mock_service = AsyncMock()
        mock_service.create_batch = AsyncMock(return_value=("batch-1", ["j1", "j2"]))
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        body = BatchCritiqueRequest(urls=["https://a.com", "https://b.com"])

        with (
            patch("src.config.get_settings") as mock_settings,
            patch("src.domains.critique.routes.asyncio") as mock_asyncio,
        ):
            mock_settings.return_value.features.batch_scan = True
            mock_asyncio.create_task = MagicMock(side_effect=_fake_create_task)
            result = await create_batch_critique(body, request, db)

        assert result.batch_id == "batch-1"
        assert len(result.job_ids) == 2

    async def test_get_batch_success(self):
        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        mock_service.update_batch_status = AsyncMock()

        mock_batch = MagicMock()
        mock_batch.batch_id = "batch-1"
        mock_batch.status = "running"
        mock_batch.total = 2
        mock_batch.completed = 1
        mock_batch.failed = 0
        mock_batch.job_ids = ["j1", "j2"]
        mock_service.get_batch = AsyncMock(return_value=mock_batch)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_batch_status("batch-1", request, db)
        assert result.batch_id == "batch-1"
        assert result.completed == 1
        # Both fetches are tenant-scoped: the batch body carries the job_ids list.
        mock_service.update_batch_status.assert_awaited_once_with(
            db, "batch-1", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )
        mock_service.get_batch.assert_awaited_once_with(
            db, "batch-1", tenant_id="e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )


class TestSSEProgress:
    """Test SSE progress endpoint."""

    async def test_sse_disabled(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.sse_progress = False
            await stream_critique_progress("job-x", request)
        assert exc.value.status_code == 501

    async def test_sse_job_not_found(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service

        # The route takes its own short-lived session for the preflight lookup
        # rather than a request-scoped ``Depends(get_db)``, which would stay
        # open for the whole stream.
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.config.get_settings") as mock_settings,
            patch(
                "src.domains.critique.routes.jobs.get_db_session",
                return_value=session_cm,
            ),
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.sse_progress = True
            await stream_critique_progress("job-x", request)
        assert exc.value.status_code == 404
        session_cm.__aexit__.assert_awaited_once()
