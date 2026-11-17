"""Tests for API routes."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from src.domains.critique.routes import (
    _get_critique_service,
    _get_tenant_id,
    _process_job_background,
)
from src.domains.critique.routes._helpers import require_operator
from src.domains.critique.schemas import CritiqueReportResponse


def _fake_create_task(coro, **kwargs):
    """Close the coroutine so it doesn't trigger 'coroutine was never awaited' warnings."""
    coro.close()
    return MagicMock()


class TestHelperFunctions:
    """Test route helper functions."""

    def test_get_tenant_id_from_state(self):
        """A real tenant passes through unchanged, and as a ``str``.

        ``str`` matters: ``set_config('app.current_tenant', $2, true)`` is
        ``(text, text, bool)``, so a ``uuid.UUID`` here raises
        ``UndefinedFunction`` and kills every transaction under RLS.
        """
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        got = _get_tenant_id(request)
        assert got == "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        assert isinstance(got, str)

    def test_get_tenant_id_absent_is_401_not_a_fabricated_sentinel(self):
        """This used to return the literal ``"anonymous"``.

        That sentinel is not a tenant — it is one of exactly two dirty values
        migration 0013 had to remediate out of the live tables. And since 0013
        made ``tenant_id`` a native ``uuid`` column, feeding a slug onward no
        longer degrades to "0 rows / 404": Postgres raises
        ``InvalidTextRepresentation`` and the caller gets a 500.
        """
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401

    def test_get_tenant_id_rejects_a_legacy_slug(self):
        """``n8n-dev`` is a REGISTERED TENANT NAME, not an id. It reached this
        service as recently as May; after 0013 it must be a 4xx, never a 500."""
        request = MagicMock()
        request.state.tenant_id = "n8n-dev"
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401

    def test_get_critique_service(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = MagicMock()
        request.app.state.critique_service = mock_service
        assert _get_critique_service(request) == mock_service


class TestRequireOperator:
    """The operator-only gate on global runtime-config routes (scoring weights /
    config overrides). Global tuning must not be reachable by an ordinary tenant."""

    @staticmethod
    def _req(roles):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        request.state.roles = roles
        return request

    def test_no_roles_is_denied(self):
        # License-token / in-cluster identities carry no roles -> 403.
        with pytest.raises(HTTPException) as exc:
            require_operator(self._req([]))
        assert exc.value.status_code == 403

    def test_non_operator_role_is_denied(self):
        with pytest.raises(HTTPException) as exc:
            require_operator(self._req(["viewer"]))
        assert exc.value.status_code == 403

    def test_missing_roles_attr_is_denied(self):
        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        request.state = MagicMock(spec=[])  # no `roles` attribute at all
        with pytest.raises(HTTPException) as exc:
            require_operator(request)
        assert exc.value.status_code == 403

    def test_operator_role_is_allowed(self):
        # A JWT identity carrying a configured operator role passes (no raise).
        with patch("src.domains.critique.routes._helpers.get_settings") as mock_settings:
            mock_settings.return_value.authz.operator_roles = ["operator", "analysis-admin"]
            require_operator(self._req(["analysis-admin"]))


class TestProcessJobBackground:
    """Test the background job processing wrapper."""

    async def test_process_job_background_success(self):
        mock_service = AsyncMock()
        mock_service.process_job = AsyncMock()

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.shared.database.session.get_db_session",
            return_value=mock_db,
        ):
            await _process_job_background(
                mock_service, "job-123", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

        mock_service.process_job.assert_awaited_once()

    async def test_process_job_background_exception(self):
        mock_service = AsyncMock()
        mock_service.process_job = AsyncMock(side_effect=RuntimeError("fail"))

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.shared.database.session.get_db_session",
            return_value=mock_db,
        ):
            # Should not raise -- logs the exception
            await _process_job_background(
                mock_service, "job-123", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

        # Failure reason is persisted with the exception type prefix.
        mock_service.mark_failed.assert_awaited_once()
        assert mock_service.mark_failed.await_args.args[2] == "RuntimeError: fail"

    async def test_process_job_background_blank_error_uses_type_name(self):
        """httpx.ConnectError stringifies to '' — must still store a usable reason.

        Regression: a blank reason surfaced upstream as 'critique job <id> failed:'
        with nothing after the colon, making fetch failures undebuggable.
        """
        import httpx

        mock_service = AsyncMock()
        mock_service.process_job = AsyncMock(side_effect=httpx.ConnectError(""))

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.shared.database.session.get_db_session",
            return_value=mock_db,
        ):
            await _process_job_background(
                mock_service, "job-123", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

        mock_service.mark_failed.assert_awaited_once()
        detail = mock_service.mark_failed.await_args.args[2]
        assert detail == "ConnectError"
        assert detail  # never blank


class TestCreateCritiqueRoute:
    """Test create_critique endpoint logic."""

    async def test_create_critique(self):
        from src.domains.critique.routes import create_critique
        from src.domains.critique.schemas import CritiqueRequest

        body = CritiqueRequest(url="https://example.com", categories=["seo"])
        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        mock_service.create_job = AsyncMock(return_value="job-abc-123")
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with patch("src.domains.critique.routes.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock(side_effect=_fake_create_task)
            result = await create_critique(body, request, db)

        assert result.job_id == "job-abc-123"
        assert result.status == "pending"
        assert "job-abc-123" in result.poll_url


class TestCallbackUrlSSRF:
    """Callback URLs must be SSRF-validated at job creation (fail fast -> 400)."""

    _METADATA_URL = "http://169.254.169.254/latest/meta-data/"

    async def test_create_critique_rejects_metadata_callback(self):
        from src.domains.critique.routes import create_critique
        from src.domains.critique.schemas import CritiqueRequest

        body = CritiqueRequest(url="https://example.com", callback_url=self._METADATA_URL)
        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        mock_service.create_job = AsyncMock(return_value="should-not-be-reached")
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await create_critique(body, request, db)
        assert exc.value.status_code == 400
        # The job must never be persisted when the callback URL is unsafe.
        mock_service.create_job.assert_not_called()

    async def test_rerun_critique_rejects_metadata_callback(self):
        from src.domains.critique.routes import rerun_critique
        from src.domains.critique.schemas import RerunRequest

        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        original = MagicMock()
        original.url = "https://example.com"
        original.categories = ["seo"]
        original.max_pages = 1
        original.instructions = None
        original.github_repo = None
        original.callback_url = self._METADATA_URL
        mock_service.get_job = AsyncMock(return_value=original)
        mock_service.create_job = AsyncMock(return_value="should-not-be-reached")
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await rerun_critique("old-job", RerunRequest(), request, db)
        assert exc.value.status_code == 400
        mock_service.create_job.assert_not_called()

    async def test_create_batch_rejects_metadata_callback(self):
        from src.domains.critique.routes import create_batch_critique
        from src.domains.critique.schemas import BatchCritiqueRequest

        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        mock_service.create_batch = AsyncMock(return_value=("batch-x", ["j1"]))
        request.app.state.critique_service = mock_service
        db = AsyncMock()
        body = BatchCritiqueRequest(urls=["https://a.com"], callback_url=self._METADATA_URL)

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.batch_scan = True
            await create_batch_critique(body, request, db)
        assert exc.value.status_code == 400
        mock_service.create_batch.assert_not_called()


class TestGetCritiqueRoute:
    """Test get_critique endpoint logic."""

    async def test_get_critique_found(self):
        from src.domains.critique.routes import get_critique

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-123"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "low",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_job.category_scores = {"seo": 85.0}
        mock_job.overall_score = 85.0
        mock_job.confidence_scores = {"seo": 0.9}
        mock_job.summary = "Good"
        mock_job.scan_duration = 5.0
        mock_job.pages_scanned = 1
        mock_job.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_job.completed_at = datetime(2024, 1, 1, 0, 0, 5, tzinfo=UTC)
        mock_job.error_message = None
        mock_job.progress = None

        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique("job-123", request, db)
        assert result.job_id == "job-123"
        assert result.status == "completed"
        assert result.findings is not None
        assert len(result.findings) == 1

    async def test_get_critique_not_found(self):
        from src.domains.critique.routes import get_critique

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique("nonexistent", request, db)
        assert exc.value.status_code == 404


class TestDeleteCritiqueRoute:
    """Test delete_critique endpoint logic."""

    async def test_delete_critique_success(self):
        from src.domains.critique.routes import delete_critique

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.delete_job = AsyncMock(return_value=True)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await delete_critique("job-123", request, db)
        assert result is None  # 204 returns None

    async def test_delete_critique_not_found(self):
        from src.domains.critique.routes import delete_critique

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.delete_job = AsyncMock(return_value=False)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await delete_critique("nonexistent", request, db)
        assert exc.value.status_code == 404


class TestListCritiquesRoute:
    """Test list_critiques endpoint logic."""

    async def test_list_critiques(self):
        from src.domains.critique.routes import list_critiques

        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

        mock_job = MagicMock()
        mock_job.job_id = "job-1"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.findings = None
        mock_job.category_scores = {"seo": 80.0}
        mock_job.overall_score = 80.0
        mock_job.confidence_scores = None
        mock_job.summary = "Summary"
        mock_job.scan_duration = 5.0
        mock_job.pages_scanned = 1
        mock_job.created_at = datetime(2024, 1, 1, tzinfo=UTC)
        mock_job.completed_at = datetime(2024, 1, 1, 0, 0, 5, tzinfo=UTC)
        mock_job.error_message = None

        mock_service = AsyncMock()
        mock_service.list_jobs = AsyncMock(return_value=([mock_job], 1))
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await list_critiques(request, 1, 20, None, db)
        assert result.total == 1
        assert len(result.items) == 1
        assert result.page == 1
        assert result.per_page == 20


class TestCompareCritiquesRoute:
    """Test compare_critiques endpoint logic."""

    async def test_compare_critiques_job_not_found(self):
        from src.domains.critique.routes import compare_critiques

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await compare_critiques(request, "j1", "j2", db)
        assert exc.value.status_code == 404

    async def test_compare_critiques_not_completed(self):
        from src.domains.critique.routes import compare_critiques

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        job1 = MagicMock()
        job1.status = "completed"
        job2 = MagicMock()
        job2.status = "running"
        mock_service.get_job = AsyncMock(side_effect=[job1, job2])
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await compare_critiques(request, "j1", "j2", db)
        assert exc.value.status_code == 409


class TestGetCritiqueReport:
    """Test report endpoint logic."""

    async def test_get_report_not_found(self):
        from src.domains.critique.routes import get_critique_report

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_report("job-x", request, "markdown", db)
        assert exc.value.status_code == 404

    async def test_get_report_not_completed(self):
        from src.domains.critique.routes import get_critique_report

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "running"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_report("job-x", request, "markdown", db)
        assert exc.value.status_code == 409

    async def test_get_report_markdown(self):
        from src.domains.critique.routes import get_critique_report

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-x"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.report_markdown = "# Report"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique_report("job-x", request, "markdown", db)
        assert isinstance(result, CritiqueReportResponse)
        assert result.report == "# Report"

    async def test_get_report_markdown_not_available(self):
        from src.domains.critique.routes import get_critique_report

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-x"
        mock_job.status = "completed"
        mock_job.report_markdown = None
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_report("job-x", request, "markdown", db)
        assert exc.value.status_code == 404

    async def test_get_report_html(self):
        from src.domains.critique.routes import get_critique_report

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-x"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "low",
                "title": "Test",
                "description": "Desc",
            }
        ]
        mock_job.category_scores = {"seo": 85.0}
        mock_job.overall_score = 85.0
        mock_job.summary = "Summary"
        mock_job.scan_duration = 5.0
        mock_job.github_repo = None
        mock_job.confidence_scores = None
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique_report("job-x", request, "html", db)
        assert isinstance(result, HTMLResponse)
        assert result.status_code == 200


