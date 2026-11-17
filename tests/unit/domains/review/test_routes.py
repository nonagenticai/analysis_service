"""Unit tests for API routes.

Tests the route handlers directly with mocked dependencies.
Covers: start_review, _process_review, compare_reviews, batch endpoints,
get_review (cache hit/miss), summary, findings, report, progress, list, usage,
and domain history.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Stands in for what ``AuthMiddleware`` would put on ``request.state``.
_TEST_TENANT = "aeb670dd-a0f9-5110-893b-2820fbdf794e"

SAMPLE_ANALYSIS = {
    "pages": [
        {
            "url": "https://example.com",
            "title": "Example Site",
            "page_type": "homepage",
            "word_count": 500,
            "interactive_elements": [],
            "content_sections": [],
            "headings": ["Welcome"],
            "has_forms": False,
            "has_media": True,
        }
    ],
    "site": {
        "description": "A sample website",
        "target_audience": ["developers"],
        "use_cases": [],
        "selling_points": ["Fast"],
        "narrative_hooks": [],
        "tone": "professional",
        "visual_identity": {},
    },
}

MOCK_COMPLETED_RESULTS = {
    "design_review": {
        "overall_score": 7.5,
        "categories": {
            "layout_consistency": {"score": 8, "observations": ["Good grid"]},
        },
        "strengths": ["Strong layout"],
        "weaknesses": ["Minor issues"],
        "recommendations": ["Improve contrast"],
    },
}


def _make_mock_job(
    *,
    job_id: str = "test-job-1",
    status: str = "completed",
    results: dict | None = None,
    review_types: list[str] | None = None,
    error: str | None = None,
    domain: str | None = None,
    created_at: datetime | None = None,
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
):
    job = MagicMock()
    job.job_id = job_id
    job.status = status
    job.results = results if results is not None else MOCK_COMPLETED_RESULTS
    job.review_types = review_types or ["design_review"]
    job.error = error
    job.domain = domain
    job.created_at = created_at or datetime(2025, 1, 1, 12, 0, 0)
    job.tenant_id = tenant_id
    return job


def _make_mock_batch(
    *,
    batch_id: str = "batch-1",
    status: str = "processing",
    total: int = 2,
    completed: int = 0,
    failed: int = 0,
    job_ids: list[str] | None = None,
):
    batch = MagicMock()
    batch.batch_id = batch_id
    batch.status = status
    batch.total = total
    batch.completed = completed
    batch.failed = failed
    batch.job_ids = job_ids or ["job-1", "job-2"]
    return batch


def _make_mock_usage(
    *,
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
    usage_date: date | None = None,
    review_count: int = 5,
    error_count: int = 1,
):
    usage = MagicMock()
    usage.tenant_id = tenant_id
    usage.date = usage_date or date(2025, 1, 1)
    usage.review_count = review_count
    usage.error_count = error_count
    return usage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_session():
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            scalar=MagicMock(return_value=0),
            scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
        )
    )
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def mock_settings():
    settings = MagicMock()
    settings.app_name = "Review Service"
    settings.app_version = "0.1.0-test"
    settings.environment = "test"
    settings.database.url = (
        "postgresql+psycopg://test:test@localhost:5432/test"  # pragma: allowlist secret
    )
    settings.redis.url = "redis://localhost:6394/0"
    settings.api.host = "0.0.0.0"
    settings.api.port = 6304
    settings.api.cors_origins = ["*"]
    settings.api.cors_allow_credentials = True
    settings.api.cors_allow_methods = ["*"]
    settings.api.cors_allow_headers = ["*"]
    settings.api.debug = True
    settings.api.reload = False
    settings.auth.enabled = False
    settings.logging.level = "DEBUG"
    settings.logging.json_logs = False
    settings.logging.use_structlog = False
    settings.review.timeout_seconds = 180
    settings.review.job_retention_hours = 72
    settings.review.cleanup_interval_minutes = 60
    settings.review.max_pages_per_request = 50
    settings.feature_flags.sse_progress = False
    settings.feature_flags.batch_reviews = False
    settings.webhook.enabled = False
    settings.video_compiler.enabled = True
    # cachetools >= 7.2 validates maxsize (`maxsize < 0`), so a MagicMock here
    # raises TypeError in the lifespan's TTLCache(...) — give them real ints.
    settings.cache.job_maxsize = 1000
    settings.cache.job_ttl = 3600
    settings.cache.review_maxsize = 500
    settings.cache.review_ttl = 3600
    settings.video_compiler.timeout = 300
    settings.cache.enabled = False
    settings.cache.ttl = 3600
    settings.rate_limit.enabled = False
    settings.tracing.enabled = False
    settings.llm.api_key = ""
    settings.llm.base_url = ""
    settings.llm.model = "test-model"
    settings.scoring.aggregate_weights = {
        "design_review": 0.20,
        "usability_review": 0.25,
        "content_quality_review": 0.20,
        "feature_review": 0.20,
        "feature_suggestions": 0.15,
    }
    settings.scoring.get_category_weights = MagicMock(return_value={})
    settings.is_production = MagicMock(return_value=False)
    settings.is_development = MagicMock(return_value=True)
    return settings


@pytest.fixture
def mock_redis():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    client.is_available = True
    return client


@pytest.fixture
def mock_db_engine():
    engine = AsyncMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=MagicMock(scalar=MagicMock(return_value=1)))
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=conn)
    engine.begin = MagicMock(return_value=conn)
    engine.dispose = AsyncMock()
    return engine


@pytest.fixture
async def app_and_client(mock_db_engine, mock_db_session, mock_redis, mock_settings):
    """Create test app with mocked dependencies, yielding (app, client)."""
    import src.asgi

    @asynccontextmanager
    async def _mock_get_db_session() -> AsyncGenerator[AsyncMock]:
        yield mock_db_session

    async def _noop_cleanup(**kwargs) -> None:
        await asyncio.sleep(999999)

    mock_session_maker = AsyncMock()
    mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_db_session)
    mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch(
            "src.shared.database.connection.get_engine",
            new_callable=AsyncMock,
            return_value=mock_db_engine,
        ),
        patch(
            "w2v_common.database.session.get_engine",
            new_callable=AsyncMock,
            return_value=mock_db_engine,
        ),
        patch(
            "w2v_common.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=mock_session_maker,
        ),
        patch(
            "src.shared.cache.redis_client.init_redis",
            new_callable=AsyncMock,
            return_value=mock_redis,
        ),
        patch(
            "src.shared.cache.redis_client.get_redis_client",
            return_value=mock_redis,
        ),
        patch(
            "src.shared.cache.redis_client.close_redis",
            new_callable=AsyncMock,
        ),
        patch(
            "src.shared.database.connection.close_engine",
            new_callable=AsyncMock,
        ),
        patch.object(src.asgi, "settings", mock_settings),
        patch("src.domains.review.services.cleanup.cleanup_old_jobs", side_effect=_noop_cleanup),
        patch("src.domains.review.routes.get_db_session", _mock_get_db_session),
        # These tests drive the REAL ASGI app with ``settings.auth.enabled =
        # False``, so ``AuthMiddleware`` never runs and never sets
        # ``request.state.tenant_id``. The route used to paper over that by
        # fabricating the literal ``"anonymous"``; since migration 0013 made
        # ``tenant_id`` a native ``uuid`` column that fallback is gone and an
        # absent tenant is a 401 (see ``src.shared.tenant``). Standing in for the
        # middleware here keeps these tests about the ROUTES, which is what they
        # are for — the boundary itself is proven in
        # ``tests/unit/shared/test_migration_0013_tenant_uuid.py``.
        patch("src.domains.review.routes._get_tenant_id", return_value=_TEST_TENANT),
    ):
        from src.shared.database.session import get_db

        async def _override_get_db() -> AsyncGenerator[AsyncMock]:
            yield mock_db_session

        src.asgi.app.dependency_overrides[get_db] = _override_get_db
        try:
            async with src.asgi.app.router.lifespan_context(src.asgi.app):
                transport = ASGITransport(app=src.asgi.app)
                async with AsyncClient(transport=transport, base_url="http://test") as ac:
                    yield src.asgi.app, ac
        finally:
            src.asgi.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests: Compare endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCompareReviews:
    async def test_compare_two_completed_jobs(self, app_and_client) -> None:
        """Compare endpoint returns comparison data for two completed jobs."""
        _app, client = app_and_client
        job1 = _make_mock_job(job_id="j1")
        job2 = _make_mock_job(job_id="j2")

        with patch("src.domains.review.routes.JobManager") as mock_jm:

            async def _get_job(db, jid, *, tenant_id):
                return {"j1": job1, "j2": job2}.get(jid)

            mock_jm.get_job = AsyncMock(side_effect=_get_job)

            with patch("src.domains.review.routes.get_settings") as mock_gs:
                mock_gs.return_value = MagicMock(
                    cache=MagicMock(enabled=False),
                )
                response = await client.get("/api/review/compare?job_id_1=j1&job_id_2=j2")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id_1"] == "j1"
        assert data["job_id_2"] == "j2"

    async def test_compare_missing_job_returns_404(self, app_and_client) -> None:
        """Compare endpoint returns 404 if one job is missing."""
        _app, client = app_and_client
        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_job = AsyncMock(return_value=None)

            response = await client.get("/api/review/compare?job_id_1=j1&job_id_2=j2")
        assert response.status_code == 404

    async def test_compare_incomplete_job_returns_400(self, app_and_client) -> None:
        """Compare endpoint returns 400 if a job is not completed."""
        _app, client = app_and_client
        job1 = _make_mock_job(job_id="j1", status="completed")
        job2 = _make_mock_job(job_id="j2", status="processing", results=None)

        with patch("src.domains.review.routes.JobManager") as mock_jm:

            async def _get_job(db, jid, *, tenant_id):
                return {"j1": job1, "j2": job2}.get(jid)

            mock_jm.get_job = AsyncMock(side_effect=_get_job)

            response = await client.get("/api/review/compare?job_id_1=j1&job_id_2=j2")
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Tests: Get review (cache hit/miss)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetReview:
    async def test_get_review_cache_hit(self, app_and_client) -> None:
        """When cache is enabled and hit, return cached result."""
        _app, client = app_and_client
        cached_data = {"job_id": "j1", "status": "completed", "results": {}, "error": None}

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager"),
        ):
            mock_gs.return_value = MagicMock(cache=MagicMock(enabled=True))

            with patch("src.domains.review.services.review_cache.get_redis_client") as mock_rc:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=cached_data)
                mock_rc.return_value = mock_client

                response = await client.get("/api/review/j1")

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "j1"

    async def test_get_review_from_db(self, app_and_client) -> None:
        """When cache disabled, return from DB."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
        ):
            mock_gs.return_value = MagicMock(cache=MagicMock(enabled=False))
            mock_jm.get_job = AsyncMock(return_value=job)

            response = await client.get("/api/review/j1")

        assert response.status_code == 200

    async def test_get_review_not_found(self, app_and_client) -> None:
        """Returns 404 for nonexistent job."""
        _app, client = app_and_client

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
        ):
            mock_gs.return_value = MagicMock(cache=MagicMock(enabled=False))
            mock_jm.get_job = AsyncMock(return_value=None)

            response = await client.get("/api/review/nonexistent")

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Tests: Review summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReviewSummary:
    async def test_summary_returns_data(self, app_and_client) -> None:
        """Summary endpoint returns aggregate data for completed job."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.generate_summary") as mock_summary,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={"design_review": 1.0}),
            )
            mock_summary.return_value = {
                "job_id": "j1",
                "aggregate_score": 7.5,
                "grade": "B",
                "per_review_scores": {"design_review": 7.5},
                "top_strengths": ["Good layout"],
                "critical_issues": ["Minor issues"],
                "top_recommendations": ["Improve contrast"],
                "score_100": 75.0,
                "penalty_score": None,
            }

            response = await client.get("/api/review/j1/summary")

        assert response.status_code == 200
        data = response.json()
        assert data["aggregate_score"] == 7.5

    async def test_summary_not_completed_returns_400(self, app_and_client) -> None:
        """Summary returns 400 for non-completed job."""
        _app, client = app_and_client
        job = _make_mock_job(status="processing", results=None)

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_job = AsyncMock(return_value=job)

            response = await client.get("/api/review/j1/summary")

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Tests: Findings endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFindings:
    async def test_findings_returns_paginated(self, app_and_client) -> None:
        """Findings endpoint returns paginated findings."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.services.findings.extract_findings") as mock_extract,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_extract.return_value = [
                {
                    "review_type": "design_review",
                    "severity": "high",
                    "title": "Low contrast",
                    "description": "Buttons have low contrast",
                    "category": "contrast",
                },
            ]

            response = await client.get("/api/review/j1/findings")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["findings"]) == 1

    async def test_findings_not_completed_returns_400(self, app_and_client) -> None:
        """Findings returns 400 for non-completed job."""
        _app, client = app_and_client
        job = _make_mock_job(status="processing", results=None)

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_job = AsyncMock(return_value=job)

            response = await client.get("/api/review/j1/findings")

        assert response.status_code == 400

    async def test_findings_filter_by_severity(self, app_and_client) -> None:
        """Findings endpoint filters by severity."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.services.findings.extract_findings") as mock_extract,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_extract.return_value = [
                {
                    "review_type": "design_review",
                    "severity": "high",
                    "title": "Issue 1",
                    "description": "Desc",
                    "category": "cat",
                },
                {
                    "review_type": "design_review",
                    "severity": "low",
                    "title": "Issue 2",
                    "description": "Desc",
                    "category": "cat",
                },
            ]

            response = await client.get("/api/review/j1/findings?severity=high")

        data = response.json()
        assert data["total"] == 1


# ---------------------------------------------------------------------------
# Tests: Report endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReportEndpoint:
    async def test_report_markdown_default(self, app_and_client) -> None:
        """Report endpoint returns markdown by default."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.generate_summary") as mock_summary,
            patch(
                "src.domains.review.services.report_generator.generate_markdown_report"
            ) as mock_gen,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={"design_review": 1.0}),
                cache=MagicMock(enabled=False),
            )
            mock_summary.return_value = {"aggregate_score": 7.5, "grade": "B"}
            mock_gen.return_value = "# Report\n\nContent here."

            response = await client.get("/api/review/j1/report")

        assert response.status_code == 200

    async def test_report_json_format(self, app_and_client) -> None:
        """Report endpoint returns JSON when format=json."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.generate_summary") as mock_summary,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={"design_review": 1.0}),
                cache=MagicMock(enabled=False),
            )
            mock_summary.return_value = {"aggregate_score": 7.5, "grade": "B"}

            response = await client.get("/api/review/j1/report?format=json")

        assert response.status_code == 200

    async def test_report_html_format(self, app_and_client) -> None:
        """Report endpoint returns HTML when format=html."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.generate_summary") as mock_summary,
            patch(
                "src.domains.review.services.html_report_generator.generate_html_report"
            ) as mock_gen,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={"design_review": 1.0}),
                cache=MagicMock(enabled=False),
            )
            mock_summary.return_value = {"aggregate_score": 7.5, "grade": "B"}
            mock_gen.return_value = "<html><body>Report</body></html>"

            response = await client.get("/api/review/j1/report?format=html")

        assert response.status_code == 200
        assert "html" in response.headers.get("content-type", "").lower()

    async def test_report_not_found(self, app_and_client) -> None:
        """Report returns 404 for missing job."""
        _app, client = app_and_client
        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
        ):
            mock_jm.get_job = AsyncMock(return_value=None)
            mock_gs.return_value = MagicMock(cache=MagicMock(enabled=False))

            response = await client.get("/api/review/nonexistent/report")

        assert response.status_code == 404

    async def test_report_not_completed_returns_400(self, app_and_client) -> None:
        """Report returns 400 for processing job."""
        _app, client = app_and_client
        job = _make_mock_job(status="processing", results=None)

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(cache=MagicMock(enabled=False))

            response = await client.get("/api/review/j1/report")

        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Tests: SSE Progress
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProgressEndpoint:
    async def test_progress_disabled_returns_501(self, app_and_client) -> None:
        """Progress endpoint returns 501 when SSE flag is off."""
        _app, client = app_and_client
        with patch("src.domains.review.routes.get_settings") as mock_gs:
            mock_gs.return_value = MagicMock(
                feature_flags=MagicMock(sse_progress=False),
                cache=MagicMock(enabled=False),
            )

            response = await client.get("/api/review/j1/progress")

        assert response.status_code == 501


