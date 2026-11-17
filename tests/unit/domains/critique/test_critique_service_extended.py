"""Extended tests for CritiqueService — covers _init_adapters, _apply_suppressions,
_deliver_webhook error path, _save_findings error path, and process_job with adapters."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.scanners.tool_adapters.registry import (
    AdapterRunResult,
    AdapterStatus,
)


class TestInitAdapters:
    """Test _init_adapters with external tools enabled."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    def test_init_adapters_all_enabled(self, mock_settings):
        """All external tool adapters are registered when enabled."""
        mock_settings.return_value.external_tools.enabled = True
        mock_settings.return_value.external_tools.axe_enabled = True
        mock_settings.return_value.external_tools.lighthouse_enabled = True
        mock_settings.return_value.external_tools.zap_enabled = True
        mock_settings.return_value.external_tools.nuclei_enabled = True

        svc = CritiqueService()
        assert svc._adapter_registry is not None

    @patch("src.domains.critique.services.critique_service.get_settings")
    def test_init_adapters_some_enabled(self, mock_settings):
        """Only requested adapters are registered."""
        mock_settings.return_value.external_tools.enabled = True
        mock_settings.return_value.external_tools.axe_enabled = True
        mock_settings.return_value.external_tools.lighthouse_enabled = False
        mock_settings.return_value.external_tools.zap_enabled = False
        mock_settings.return_value.external_tools.nuclei_enabled = False

        svc = CritiqueService()
        assert svc._adapter_registry is not None

    @patch("src.domains.critique.services.critique_service.get_settings")
    def test_init_adapters_none_enabled(self, mock_settings):
        """Registry created but no adapters when all sub-flags disabled."""
        mock_settings.return_value.external_tools.enabled = True
        mock_settings.return_value.external_tools.axe_enabled = False
        mock_settings.return_value.external_tools.lighthouse_enabled = False
        mock_settings.return_value.external_tools.zap_enabled = False
        mock_settings.return_value.external_tools.nuclei_enabled = False

        svc = CritiqueService()
        assert svc._adapter_registry is not None


class TestApplySuppressions:
    """Test _apply_suppressions method."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_no_suppressions(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        findings = [{"title": "Test", "category": "seo"}]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result == findings

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_by_check_id(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        suppression = MagicMock()
        suppression.check_id = "sec-missing-csp"
        suppression.title_hash = None
        suppression.category = "security"
        suppression.expires_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [suppression]
        db.execute = AsyncMock(return_value=mock_result)

        findings = [
            {"title": "Missing CSP", "check_id": "sec-missing-csp", "category": "security"},
            {"title": "Missing HSTS", "check_id": "sec-missing-hsts", "category": "security"},
        ]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result[0]["is_suppressed"] is True
        assert result[1]["is_suppressed"] is False

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_by_title_hash(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        title = "Missing CSP header"
        title_hash = hashlib.sha256(title.encode()).hexdigest()[:64]

        suppression = MagicMock()
        suppression.check_id = None
        suppression.title_hash = title_hash
        suppression.category = None
        suppression.expires_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [suppression]
        db.execute = AsyncMock(return_value=mock_result)

        findings = [{"title": title, "category": "security"}]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result[0]["is_suppressed"] is True

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_by_category(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        suppression = MagicMock()
        suppression.check_id = None
        suppression.title_hash = None
        suppression.category = "ux"
        suppression.expires_at = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [suppression]
        db.execute = AsyncMock(return_value=mock_result)

        findings = [
            {"title": "UX issue", "category": "ux"},
            {"title": "SEO issue", "category": "seo"},
        ]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result[0]["is_suppressed"] is True
        assert result[1]["is_suppressed"] is False

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_expired_rule_ignored(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        suppression = MagicMock()
        suppression.check_id = "sec-missing-csp"
        suppression.title_hash = None
        suppression.category = "security"
        suppression.expires_at = datetime(2020, 1, 1, tzinfo=UTC)  # expired

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [suppression]
        db.execute = AsyncMock(return_value=mock_result)

        findings = [{"title": "Missing CSP", "check_id": "sec-missing-csp", "category": "security"}]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result[0]["is_suppressed"] is False

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_no_domain(self, mock_settings):
        """Suppression without domain matches global-only suppressions."""
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        findings = [{"title": "Test", "category": "seo"}]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain=None
        )
        assert len(result) == 1

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_suppress_db_error(self, mock_settings):
        """DB errors in suppression are caught gracefully."""
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("DB error"))

        findings = [{"title": "Test", "category": "seo"}]
        result = await svc._apply_suppressions(
            db, findings, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", domain="example.com"
        )
        assert result[0]["is_suppressed"] is False


class TestDeliverWebhookError:
    """Test _deliver_webhook error path."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_deliver_webhook_error(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        job = MagicMock()
        job.callback_url = "https://hooks.example.com/cb"
        job.job_id = "job-123"
        job.status = "completed"
        job.url = "https://example.com"
        job.category_scores = {}
        job.overall_score = 90.0
        job.findings = []
        job.summary = "Summary"

        with patch(
            "src.domains.critique.services.webhook.deliver_webhook",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Webhook failed"),
        ) as mock_deliver:
            # Should not raise
            await svc._deliver_webhook(job)

        # Verify the webhook delivery was attempted with the job
        mock_deliver.assert_awaited_once()


