"""Tests for DB hardening: model columns and transaction boundaries.

The `results_json` and `analysis_artifacts` columns are part of the initial
combined migration (`0001_initial_combined.py`) — there is no separate 0003
migration. The previous skipif-guarded tests that referenced a phantom
`0003_add_results_json_and_indexes.py` file were stale and have been removed;
column presence is asserted directly against the ORM model below, which is
the actual contract that downstream code depends on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.domains.analyze.models import AnalysisJob
from src.domains.analyze.services.analyzer_service import SiteAnalysisResult

# ---------------------------------------------------------------------------
# 1. Model column presence
# ---------------------------------------------------------------------------


def test_analysis_job_has_analysis_artifacts_column() -> None:
    """AnalysisJob ORM model must expose an analysis_artifacts JSONB column."""
    col = AnalysisJob.__table__.columns.get("analysis_artifacts")
    assert col is not None, "analysis_artifacts column missing from AnalysisJob"
    assert col.nullable is True


def test_analysis_job_has_results_json_column() -> None:
    """AnalysisJob ORM model must expose a results_json JSONB column."""
    col = AnalysisJob.__table__.columns.get("results_json")
    assert col is not None, "results_json column missing from AnalysisJob"
    assert col.nullable is True


# ---------------------------------------------------------------------------
# 2. Transaction rollback on DB failure (cache not corrupted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_rollback_preserves_cache_on_db_failure() -> None:
    """When the DB write fails inside session.begin(), the in-memory cache should still
    reflect the result_data that was set *before* the DB call, and the DB error should
    be logged but not propagated."""

    from src.domains.analyze.routes import _process_analysis

    # Build a minimal app stub with an in-memory cache
    app = MagicMock()
    app.state.analysis_jobs = {"job-tx-1": {"status": "processing"}}
    app.state.cancellation_tokens = {}

    # session.begin() context manager that raises on __aenter__
    # Use MagicMock (not AsyncMock) because session.begin() is a synchronous call
    # in SQLAlchemy that returns an async context manager. AsyncMock would create
    # an unawaited coroutine from .begin(), triggering RuntimeWarning.
    failing_session = MagicMock()
    failing_begin = AsyncMock()
    failing_begin.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("DB down"))
    failing_begin.__aexit__ = AsyncMock(return_value=False)
    failing_session.begin.return_value = failing_begin

    # session_factory as async context manager yielding the failing session
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=failing_session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=session_ctx)
    app.state.db_session_factory = session_factory

    # Build a mock service that returns minimal valid data
    service = MagicMock()
    service.analyze_pages = AsyncMock(return_value=[{"page_id": "p1", "url": "https://x.com"}])
    service.analyze_site = AsyncMock(return_value=SiteAnalysisResult(analysis={"tone": "neutral"}))
    service.extract_narrative.return_value = {}
    service.filter_language_duplicates.return_value = (
        [{"page_id": "p1", "url": "https://x.com"}],
        0,
    )

    # Build a minimal body
    mock_page = MagicMock()
    mock_page.model_dump.return_value = {"page_id": "p1", "url": "https://x.com"}
    body = MagicMock()
    body.pages = [mock_page]
    body.keep_lang = "en"
    body.user_context = None
    body.auto_enrich_context = False
    body.config.enable_language_dedup = False
    body.config.enable_spa_detection = False
    body.config.enable_context_enrichment = False
    body.config.include_narrative = True
    body.config.temperature = None
    body.config.custom_prompts = None

    await _process_analysis(service, "job-tx-1", body, app, "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d")

    # The cache must have been updated to "completed" even though DB failed
    cached = app.state.analysis_jobs["job-tx-1"]
    assert cached["status"] == "completed"


# ---------------------------------------------------------------------------
# 3. Explicit transaction boundaries in routes.py
# ---------------------------------------------------------------------------


def test_routes_use_session_begin_context_manager() -> None:
    """All DB write paths in routes.py must use 'async with session.begin():'
    instead of manual session.commit()."""

    routes_path = Path(__file__).resolve().parents[4] / "src" / "domains" / "analyze" / "routes.py"
    source = routes_path.read_text()

    # There should be NO bare session.commit() calls
    assert "await session.commit()" not in source, (
        "routes.py still contains 'await session.commit()' -- "
        "all DB writes should use 'async with session.begin():' instead"
    )

    # There should be at least 3 uses of session.begin() (create, success, failure)
    begin_count = source.count("session.begin()")
    assert begin_count >= 3, (
        f"Expected at least 3 session.begin() blocks in routes.py, found {begin_count}"
    )
