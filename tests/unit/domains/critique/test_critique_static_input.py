"""Tests for critique scoring of a pre-rendered artifact (html / artifact_ref).

Covers the "Option B" path: score stored HTML with no live URL — the request
contract (exactly one of url/html/artifact_ref), the resolver/injection seam,
the live-only category skips + progress surfacing, the no-network security
subset, and the asymmetric compare deltas.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.domains.critique.schemas import CritiqueRequest
from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.report_generator import compute_score_deltas
from src.domains.critique.services.scanners.security_scanner import SecurityScanner


# --------------------------------------------------------------------------- #
# 1. Request contract — exactly one of {url, html, artifact_ref}
# --------------------------------------------------------------------------- #
class TestCritiqueRequestSource:
    def test_url_only_valid(self):
        req = CritiqueRequest(url="https://example.com")
        assert req.url == "https://example.com"
        assert req.html is None and req.artifact_ref is None

    def test_html_only_valid(self):
        req = CritiqueRequest(html="<html><body>hi</body></html>")
        assert req.html == "<html><body>hi</body></html>"
        assert req.url is None

    def test_artifact_ref_only_valid(self):
        # A relative object key. URI forms are accepted by the schema (which only
        # enforces the one-source rule) and rejected later, at resolution time, by
        # ``CritiqueHelpersMixin._artifact_object_key`` — see
        # ``test_artifact_ref_confinement.py``.
        req = CritiqueRequest(artifact_ref="jobs/abc/index.html")
        assert req.artifact_ref == "jobs/abc/index.html"
        assert req.url is None

    def test_none_of_three_rejected(self):
        with pytest.raises(ValidationError, match="exactly one of"):
            CritiqueRequest()

    def test_two_of_three_rejected(self):
        with pytest.raises(ValidationError, match="exactly one of"):
            CritiqueRequest(url="https://example.com", html="<html></html>")

    def test_all_three_rejected(self):
        with pytest.raises(ValidationError, match="exactly one of"):
            CritiqueRequest(url="https://x.com", html="<html></html>", artifact_ref="k")

    def test_blank_strings_do_not_count(self):
        # whitespace-only url should not satisfy the "one source" rule
        with pytest.raises(ValidationError, match="exactly one of"):
            CritiqueRequest(url="   ")


# --------------------------------------------------------------------------- #
# 2. _build_scan_tasks — live-only tasks dropped / security routed to static
# --------------------------------------------------------------------------- #
def _scanner_settings() -> MagicMock:
    s = MagicMock()
    s.scanners.seo_timeout = 30
    s.scanners.a11y_timeout = 30
    s.scanners.performance_timeout = 30
    s.scanners.security_timeout = 30
    s.scanners.ux_timeout = 30
    s.scanners.code_quality_timeout = 30
    return s


class TestBuildScanTasksStatic:
    def _svc(self) -> CritiqueService:
        with patch.object(CritiqueService, "_init_adapters", return_value=None):
            return CritiqueService()

    def _job(self):
        job = MagicMock()
        job.url = "rendered://job-1"
        job.instructions = None
        return job

    def test_static_drops_seo_live(self):
        svc = self._svc()
        tasks = svc._build_scan_tasks(
            job=self._job(),
            html="<html></html>",
            soup=None,
            categories={"seo", "security"},
            github_repo=None,
            settings=_scanner_settings(),
            site_analysis=None,
            llm_config=None,
            is_static=True,
        )
        names = {name for name, _, _ in tasks}
        assert "seo" in names
        assert "sec" in names
        assert "seo_live" not in names

    def test_live_keeps_seo_live(self):
        svc = self._svc()
        tasks = svc._build_scan_tasks(
            job=self._job(),
            html="<html></html>",
            soup=None,
            categories={"seo"},
            github_repo=None,
            settings=_scanner_settings(),
            site_analysis=None,
            llm_config=None,
            is_static=False,
        )
        names = {name for name, _, _ in tasks}
        assert "seo_live" in names

    async def test_static_security_routes_to_scan_static(self):
        svc = self._svc()
        tasks = svc._build_scan_tasks(
            job=self._job(),
            html="<html></html>",
            soup=None,
            categories={"security"},
            github_repo=None,
            settings=_scanner_settings(),
            site_analysis=None,
            llm_config=None,
            is_static=True,
        )
        sec_factory = next(factory for name, factory, _ in tasks if name == "sec")
        with (
            patch.object(svc, "_run_security", new_callable=AsyncMock) as live,
            patch.object(svc, "_run_security_static", new_callable=AsyncMock) as static,
        ):
            await sec_factory()
        static.assert_awaited_once()
        live.assert_not_awaited()


# --------------------------------------------------------------------------- #
# 3. process_job static paths — no live fetch, synthetic url, progress surfacing
# --------------------------------------------------------------------------- #
def _mock_process_settings(mock_settings):
    mock_settings.return_value.external_tools.enabled = False
    sc = mock_settings.return_value.scanners
    for attr in (
        "seo_timeout",
        "a11y_timeout",
        "performance_timeout",
        "security_timeout",
        "ux_timeout",
        "code_quality_timeout",
    ):
        setattr(sc, attr, 30)
    sc.overall_timeout = 120
    sc.max_retries = 1
    scoring = mock_settings.return_value.scoring
    scoring.critical_weight = 30
    scoring.high_weight = 15
    scoring.medium_weight = 7
    scoring.low_weight = 3
    scoring.info_weight = 0
    scoring.decay_k = 25
    scoring.a11y_weight = 0.25
    scoring.security_weight = 0.25
    scoring.performance_weight = 0.20
    scoring.seo_weight = 0.15
    scoring.ux_weight = 0.15


def _static_job():
    job = MagicMock()
    job.job_id = "job-static"
    job.tenant_id = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
    job.url = "rendered://job-static"
    job.categories = ["seo", "security"]
    job.github_repo = None
    job.max_pages = 1
    job.instructions = None
    job.callback_url = None
    job.status = "pending"
    job.findings = None
    job.progress = None
    return job


def _canned_findings():
    return [
        {
            "finding_id": "f1",
            "category": "seo",
            "severity": "medium",
            "title": "Missing meta description",
            "description": "No meta desc",
            "url": "rendered://job-static",
        }
    ]


class TestProcessJobStatic:
    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_html_input_skips_fetch_and_surfaces_progress(self, mock_settings):
        _mock_process_settings(mock_settings)
        with patch.object(CritiqueService, "_init_adapters", return_value=None):
            svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        job = _static_job()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.get_cached_result",
                new_callable=AsyncMock,
                return_value=None,
            ) as get_cache,
            patch(
                "src.domains.critique.services.scan_cache.set_cached_result",
                new_callable=AsyncMock,
            ) as set_cache,
            patch.object(svc, "_fetch_html", new_callable=AsyncMock) as fetch_html,
            patch.object(
                svc,
                "_run_scanner_with_timeout",
                new_callable=AsyncMock,
                return_value=(_canned_findings(), 0.1),
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
            await svc.process_job(
                db,
                "job-static",
                "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
                html_input="<html><body>hi</body></html>",
            )

        # Live fetch is never used for static input.
        fetch_html.assert_not_awaited()
        # URL-keyed cache read + write are bypassed.
        get_cache.assert_not_awaited()
        set_cache.assert_not_awaited()
        # Job completes and surfaces the static input mode + skipped live checks.
        assert job.status == "completed"
        assert job.progress is not None
        assert job.progress["input_mode"] == "html"
        assert "seo_live" in job.progress["skipped_categories"]
        assert "security_live" in job.progress["skipped_categories"]

    @patch("src.domains.critique.services.critique_service.get_settings")
    async def test_artifact_ref_fetches_blob_not_url(self, mock_settings):
        _mock_process_settings(mock_settings)
        with patch.object(CritiqueService, "_init_adapters", return_value=None):
            svc = CritiqueService()
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        job = _static_job()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)

        with (
            patch(
                "src.domains.critique.services.critique_service.log_event", new_callable=AsyncMock
            ),
            patch(
                "src.domains.critique.services.scan_cache.set_cached_result",
                new_callable=AsyncMock,
            ),
            patch.object(svc, "_fetch_html", new_callable=AsyncMock) as fetch_html,
            patch.object(
                svc, "_fetch_artifact", new_callable=AsyncMock, return_value="<html>blob</html>"
            ) as fetch_artifact,
            patch.object(
                svc,
                "_run_scanner_with_timeout",
                new_callable=AsyncMock,
                return_value=(_canned_findings(), 0.1),
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
            await svc.process_job(
                db,
                "job-static",
                "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
                artifact_ref="jobs/abc/index.html",
            )

        fetch_html.assert_not_awaited()
        # The owning tenant is forwarded so the object key can be confined to it.
        fetch_artifact.assert_awaited_once_with(
            "jobs/abc/index.html", tenant_id="2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        )
        assert job.status == "completed"
        assert job.progress["input_mode"] == "artifact"


# --------------------------------------------------------------------------- #
# 4. SecurityScanner.scan_static — HTML-only subset, zero outbound requests
# --------------------------------------------------------------------------- #
class TestSecurityScanStatic:
    async def test_scan_static_makes_no_request(self):
        scanner = SecurityScanner()
        html = (
            "<html><head></head><body>"
            "<script src='http://insecure.example/a.js'></script>"
            "<form action='http://insecure.example/submit'></form>"
            "</body></html>"
        )
        with patch("httpx.AsyncClient") as client:
            findings = await scanner.scan_static(html, url="https://example.com")
        client.assert_not_called()
        assert isinstance(findings, list)


# --------------------------------------------------------------------------- #
# 5. compute_score_deltas — asymmetric category sets (live before vs static after)
# --------------------------------------------------------------------------- #
class TestAsymmetricScoreDeltas:
    def test_missing_after_category_yields_full_negative(self):
        before = {"seo": 80.0, "security": 90.0, "accessibility": 70.0}
        after = {"seo": 85.0, "accessibility": 75.0}  # static "after": no live security
        deltas = compute_score_deltas(before, after)
        assert deltas["seo"] == 5.0
        assert deltas["accessibility"] == 5.0
        # security present only on the (live) before side → its full negative
        assert deltas["security"] == -90.0