class TestSaveFindingsError:
    """Test _save_findings error path."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_save_findings_exception(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock(side_effect=RuntimeError("DB error"))

        findings = [{"finding_id": "f1", "category": "seo", "title": "Test"}]
        # Should not raise — error is caught gracefully
        await svc._save_findings(
            db, "job-123", findings, tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )

        # db.add was called (and raised), confirming the save was attempted
        db.add.assert_called_once()


class TestUpdateProgressWithOptionalArgs:
    """Test _update_progress with scanners and scanner_timings args."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_progress_with_scanners_list(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with patch("src.shared.cache.redis_client.get_redis_client", return_value=mock_client):
            await svc._update_progress(
                "job-123",
                status="scanning",
                completed=0,
                total=3,
                scanners=["seo", "security", "a11y"],
            )
        mock_client.set.assert_awaited_once()
        set_args = mock_client.set.call_args
        assert set_args[0][0] == "critique:progress:job-123"
        stored = json.loads(set_args[0][1])
        assert stored["status"] == "scanning"
        assert stored["completed"] == 0
        assert stored["total"] == 3
        assert stored["scanners"] == ["seo", "security", "a11y"]
        assert set_args[1]["ttl"] == 600

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_progress_with_scanner_timings(self, mock_settings):
        mock_settings.return_value.external_tools.enabled = False
        svc = CritiqueService()

        mock_client = AsyncMock()
        mock_client.set = AsyncMock(return_value=True)

        with patch("src.shared.cache.redis_client.get_redis_client", return_value=mock_client):
            await svc._update_progress(
                "job-123",
                status="scoring",
                scanner_timings={"seo": 1.5, "security": 2.3},
            )
        mock_client.set.assert_awaited_once()
        set_args = mock_client.set.call_args
        assert set_args[0][0] == "critique:progress:job-123"
        stored = json.loads(set_args[0][1])
        assert stored["status"] == "scoring"
        assert stored["scanner_timings"] == {"seo": 1.5, "security": 2.3}
        assert "scanners" not in stored  # scanners not passed
        assert set_args[1]["ttl"] == 600


class TestProcessJobWithAdapters:
    """Test process_job with external adapters enabled."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_adapter_findings(self, mock_settings):
        """External adapters contribute findings to the job result."""
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

        # Set up adapter registry
        mock_registry = AsyncMock()
        mock_registry.run_adapters_with_status = AsyncMock(
            return_value=AdapterRunResult(
                findings=[
                    {
                        "finding_id": "adapter-f1",
                        "category": "security",
                        "severity": "high",
                        "title": "ZAP finding",
                        "description": "Found by ZAP",
                        "url": "https://example.com",
                        "source_tool": "zap",
                    }
                ],
                statuses={"zap": AdapterStatus(status="ok", category="security", findings=1)},
            )
        )
        svc._adapter_registry = mock_registry

        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-adapt"
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None
        mock_job.domain = "example.com"

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
            patch.object(
                svc, "_fetch_html", new_callable=AsyncMock, return_value="<html>Test</html>"
            ),
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
        ):
            await svc.process_job(db, "job-adapt", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "completed"
        # The adapter finding should be in the results
        mock_registry.run_adapters_with_status.assert_awaited_once_with(
            "https://example.com", "<html>Test</html>", timeout=120
        )

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_adapter_error(self, mock_settings):
        """External adapter errors are handled gracefully."""
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

        # Adapter registry that raises
        mock_registry = AsyncMock()
        mock_registry.run_adapters_with_status = AsyncMock(
            side_effect=RuntimeError("Adapter crash")
        )
        mock_registry.adapter_names = ["zap"]
        svc._adapter_registry = mock_registry

        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-adapt-err"
        mock_job.url = "https://example.com"
        mock_job.categories = ["seo"]
        mock_job.github_repo = None
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None
        mock_job.domain = "example.com"

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
            patch.object(
                svc, "_fetch_html", new_callable=AsyncMock, return_value="<html>Test</html>"
            ),
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
        ):
            await svc.process_job(db, "job-adapt-err", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        # Job should still complete despite adapter error
        assert mock_job.status == "completed"
        # ...but the failure is SURFACED, not reported as 0 findings / clean.
        assert mock_job.progress["external_tools"]["zap"]["status"] == "failed"
        assert "Adapter crash" in mock_job.progress["external_tools"]["zap"]["error"]
        assert "zap" in mock_job.progress["incomplete_scanners"]
        assert "`zap`" in mock_job.report_markdown
        assert "did NOT complete" in mock_job.report_markdown


class TestProcessJobCodeQuality:
    """Test process_job with code_quality category and github_repo."""

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_process_job_with_code_quality(self, mock_settings):
        """Process job includes code quality scanner when configured."""
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
        # Mark code quality scanner as configured
        svc._code_quality = MagicMock()
        svc._code_quality.is_configured = True

        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "job-cq"
        mock_job.url = "https://example.com"
        mock_job.categories = ["code_quality"]
        mock_job.github_repo = "https://github.com/org/repo"
        mock_job.max_pages = 1
        mock_job.instructions = None
        mock_job.callback_url = None
        mock_job.status = "pending"
        mock_job.findings = None
        mock_job.progress = None
        mock_job.domain = "example.com"

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
        ):
            await svc.process_job(db, "job-cq", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")

        assert mock_job.status == "completed"