# ---------------------------------------------------------------------------
# Tests: Batch endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBatchEndpoints:
    async def test_batch_disabled_returns_501(self, app_and_client) -> None:
        """Batch endpoint returns 501 when feature flag is off."""
        _app, client = app_and_client
        with patch("src.domains.review.routes.get_settings") as mock_gs:
            mock_gs.return_value = MagicMock(
                feature_flags=MagicMock(batch_reviews=False),
            )

            response = await client.post(
                "/api/review/batch",
                json={"reviews": [{"analysis": SAMPLE_ANALYSIS}]},
            )

        assert response.status_code == 501

    async def test_batch_status_not_found(self, app_and_client) -> None:
        """Batch status returns 404 for missing batch."""
        _app, client = app_and_client
        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_batch = AsyncMock(return_value=None)

            response = await client.get("/api/review/batch/nonexistent")

        assert response.status_code == 404

    async def test_batch_status_returns_progress(self, app_and_client) -> None:
        """Batch status returns current progress."""
        _app, client = app_and_client
        batch = _make_mock_batch(status="completed", completed=2, failed=0)

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_batch = AsyncMock(return_value=batch)
            mock_jm.update_batch_progress = AsyncMock()

            response = await client.get("/api/review/batch/batch-1")

        assert response.status_code == 200
        data = response.json()
        assert data["completed"] == 2

    async def test_batch_create_enabled(self, app_and_client) -> None:
        """Batch create works when feature flag is enabled."""
        _app, client = app_and_client

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
        ):
            mock_gs.return_value = MagicMock(
                feature_flags=MagicMock(batch_reviews=True),
                review=MagicMock(max_pages_per_request=50),
                webhook=MagicMock(enabled=False),
                cache=MagicMock(enabled=False),
            )
            mock_jm.create_job = AsyncMock(return_value=MagicMock())
            mock_jm.create_batch = AsyncMock(return_value=MagicMock())

            response = await client.post(
                "/api/review/batch",
                json={
                    "reviews": [
                        {"analysis": SAMPLE_ANALYSIS, "review_types": ["design_review"]},
                    ],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert "batch_id" in data
        assert data["total"] == 1


# ---------------------------------------------------------------------------
# Tests: List reviews
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListReviews:
    async def test_list_returns_jobs(self, app_and_client) -> None:
        """List endpoint returns paginated jobs."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.list_jobs = AsyncMock(return_value=([job], 1))

            response = await client.get("/api/review")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["jobs"]) == 1


# ---------------------------------------------------------------------------
# Tests: Usage endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUsageEndpoint:
    async def test_usage_returns_records(self, app_and_client) -> None:
        """Usage endpoint returns usage records."""
        _app, client = app_and_client
        usage = _make_mock_usage()

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_usage = AsyncMock(return_value=[usage])

            response = await client.get("/api/usage")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

    async def test_usage_with_date_range(self, app_and_client) -> None:
        """Usage endpoint accepts date range parameters."""
        _app, client = app_and_client

        with patch("src.domains.review.routes.JobManager") as mock_jm:
            mock_jm.get_usage = AsyncMock(return_value=[])

            response = await client.get("/api/usage?start_date=2025-01-01&end_date=2025-12-31")

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests: Domain history
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDomainHistory:
    async def test_domain_history_returns_entries(self, app_and_client) -> None:
        """Domain history returns review entries with trend."""
        _app, client = app_and_client
        job1 = _make_mock_job(job_id="j1", domain="example.com")
        job2 = _make_mock_job(
            job_id="j2",
            domain="example.com",
            results={
                "design_review": {
                    "overall_score": 5.0,
                    "categories": {},
                    "strengths": [],
                    "weaknesses": [],
                    "recommendations": [],
                }
            },
        )

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.generate_summary") as mock_summary,
        ):
            mock_jm.get_domain_history = AsyncMock(return_value=[job1, job2])
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={"design_review": 1.0}),
            )
            # Return different scores for each call to simulate a trend
            mock_summary.side_effect = [
                {"aggregate_score": 8.0, "grade": "B+"},
                {"aggregate_score": 5.0, "grade": "C"},
            ]

            response = await client.get("/api/domain/example.com/history")

        assert response.status_code == 200
        data = response.json()
        assert data["domain"] == "example.com"
        assert data["total"] == 2
        assert data["trend"] == "improving"  # first (8.0) > last (5.0)

    async def test_domain_history_empty(self, app_and_client) -> None:
        """Domain history returns empty list for unknown domain."""
        _app, client = app_and_client

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
        ):
            mock_jm.get_domain_history = AsyncMock(return_value=[])
            mock_gs.return_value = MagicMock(
                scoring=MagicMock(aggregate_weights={}),
            )

            response = await client.get("/api/domain/unknown.com/history")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["trend"] is None


# ---------------------------------------------------------------------------
# Tests: Start review validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStartReviewValidation:
    async def test_invalid_callback_url_returns_400(self, app_and_client) -> None:
        """Review with invalid callback URL returns 400."""
        _app, client = app_and_client

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.shared.security.url_validator.validate_callback_url") as mock_validate,
        ):
            mock_gs.return_value = MagicMock(
                review=MagicMock(max_pages_per_request=50),
            )
            mock_validate.side_effect = ValueError("SSRF blocked")

            response = await client.post(
                "/api/review",
                json={
                    "analysis": SAMPLE_ANALYSIS,
                    "review_types": ["design_review"],
                    "callback_url": "http://169.254.169.254/latest/meta-data/",
                },
            )

        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "callback URL" in message.lower() or "callback" in message

    async def test_too_many_pages_returns_400(self, app_and_client) -> None:
        """Review with too many pages returns 400."""
        _app, client = app_and_client

        # Create analysis with many pages
        many_pages = [
            {
                "url": f"https://example.com/page{i}",
                "page_type": "page",
            }
            for i in range(60)
        ]
        analysis = {
            "pages": many_pages,
            "site": {"description": "Test site"},
        }

        with patch("src.domains.review.routes.get_settings") as mock_gs:
            mock_gs.return_value = MagicMock(
                review=MagicMock(max_pages_per_request=50),
            )

            response = await client.post(
                "/api/review",
                json={
                    "analysis": analysis,
                    "review_types": ["design_review"],
                },
            )

        assert response.status_code == 400
        assert "too many pages" in response.json()["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Tests: Video endpoint
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVideoEndpoint:
    async def test_video_returns_job_id(self, app_and_client) -> None:
        """Video endpoint returns video job_id for completed review."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1", domain="example.com")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.services.findings.extract_findings") as mock_extract,
            patch("src.domains.review.services.video_client.VideoCompilerClient") as mock_video_cls,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                video_compiler=MagicMock(enabled=True, base_url="http://test:6309", timeout=300),
                cache=MagicMock(enabled=False),
            )
            mock_extract.return_value = [
                {
                    "review_type": "design_review",
                    "severity": "high",
                    "description": "Low contrast",
                    "evidence": ["obs1", "obs2"],
                    "category": "contrast",
                },
            ]
            mock_client_instance = AsyncMock()
            mock_client_instance.request_findings_video.return_value = {
                "job_id": "video-123",
                "poll_url": "/api/video/video-123/status",
            }
            mock_video_cls.return_value = mock_client_instance

            response = await client.post("/api/review/j1/video")

        assert response.status_code == 200
        data = response.json()
        assert data["review_job_id"] == "j1"
        assert data["video_job_id"] == "video-123"
        assert data["findings_count"] == 1

    async def test_video_not_found_returns_404(self, app_and_client) -> None:
        """Video endpoint returns 404 for missing job."""
        _app, client = app_and_client

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
        ):
            mock_jm.get_job = AsyncMock(return_value=None)
            mock_gs.return_value = MagicMock(
                video_compiler=MagicMock(enabled=True),
                cache=MagicMock(enabled=False),
            )

            response = await client.post("/api/review/nonexistent/video")

        assert response.status_code == 404

    async def test_video_not_completed_returns_400(self, app_and_client) -> None:
        """Video endpoint returns 400 for non-completed job."""
        _app, client = app_and_client
        job = _make_mock_job(status="processing", results=None)

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                video_compiler=MagicMock(enabled=True),
                cache=MagicMock(enabled=False),
            )

            response = await client.post("/api/review/j1/video")

        assert response.status_code == 400

    async def test_video_disabled_returns_501(self, app_and_client) -> None:
        """Video endpoint returns 501 when disabled (feature not implemented)."""
        _app, client = app_and_client

        with patch("src.domains.review.routes.get_settings") as mock_gs:
            mock_gs.return_value = MagicMock(
                video_compiler=MagicMock(enabled=False),
            )

            response = await client.post("/api/review/j1/video")

        assert response.status_code == 501

    async def test_video_compiler_unavailable_returns_503(self, app_and_client) -> None:
        """Video endpoint returns 503 when video compiler is unreachable."""
        _app, client = app_and_client
        job = _make_mock_job(job_id="j1")

        with (
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.services.findings.extract_findings") as mock_extract,
            patch("src.domains.review.services.video_client.VideoCompilerClient") as mock_video_cls,
        ):
            mock_jm.get_job = AsyncMock(return_value=job)
            mock_gs.return_value = MagicMock(
                video_compiler=MagicMock(enabled=True, base_url="http://test:6309", timeout=300),
                cache=MagicMock(enabled=False),
            )
            mock_extract.return_value = [
                {
                    "review_type": "design_review",
                    "severity": "high",
                    "description": "Issue",
                    "evidence": [],
                    "category": "cat",
                },
            ]
            mock_client_instance = AsyncMock()
            mock_client_instance.request_findings_video.side_effect = ConnectionError(
                "Connection refused"
            )
            mock_video_cls.return_value = mock_client_instance

            response = await client.post("/api/review/j1/video")

        assert response.status_code == 503


