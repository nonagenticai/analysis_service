"""Real-Postgres tests for the durable analyze review-job store (cross-replica).

A review job written through on one "instance" (its in-memory cache empty on the
polling instance) must still resolve via the Postgres fallback in ``get_review``
— the fix for the create-on-podA / poll-on-podB 404.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.domains.analyze.routes import (
    _persist_review_create,
    _persist_review_result,
    get_review,
)

pytestmark = pytest.mark.asyncio


def _instance(db_session_factory) -> MagicMock:
    """A stand-in FastAPI app/request whose only shared state is the DB factory."""
    app = MagicMock()
    app.state.db_session_factory = db_session_factory
    app.state.review_jobs = {}
    return app


class TestReviewJobStore:
    async def test_review_readable_from_other_instance(self, db_session_factory):
        review_job_id = "rev-cross-1"
        writer = _instance(db_session_factory)

        await _persist_review_create(
            writer,
            review_job_id=review_job_id,
            tenant_id="9ea6a4da-9056-5a5a-bd14-9f92394a7b2c",
            review_types=["seo"],
        )
        await _persist_review_result(
            writer,
            review_job_id=review_job_id,
            tenant_id="9ea6a4da-9056-5a5a-bd14-9f92394a7b2c",
            status="completed",
            reviews={"seo": {"review_type": "seo", "overall_score": 90}},
            processing_time_seconds=1.25,
            llm_calls=3,
        )

        # A *different* instance with an empty in-memory cache polls the job.
        request = MagicMock()
        request.state.tenant_id = "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c"
        request.app = _instance(db_session_factory)  # review_jobs = {} → forces DB fallback

        result = await get_review(review_job_id, request)

        assert result.status == "completed"
        assert result.llm_calls == 3
        assert result.processing_time_seconds == 1.25
        assert "seo" in result.reviews

    async def test_review_is_tenant_scoped(self, db_session_factory):
        review_job_id = "rev-cross-2"
        writer = _instance(db_session_factory)
        await _persist_review_create(
            writer,
            review_job_id=review_job_id,
            tenant_id="29763c73-ce2f-547e-90e1-fa1a09d3a976",
            review_types=["seo"],
        )
        await _persist_review_result(
            writer,
            review_job_id=review_job_id,
            tenant_id="29763c73-ce2f-547e-90e1-fa1a09d3a976",
            status="completed",
            reviews={"seo": {"review_type": "seo"}},
            llm_calls=1,
        )

        # A foreign tenant polling the same id must get a 404 (no cross-tenant read).
        request = MagicMock()
        request.state.tenant_id = "bf7f867f-3cd3-5a4a-bfb4-475d56132c9d"
        request.app = _instance(db_session_factory)

        with pytest.raises(HTTPException) as exc_info:
            await get_review(review_job_id, request)
        assert exc_info.value.status_code == 404