class TestGetCritiqueFindingsRoute:
    """Test get_critique_findings endpoint."""

    async def test_get_findings_not_found(self):
        from src.domains.critique.routes import get_critique_findings

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_findings("job-x", request, None, None, 1, 20, db)
        assert exc.value.status_code == 404

    async def test_get_findings_not_completed(self):
        from src.domains.critique.routes import get_critique_findings

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "running"
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_critique_findings("job-x", request, None, None, 1, 20, db)
        assert exc.value.status_code == 409

    async def test_get_findings_success(self):
        from src.domains.critique.routes import get_critique_findings

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "high",
                "title": "Test",
                "description": "Desc",
            },
            {
                "finding_id": "f2",
                "category": "security",
                "severity": "low",
                "title": "Sec",
                "description": "Sec desc",
            },
        ]
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique_findings("job-x", request, None, None, 1, 20, db)
        assert result.total == 2
        assert len(result.findings) == 2

    async def test_get_findings_with_category_filter(self):
        from src.domains.critique.routes import get_critique_findings

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.status = "completed"
        mock_job.findings = [
            {
                "finding_id": "f1",
                "category": "seo",
                "severity": "high",
                "title": "Test",
                "description": "Desc",
            },
            {
                "finding_id": "f2",
                "category": "security",
                "severity": "low",
                "title": "Sec",
                "description": "Sec desc",
            },
        ]
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        result = await get_critique_findings("job-x", request, "seo", None, 1, 20, db)
        assert result.total == 1
        assert result.findings[0].category == "seo"