# ---------------------------------------------------------------------------
# Tests: Commit-before-dispatch race regression
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommitBeforeDispatch:
    """Regression: the job row must be COMMITTED before the background task is
    dispatched. The task opens its own session and UPDATEs the row; if the row
    is only flushed (committed post-handler), the UPDATE races a not-yet-visible
    INSERT. These spy on the commit await-count at create_task time.
    """

    async def test_single_review_commits_before_dispatch(
        self, app_and_client, mock_db_session
    ) -> None:
        _app, client = app_and_client
        commit_counts: list[int] = []

        def _fake_create_task(coro, *args, **kwargs):
            # coro is the un-awaited _process_review coroutine — close it so the
            # background task never actually runs, then record how many commits
            # had completed at the moment of dispatch.
            if hasattr(coro, "close"):
                coro.close()
            commit_counts.append(mock_db_session.commit.await_count)
            return MagicMock()  # supports add_done_callback + hashable/addable

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch(
                "src.domains.review.routes.asyncio.create_task",
                side_effect=_fake_create_task,
            ),
        ):
            mock_gs.return_value = MagicMock(review=MagicMock(max_pages_per_request=50))
            mock_jm.create_job = AsyncMock(return_value=MagicMock())

            response = await client.post(
                "/api/review",
                json={"analysis": SAMPLE_ANALYSIS, "review_types": ["design_review"]},
            )

        assert response.status_code == 200
        assert commit_counts, "background task was never dispatched"
        assert commit_counts[0] >= 1, "job row was not committed before task dispatch"

    async def test_batch_review_commits_before_dispatch(
        self, app_and_client, mock_db_session
    ) -> None:
        _app, client = app_and_client
        commit_counts: list[int] = []

        def _fake_create_task(coro, *args, **kwargs):
            if hasattr(coro, "close"):
                coro.close()
            commit_counts.append(mock_db_session.commit.await_count)
            return MagicMock()

        with (
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch(
                "src.domains.review.routes.asyncio.create_task",
                side_effect=_fake_create_task,
            ),
        ):
            mock_gs.return_value = MagicMock(
                feature_flags=MagicMock(batch_reviews=True),
                review=MagicMock(max_pages_per_request=50),
                webhook=MagicMock(enabled=False),
                cache=MagicMock(enabled=False),
            )
            mock_jm.create_job = AsyncMock(return_value=MagicMock())
            mock_jm.create_batch = AsyncMock(return_value=MagicMock())

            response = await client.post(
                "/api/review/batch",
                json={
                    "reviews": [{"analysis": SAMPLE_ANALYSIS, "review_types": ["design_review"]}]
                },
            )

        assert response.status_code == 200
        assert commit_counts, "batch background task was never dispatched"
        assert commit_counts[0] >= 1, "job row was not committed before task dispatch"


