"""Unit tests for additional routes.py coverage (context endpoints, helpers, edge cases)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.domains.analyze.routes import (
    _check_cancelled,
    _context_to_dict,
    _process_review,
    _run_with_timeout,
    cancel_analysis,
    enrich_context,
    get_analysis,
    get_artifacts,
    get_review,
    infer_context,
    list_analysis_jobs,
    start_analysis,
    validate_context,
)
from src.domains.analyze.schemas import (
    AnalyzeRequest,
    CrawledPageInput,
    IntelligenceContext,
)

# ---------------------------------------------------------------------------
# _context_to_dict
# ---------------------------------------------------------------------------


class TestContextToDict:
    def test_none_returns_none(self):
        assert _context_to_dict(None) is None

    def test_dict_returns_dict(self):
        d = {"key": "value"}
        assert _context_to_dict(d) == d

    def test_intelligence_context_returns_dict(self):
        ctx = IntelligenceContext(description="tech company", site_type="saas")
        result = _context_to_dict(ctx)
        assert isinstance(result, dict)
        assert result["description"] == "tech company"
        assert result["site_type"] == "saas"

    def test_unknown_type_returns_none(self):
        assert _context_to_dict(42) is None
        assert _context_to_dict("string") is None


# ---------------------------------------------------------------------------
# _check_cancelled
# ---------------------------------------------------------------------------


class TestCheckCancelled:
    def test_raises_when_cancelled(self):
        app = MagicMock()
        app.state.cancellation_tokens = {"job-1": True}
        with pytest.raises(RuntimeError, match="was cancelled"):
            _check_cancelled(app, "job-1")

    def test_no_raise_when_not_cancelled(self):
        app = MagicMock()
        app.state.cancellation_tokens = {}
        _check_cancelled(app, "job-1")  # Should not raise

    def test_no_raise_when_token_is_false(self):
        app = MagicMock()
        app.state.cancellation_tokens = {"job-1": False}
        _check_cancelled(app, "job-1")  # Should not raise


# ---------------------------------------------------------------------------
# _run_with_timeout
# ---------------------------------------------------------------------------


class TestRunWithTimeout:
    async def test_timeout_sets_failed_status(self):
        """When analysis times out, the job should be marked as failed."""
        import asyncio

        service = MagicMock()

        async def slow_analysis(*args, **kwargs):
            await asyncio.sleep(10)

        app = MagicMock()
        app.state.analysis_jobs = {"job-timeout": {"status": "processing"}}
        app.state.cancellation_tokens = {}
        app.state.db_session_factory = None

        body = MagicMock()
        body.config.timeout_seconds = 0.01
        body.pages = []

        with patch("src.domains.analyze.routes._process_analysis", side_effect=slow_analysis):
            await _run_with_timeout(
                service, "job-timeout", body, app, "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

        assert app.state.analysis_jobs["job-timeout"]["status"] == "failed"
        assert "Timed out" in app.state.analysis_jobs["job-timeout"]["error"]

    async def test_timeout_persists_to_db(self):
        """When timeout occurs and DB is available, persist failure."""
        import asyncio

        service = MagicMock()

        async def slow_analysis(*args, **kwargs):
            await asyncio.sleep(10)

        # Set up mock DB session — use MagicMock (not AsyncMock) because
        # session.begin() is synchronous in SQLAlchemy. AsyncMock would create
        # an unawaited coroutine from .begin(), triggering RuntimeWarning.
        mock_session = MagicMock()
        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=mock_begin)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = mock_begin
        # `bind_session_tenant` awaits `session.execute`. It used to be a NO-OP —
        # w2v_common's `TENANT_RLS_MODE` defaulted to "off" — so a MagicMock
        # session was enough. #113 flipped the default to "enforce" (fail-CLOSED),
        # so the bind now really runs and the mock must be awaitable.
        mock_session.execute = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session_factory = MagicMock(return_value=mock_ctx)

        app = MagicMock()
        app.state.analysis_jobs = {"job-timeout-db": {"status": "processing"}}
        app.state.cancellation_tokens = {}
        app.state.db_session_factory = session_factory

        body = MagicMock()
        body.config.timeout_seconds = 0.01
        body.pages = []

        with (
            patch("src.domains.analyze.routes._process_analysis", side_effect=slow_analysis),
            patch("src.domains.analyze.routes.AnalysisJobRepository") as mock_repo,
        ):
            mock_repo.update_status = AsyncMock()
            await _run_with_timeout(
                service, "job-timeout-db", body, app, "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

        assert app.state.analysis_jobs["job-timeout-db"]["status"] == "failed"


# ---------------------------------------------------------------------------
# start_analysis — correlation + tenant propagation to background task
# ---------------------------------------------------------------------------


class TestStartAnalysisPropagation:
    @pytest.mark.asyncio
    async def test_start_analysis_propagates_correlation_and_tenant_to_bg_task(self) -> None:
        """The dispatch must carry the request's cost-correlation + tenant into the
        detached _run_with_timeout task, or its downstream WM/IR cost events go out
        with a NULL correlation_id and the wrong tenant. Guards the create_task
        context-inheritance contract against a future regression (e.g. moving the
        work to a thread/process pool or a blank context)."""
        import asyncio

        from w2v_common.auth import (
            clear_identity_context,
            get_correlation_id_from_context,
            get_stage_from_context,
            get_tenant_id_from_context,
            set_correlation_context,
            set_identity_context,
        )

        from src.domains.analyze.routes import _background_tasks

        seen: dict[str, object] = {}

        async def _spy(*_args, **_kwargs) -> None:
            seen["correlation_id"] = get_correlation_id_from_context()
            seen["stage"] = get_stage_from_context()
            seen["tenant_id"] = get_tenant_id_from_context()

        request = MagicMock()
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analyzer_service = MagicMock()
        request.app.state.analysis_jobs = {}
        request.app.state.db_session_factory = None
        body = AnalyzeRequest(pages=[CrawledPageInput(url="https://example.com/")])

        set_identity_context(uuid.UUID("33333333-3333-4333-8333-333333333333"), "lic-1", "subj-1")
        set_correlation_context("run-xyz", "analyze")
        try:
            with patch("src.domains.analyze.routes._run_with_timeout", new=_spy):
                await start_analysis(body, request)
                # Let the scheduled background task run to completion.
                await asyncio.gather(*list(_background_tasks))
        finally:
            clear_identity_context()
            set_correlation_context(None, None)

        assert seen["correlation_id"] == "run-xyz"
        assert seen["stage"] == "analyze"
        assert seen["tenant_id"] == uuid.UUID("33333333-3333-4333-8333-333333333333")


# ---------------------------------------------------------------------------
# list_analysis_jobs
# ---------------------------------------------------------------------------


class TestListAnalysisJobs:
    async def test_no_session_factory_returns_empty(self):
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.headers.get.return_value = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
        request.app.state.db_session_factory = None

        result = await list_analysis_jobs(request)
        assert result.jobs == []
        assert result.total == 0

    async def test_db_exception_raises_503_not_empty(self):
        # A DB outage must NOT be swallowed into an empty list (which would be
        # indistinguishable from "this tenant has 0 jobs") — it surfaces as 503.
        from fastapi import HTTPException

        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.headers.get.return_value = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"

        session_factory = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("db error"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory.return_value = ctx
        request.app.state.db_session_factory = session_factory

        with pytest.raises(HTTPException) as exc_info:
            await list_analysis_jobs(request)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# get_analysis - fallback to DB
# ---------------------------------------------------------------------------


class TestGetAnalysis:
    async def test_job_not_found_raises_404(self):
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {}
        request.app.state.db_session_factory = None

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_analysis("nonexistent-job", request)
        assert exc_info.value.status_code == 404

    async def test_db_fallback_with_results_json(self):
        """When job is not in memory but in DB with results_json, should return it."""
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {}

        db_job = MagicMock()
        db_job.status = "completed"
        db_job.total_pages = 2
        db_job.pages_analyzed = [
            {
                "page_id": "p1",
                "url": "https://example.com",
                "title": "Test",
                "page_type": "homepage",
                "interactive_elements": [],
                "content_sections": [],
                "navigation": [],
                "headings": [],
                "word_count": 10,
                "has_forms": False,
                "has_media": False,
                "meta_description": "",
            }
        ]
        db_job.site_analysis = {"tone": "professional"}
        db_job.narrative_extraction = {}
        db_job.error = None
        db_job.results_json = {
            "enriched_context": {"industry": "tech"},
            "language_duplicates_removed": 1,
            "processing_time_seconds": 2.5,
            "llm_calls": 3,
            "schema_version": "1.0",
            "pages_analyzed_count": 1,
        }

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=mock_ctx)

        request.app.state.db_session_factory = session_factory

        with patch(
            "src.domains.analyze.routes.AnalysisJobRepository.get",
            new_callable=AsyncMock,
            return_value=db_job,
        ):
            result = await get_analysis("db-job-123", request)
            assert result.status == "completed"
            assert result.enriched_context == {"industry": "tech"}
            assert result.llm_calls == 3


# ---------------------------------------------------------------------------
# cancel_analysis
# ---------------------------------------------------------------------------


class TestCancelAnalysis:
    async def test_cancel_in_memory_job(self):
        """No DB configured + matching-tenant cache entry: cancel succeeds."""
        request = MagicMock()
        request.state.tenant_id = "d442c70e-00db-57a0-9d48-936b9da74afa"
        request.app.state.cancellation_tokens = {}
        request.app.state.analysis_jobs = {
            "job-c1": {"status": "processing", "tenant_id": "d442c70e-00db-57a0-9d48-936b9da74afa"}
        }
        request.app.state.db_session_factory = None

        result = await cancel_analysis("job-c1", request)
        assert result.status == "cancelled"
        assert request.app.state.analysis_jobs["job-c1"]["status"] == "cancelled"

    async def test_cancel_cross_tenant_cache_returns_404(self):
        """Cross-tenant cache hit must be hidden as 404 — no leak via cancel."""
        from fastapi import HTTPException

        request = MagicMock()
        request.state.tenant_id = "bf7f867f-3cd3-5a4a-bfb4-475d56132c9d"
        request.app.state.cancellation_tokens = {}
        request.app.state.analysis_jobs = {
            "job-c-isolated": {
                "status": "processing",
                "tenant_id": "29763c73-ce2f-547e-90e1-fa1a09d3a976",
            }
        }
        request.app.state.db_session_factory = None

        with pytest.raises(HTTPException) as exc_info:
            await cancel_analysis("job-c-isolated", request)
        assert exc_info.value.status_code == 404
        # Owner's job must remain untouched.
        assert request.app.state.analysis_jobs["job-c-isolated"]["status"] == "processing"

    async def test_cancel_db_error_falls_back_to_cancelled(self):
        """Transient DB error with no cache: best-effort cancelled (degraded path)."""
        request = MagicMock()
        request.state.tenant_id = "d442c70e-00db-57a0-9d48-936b9da74afa"
        request.app.state.cancellation_tokens = {}
        request.app.state.analysis_jobs = {}

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("db error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=mock_ctx)
        request.app.state.db_session_factory = session_factory

        result = await cancel_analysis("job-c3", request)
        assert result.status == "cancelled"

    async def test_cancel_db_returns_none_returns_404(self):
        """DB query succeeds but returns no row for this tenant: 404."""
        from fastapi import HTTPException

        request = MagicMock()
        request.state.tenant_id = "d442c70e-00db-57a0-9d48-936b9da74afa"
        request.app.state.cancellation_tokens = {}
        request.app.state.analysis_jobs = {}

        # session.begin() is sync in SQLAlchemy — MagicMock, not AsyncMock —
        # otherwise the route's `session.begin()` returns a coroutine that
        # can't be used as a context manager. See test_run_with_timeout_db
        # for the canonical pattern.
        mock_session = MagicMock()
        mock_begin = AsyncMock()
        mock_begin.__aenter__ = AsyncMock(return_value=mock_begin)
        mock_begin.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = mock_begin

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        session_factory = MagicMock(return_value=mock_ctx)
        request.app.state.db_session_factory = session_factory

        with (
            patch(
                "src.domains.analyze.routes.AnalysisJobRepository.cancel",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await cancel_analysis("job-c4", request)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# get_artifacts
# ---------------------------------------------------------------------------


class TestGetArtifacts:
    async def test_from_memory(self):
        sample_artifacts = [{"type": "llm_call", "model": "claude", "prompt_preview": "test"}]
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {"job-a1": {"_artifacts": sample_artifacts}}
        request.app.state.db_session_factory = None

        result = await get_artifacts("job-a1", request)
        assert result.job_id == "job-a1"
        assert len(result.artifacts) == 1

    async def test_from_db(self):
        sample_artifacts = [{"type": "llm_call", "model": "claude", "prompt_preview": "db-test"}]
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {}

        mock_session = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=mock_ctx)
        request.app.state.db_session_factory = session_factory

        with patch(
            "src.domains.analyze.routes.AnalysisJobRepository.get_artifacts",
            new_callable=AsyncMock,
            return_value=sample_artifacts,
        ):
            result = await get_artifacts("job-a2", request)
            assert len(result.artifacts) == 1

    async def test_not_found_raises_404(self):
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {}
        request.app.state.db_session_factory = None

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_artifacts("job-a3", request)
        assert exc_info.value.status_code == 404

    async def test_db_error_falls_through_to_404(self):
        """DB error during artifacts lookup should fall through to 404."""
        request = MagicMock()
        # 0.67.0: request.state.tenant_id is parsed, so a bare MagicMock
        # attribute is now an InvalidTenantIdError, not a silent pass.
        request.state.tenant_id = "33333333-3333-4333-8333-333333333333"
        request.app.state.analysis_jobs = {}

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("db error"))
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=mock_ctx)
        request.app.state.db_session_factory = session_factory

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_artifacts("job-a4", request)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# get_review
# ---------------------------------------------------------------------------


class TestGetReview:
    async def test_found(self):
        request = MagicMock()
        request.state.tenant_id = "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c"
        request.app.state.db_session_factory = None
        request.app.state.review_jobs = {
            "rev-1": {
                "status": "completed",
                "tenant_id": "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c",
                "reviews": {
                    "seo": {
                        "review_type": "seo",
                        "overall_score": 85,
                        "categories": {},
                        "strengths": [],
                        "weaknesses": [],
                        "issues": [],
                        "recommendations": [],
                    }
                },
                "processing_time_seconds": 1.5,
                "llm_calls": 2,
            }
        }

        result = await get_review("rev-1", request)
        assert result.status == "completed"
        assert result.llm_calls == 2

    async def test_not_found_raises_404(self):
        request = MagicMock()
        request.state.tenant_id = "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c"
        request.app.state.db_session_factory = None
        request.app.state.review_jobs = {}

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await get_review("rev-none", request)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Context endpoints: infer_context, validate_context, enrich_context
# ---------------------------------------------------------------------------


class TestInferContext:
    async def test_from_pages_only(self):
        body = {
            "pages": [{"page_type": "homepage", "url": "https://example.com/", "has_forms": False}]
        }
        result = await infer_context(body)
        assert isinstance(result, dict)

    async def test_with_site_analysis(self):
        body = {
            "pages": [{"page_type": "homepage", "url": "https://example.com/"}],
            "site_analysis": {
                "description": "A tech company",
                "target_audience": ["developers"],
                "tone": "professional",
            },
        }
        result = await infer_context(body)
        assert isinstance(result, dict)

    async def test_empty_pages(self):
        body = {"pages": []}
        result = await infer_context(body)
        assert isinstance(result, dict)


class TestValidateContext:
    async def test_valid_context(self):
        body = {
            "context": {"description": "A test site"},
            "discovered_urls": ["https://example.com/"],
        }
        result = await validate_context(body)
        assert isinstance(result, dict)
        assert "issues" in result

    async def test_with_page_types_and_features(self):
        body = {
            "context": {
                "focus_pages": ["https://example.com/pricing"],
                "feature_priorities": [{"feature": "pricing", "priority": "high"}],
            },
            "discovered_urls": ["https://example.com/", "https://example.com/pricing"],
            "page_types": {
                "https://example.com/": "homepage",
                "https://example.com/pricing": "pricing",
            },
            "features_found": ["pricing", "contact"],
        }
        result = await validate_context(body)
        assert isinstance(result, dict)

    async def test_empty_context(self):
        body = {"context": {}, "discovered_urls": []}
        result = await validate_context(body)
        assert isinstance(result, dict)
        assert result["issues"] == []


class TestEnrichContext:
    async def test_merge_user_and_inferred(self):
        body = {
            "user_context": {"description": "My company"},
            "inferred_context": {
                "description": "Auto detected company",
                "site_type": "saas",
            },
        }
        result = await enrich_context(body)
        assert isinstance(result, dict)

    async def test_no_user_context(self):
        body = {
            "user_context": None,
            "inferred_context": {
                "description": "Inferred",
                "site_type": "ecommerce",
            },
        }
        result = await enrich_context(body)
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _process_review
# ---------------------------------------------------------------------------


class TestProcessReview:
    async def test_successful_review(self):
        app = MagicMock()
        app.state.db_session_factory = None
        review_jobs = {"rev-test-1": {"status": "processing"}}
        app.state.review_jobs = review_jobs

        mock_generator = MagicMock()
        mock_generator.generate_reviews = AsyncMock(
            return_value={"seo": {"score": 90, "llm_calls": 1}}
        )

        with (
            patch(
                "src.domains.analyze.services.review_generator.ReviewGenerator",
                return_value=mock_generator,
            ),
        ):
            await _process_review(
                review_job_id="rev-test-1",
                review_types=["seo"],
                analysis_result={"pages": [], "site_analysis": {}},
                user_context=None,
                app=app,
                # 0.67.0: the "anonymous" default is gone — tenant_id is required.
                tenant_id="33333333-3333-4333-8333-333333333333",
            )

        assert review_jobs["rev-test-1"]["status"] == "completed"

    async def test_failed_review(self):
        app = MagicMock()
        app.state.db_session_factory = None
        review_jobs = {"rev-fail-1": {"status": "processing"}}
        app.state.review_jobs = review_jobs

        mock_generator = MagicMock()
        mock_generator.generate_reviews = AsyncMock(side_effect=RuntimeError("LLM error"))

        with (
            patch(
                "src.domains.analyze.services.review_generator.ReviewGenerator",
                return_value=mock_generator,
            ),
        ):
            await _process_review(
                review_job_id="rev-fail-1",
                review_types=["seo"],
                analysis_result={"pages": [], "site_analysis": {}},
                user_context=None,
                app=app,
                # 0.67.0: the "anonymous" default is gone — tenant_id is required.
                tenant_id="33333333-3333-4333-8333-333333333333",
            )

        assert review_jobs["rev-fail-1"]["status"] == "failed"
        assert "LLM error" in review_jobs["rev-fail-1"]["error"]