class TestUpdateJobTags:
    """Test tag update endpoint."""

    async def test_update_tags_not_found(self):
        from src.domains.critique.routes import update_job_tags
        from src.domains.critique.schemas import TagsUpdateRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await update_job_tags("job-x", TagsUpdateRequest(tags=["v2"]), request, db)
        assert exc.value.status_code == 404

    async def test_update_tags_success(self):
        from src.domains.critique.routes import update_job_tags
        from src.domains.critique.schemas import TagsUpdateRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_job = MagicMock()
        mock_job.job_id = "job-x"
        mock_job.status = "completed"
        mock_job.url = "https://example.com"
        mock_job.findings = None
        mock_job.category_scores = None
        mock_job.overall_score = None
        mock_service.get_job = AsyncMock(return_value=mock_job)
        request.app.state.critique_service = mock_service
        db = AsyncMock()
        db.commit = AsyncMock()

        result = await update_job_tags("job-x", TagsUpdateRequest(tags=["v2", "prod"]), request, db)
        assert mock_job.tags == ["v2", "prod"]
        assert result.job_id == "job-x"


class TestRerunCritique:
    """Test rerun endpoint."""

    async def test_rerun_not_found(self):
        from src.domains.critique.routes import rerun_critique
        from src.domains.critique.schemas import RerunRequest

        request = MagicMock()
        mock_service = AsyncMock()
        mock_service.get_job = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await rerun_critique("job-x", RerunRequest(), request, db)
        assert exc.value.status_code == 404

    async def test_rerun_success(self):
        from src.domains.critique.routes import rerun_critique
        from src.domains.critique.schemas import RerunRequest

        request = MagicMock()
        request.state.tenant_id = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        mock_service = AsyncMock()
        original = MagicMock()
        original.url = "https://example.com"
        original.categories = ["seo"]
        original.max_pages = 1
        original.instructions = None
        original.github_repo = None
        original.callback_url = None
        mock_service.get_job = AsyncMock(return_value=original)
        mock_service.create_job = AsyncMock(return_value="new-job-id")
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with patch("src.domains.critique.routes.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock(side_effect=_fake_create_task)
            result = await rerun_critique(
                "old-job", RerunRequest(categories=["security"]), request, db
            )

        assert result.job_id == "new-job-id"
        assert result.status == "pending"


class TestExportRoutes:
    """Test export endpoint error paths."""

    async def test_export_csv_not_found(self):
        from src.domains.critique.routes import export_csv

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
            mock_settings.return_value.features.export_csv = True
            await export_csv("job-x", request, db)
        assert exc.value.status_code == 404

    async def test_export_csv_not_completed(self):
        from src.domains.critique.routes import export_csv

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
            mock_settings.return_value.features.export_csv = True
            await export_csv("job-x", request, db)
        assert exc.value.status_code == 409

    async def test_export_csv_feature_disabled(self):
        from src.domains.critique.routes import export_csv

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.export_csv = False
            await export_csv("job-x", request, db)
        assert exc.value.status_code == 501

    async def test_export_sarif_feature_disabled(self):
        from src.domains.critique.routes import export_sarif

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.export_sarif = False
            await export_sarif("job-x", request, db)
        assert exc.value.status_code == 501


class TestScoringWeightsRoute:
    """Test scoring weights endpoints."""

    async def test_get_scoring_weights(self):
        from src.domains.critique.routes import get_scoring_weights_endpoint

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        with patch(
            "src.domains.critique.services.runtime_config.get_scoring_weights",
            new_callable=AsyncMock,
            return_value={"critical_weight": 30, "a11y_weight": 0.25},
        ):
            result = await get_scoring_weights_endpoint(request)
        assert "critical_weight" in result.weights

    async def test_update_scoring_weights(self):
        from src.domains.critique.routes import update_scoring_weights
        from src.domains.critique.schemas import ScoringWeightsUpdateRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        body = ScoringWeightsUpdateRequest(weights={"critical_weight": 25})

        with (
            patch(
                "src.domains.critique.services.runtime_config.set_runtime_value",
                new_callable=AsyncMock,
            ),
            patch(
                "src.domains.critique.services.runtime_config.get_scoring_weights",
                new_callable=AsyncMock,
                return_value={"critical_weight": 25},
            ),
        ):
            result = await update_scoring_weights(body, request)
        assert result.weights["critical_weight"] == 25


class TestConfigOverridesRoute:
    """Test config override endpoints."""

    async def test_get_config_overrides(self):
        from src.domains.critique.routes import get_config_overrides

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        with patch(
            "src.domains.critique.services.runtime_config.get_all_overrides",
            new_callable=AsyncMock,
            return_value={"scoring": {"critical_weight": "25"}},
        ):
            result = await get_config_overrides(request)
        assert "scoring" in result.overrides

    async def test_delete_config_override_success(self):
        from src.domains.critique.routes import delete_config_override

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        with patch(
            "src.domains.critique.services.runtime_config.delete_runtime_value",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await delete_config_override("scoring", "critical_weight", request)
        assert result is None

    async def test_delete_config_override_not_found(self):
        from src.domains.critique.routes import delete_config_override

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        with (
            patch(
                "src.domains.critique.services.runtime_config.delete_runtime_value",
                new_callable=AsyncMock,
                return_value=False,
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await delete_config_override("scoring", "nonexistent", request)
        assert exc.value.status_code == 404


class TestAnalyticsRoutes:
    """Test analytics endpoints."""

    async def test_analytics_trends_disabled(self):
        from src.domains.critique.routes import get_analytics_trends

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.analytics = False
            await get_analytics_trends(request, "example.com", 30, db)
        assert exc.value.status_code == 501

    async def test_domain_analytics_disabled(self):
        from src.domains.critique.routes import get_domain_analytics

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.analytics = False
            await get_domain_analytics("example.com", request, db)
        assert exc.value.status_code == 501

    async def test_tenant_summary_disabled(self):
        from src.domains.critique.routes import get_tenant_summary

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.analytics = False
            await get_tenant_summary(request, db)
        assert exc.value.status_code == 501

    async def test_top_findings_disabled(self):
        from src.domains.critique.routes import get_top_findings

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.analytics = False
            await get_top_findings(request, None, None, None, 20, 30, db)
        assert exc.value.status_code == 501


class TestScheduleRoutes:
    """Test schedule endpoint error paths."""

    async def test_create_schedule_disabled(self):
        from src.domains.critique.routes import create_schedule
        from src.domains.critique.schemas import CreateScheduleRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()
        body = CreateScheduleRequest(url="https://example.com")

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.scheduled_scans = False
            await create_schedule(body, request, db)
        assert exc.value.status_code == 501

    async def test_list_schedules_disabled(self):
        from src.domains.critique.routes import list_schedules

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.scheduled_scans = False
            await list_schedules(request, db)
        assert exc.value.status_code == 501


class TestBatchRoutes:
    """Test batch endpoint error paths."""

    async def test_create_batch_disabled(self):
        from src.domains.critique.routes import create_batch_critique
        from src.domains.critique.schemas import BatchCritiqueRequest

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()
        body = BatchCritiqueRequest(urls=["https://a.com"])

        with (
            patch("src.config.get_settings") as mock_settings,
            pytest.raises(HTTPException) as exc,
        ):
            mock_settings.return_value.features.batch_scan = False
            await create_batch_critique(body, request, db)
        assert exc.value.status_code == 501

    async def test_get_batch_not_found(self):
        from src.domains.critique.routes import get_batch_status

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        mock_service = AsyncMock()
        mock_service.update_batch_status = AsyncMock()
        mock_service.get_batch = AsyncMock(return_value=None)
        request.app.state.critique_service = mock_service
        db = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await get_batch_status("batch-x", request, db)
        assert exc.value.status_code == 404


class TestSuppressionRoutes:
    """Test suppression endpoint error paths."""

    async def test_delete_suppression_not_found(self):
        from src.domains.critique.routes import delete_suppression

        request = MagicMock()
        request.state.tenant_id = "aeb670dd-a0f9-5110-893b-2820fbdf794e"
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc:
            await delete_suppression("sup-x", request, db)
        assert exc.value.status_code == 404


class TestAcknowledgeFinding:
    """Test acknowledge finding endpoint."""

    async def test_acknowledge_finding_not_found(self):
        from src.domains.critique.routes import acknowledge_finding
        from src.domains.critique.schemas import AcknowledgeFindingRequest

        request = MagicMock()
        request.state.tenant_id = "4be0643f-1d98-573b-97cd-ca98a65347dd"
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc:
            await acknowledge_finding(
                "job-x", "finding-x", AcknowledgeFindingRequest(reason="test"), request, db
            )
        assert exc.value.status_code == 404