# ---------------------------------------------------------------------------
# Tests: recovery-path exception handling (SQLAlchemyError, not
# OSError/ValueError/RuntimeError/TimeoutError, is what the DB layer raises)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessReviewRecoveryPathExceptions:
    """``_process_review``'s inner except (routes.py:~276) guards the
    error-recovery DB writes (``mark_failed`` / ``record_usage``). What
    SQLAlchemy actually raises on a DB hiccup is ``sqlalchemy.exc.SQLAlchemyError``
    (e.g. ``OperationalError``) — not ``OSError``/``ValueError``/``RuntimeError``/
    ``TimeoutError``. This proves the handler now catches it (and does not let it
    escape the background task uncaught).
    """

    async def test_operational_error_during_recovery_write_is_caught(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from sqlalchemy.exc import OperationalError

        from src.domains.review.routes import _process_review

        service = MagicMock()
        service.generate_all = AsyncMock(side_effect=ValueError("upstream boom"))

        db_session = AsyncMock()

        @asynccontextmanager
        async def _fake_get_db_session():
            yield db_session

        job_id = "j1"
        tenant_id = _TEST_TENANT
        body = MagicMock(review_types=["design_review"], user_context=None, callback_url=None)

        with (
            patch("src.domains.review.routes.get_db_session", _fake_get_db_session),
            patch("src.domains.review.routes.get_settings") as mock_gs,
            patch("src.domains.review.routes.JobManager") as mock_jm,
            patch("src.domains.review.routes.review_jobs_total"),
            patch("src.domains.review.routes.review_active_jobs"),
            patch(
                "src.domains.review.routes._deliver_review_webhook",
                new_callable=AsyncMock,
            ),
            caplog.at_level(logging.ERROR, logger="src.domains.review.routes"),
        ):
            mock_gs.return_value = MagicMock(feature_flags=MagicMock(sse_progress=False))
            mock_jm.mark_failed = AsyncMock(return_value=None)
            # This is the recovery write that fails: SQLAlchemyError, not one of
            # the caught types.
            mock_jm.record_usage = AsyncMock(
                side_effect=OperationalError("stmt", {}, Exception("db gone"))
            )

            # Behaviour under test: the OperationalError raised by the
            # recovery write must be SWALLOWED, not propagated out of the
            # fire-and-forget background task. Pre-fix (`except OSError,
            # ValueError, RuntimeError, TimeoutError:`) does not catch
            # SQLAlchemyError, so this await would raise and fail the test
            # right here — that is the primary behavioural assertion, not
            # a call count.
            await _process_review(service, job_id, tenant_id, body)

        # And the swallow must not be silent: the recovery failure has to be
        # reported via a real logged exception (with traceback) that
        # identifies the job, or an operator has no way to notice the job is
        # stuck in `processing` forever.
        exception_records = [r for r in caplog.records if r.exc_info is not None]
        assert exception_records, "recovery failure must be logged with exc_info"
        assert any(f"review job {job_id}" in r.getMessage() for r in exception_records)
