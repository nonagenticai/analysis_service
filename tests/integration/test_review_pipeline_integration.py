"""Real HTTP + real-Postgres ASYNC job-poll integration test for the review domain.

Higher-fidelity companion to ``test_brand_extract_pipeline_integration.py`` (a
strictly-sequential flow): this drives the actual *async* review pipeline —
``POST /api/review`` (create job, commit, dispatch the detached
``_process_review``) → poll ``GET /api/review/{id}`` until terminal → assert the
persisted ``ReviewJob`` row and tenant-scoped list isolation.

It exercises two things the sequential brand test cannot:

1. **Two DB connections held open at once** — the detached ``_process_review``
   task and the poll loop, each a fresh ``NullPool`` connect on the same worker
   schema. This is the class of two-connection flow that made the per-connect
   ``CREATE SCHEMA``/``GRANT`` DDL race the ``pg_namespace`` tuple under xdist
   (``tuple concurrently updated``) and motivated the per-process schema-DDL
   guard in ``tests/conftest.py``; the deterministic regression for that guard
   is ``tests/unit/test_conftest_schema_isolation.py``. This test simply runs
   green under ``-n4 --reruns 0`` — no rerun masking.
2. **The create-commit ordering** — ``start_review`` commits the job row
   *before* ``asyncio.create_task``; the detached task opens its own session and
   ``UPDATE``s that row via ``mark_completed``, which only matches because of the
   commit (the fix in #124). Point the background session at a different schema
   and this test hangs in ``processing`` — proving the ordering end-to-end.

``_process_review`` resolves its own session via the app's ``get_db_session()``;
under xdist that engine targets a different schema than the test engine, so the
``_patch_background_session`` fixture points ``get_db_session`` at the test
factory — both then share the per-worker schema *and* the conftest ``connect``
listener, which is what makes point (1) a real concurrent-connect test. The
WM/LLM call (``ReviewService.generate_all``) is stubbed to canned results so the
job reaches a terminal state deterministically with no network.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

import src.domains.review.routes as review_routes
from src.domains.review.models import ReviewJob
from src.domains.review.routes import router as review_router
from src.shared.database.session import get_db

_TENANT_A = "f3981f12-76dc-524f-adbe-d856825ab8c0"
_TENANT_B = "00dfc1fb-5110-5bbb-a853-dc162e3a5315"
_SITE_URL = "https://acme-review-integration.example.com/"
_CANNED_RESULTS: dict[str, object] = {
    "design": {"summary": "canned design review", "score": 0.9},
    "_token_usage": {"total_tokens": 7},
}


class _StubReviewService:
    """Stands in for ``ReviewService`` on ``app.state`` — the only thing the
    route touches is ``generate_all`` (the WM/LLM gateway call). A small sleep
    keeps the job in ``processing`` long enough that the poll loop and the
    detached task genuinely hold two connections open at once."""

    async def generate_all(self, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.15)
        return dict(_CANNED_RESULTS)


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="analysis-service-review-integration-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db
    app.state.review_service = _StubReviewService()

    @app.middleware("http")
    async def _inject_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        tenant = request.headers.get("X-Test-Tenant")
        if tenant:
            request.state.tenant_id = tenant
        return await call_next(request)

    app.include_router(review_router)
    return app


@pytest.fixture
def _patch_background_session(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Point the app's ``get_db_session`` (used by the detached ``_process_review``)
    at the test factory so the background task and the poll loop share the same
    per-worker schema — otherwise the task would ``UPDATE`` a row in a different
    schema and the poll would never see ``completed``."""

    @contextlib.asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(review_routes, "get_db_session", _fake_get_db_session)


def _review_payload() -> dict[str, object]:
    return {
        "analysis": {"pages": [{"url": _SITE_URL, "title": "Acme review home"}]},
        "review_types": ["design"],
    }


async def _poll_until_terminal(
    client: AsyncClient, job_id: str, tenant: str, timeout_s: float = 8.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/review/{job_id}", headers={"X-Test-Tenant": tenant})
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") in {"completed", "failed"}:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach a terminal state in {timeout_s}s: {last}")


async def test_review_pipeline_completes_and_is_tenant_isolated(
    db_session_factory: async_sessionmaker[AsyncSession],
    _patch_background_session: None,
) -> None:
    """POST review → detached task completes it → poll sees completed →
    persisted row + list isolation, all under real HTTP + real Postgres."""
    app = _make_app(db_session_factory)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Start a review job for tenant A (async → returns a job_id).
        start = await client.post(
            "/api/review", json=_review_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        # 2. Poll until the detached _process_review marks it terminal.
        final = await _poll_until_terminal(client, job_id, _TENANT_A)
        assert final["status"] == "completed", final
        assert final["results"]["design"]["summary"] == "canned design review"

        # 3. Direct DB assertion: the row is persisted, tenant-scoped to A,
        #    completed, with the canned results the detached task wrote.
        async with db_session_factory() as verify:
            row = (
                await verify.execute(select(ReviewJob).where(ReviewJob.job_id == job_id))
            ).scalar_one()
            assert row.tenant_id == _TENANT_A
            assert row.status == "completed"
            assert row.results is not None
            assert row.results["design"]["score"] == 0.9

        # 4. App-level list scoping: the list route filters on
        #    ``list_jobs(..., tenant_id=...)`` (WHERE tenant_id), so tenant B
        #    sees none of tenant A's jobs and tenant A sees its own.
        #    (The single-job ``GET /api/review/{id}`` is app-scoped too now —
        #    ``JobManager.get_job`` takes a required ``tenant_id``. Its paired
        #    200/404 cases live in
        #    ``tests/unit/domains/review/test_route_tenant_scoping.py``; what
        #    this test asserts here is the list filter.)
        list_b = await client.get("/api/review", headers={"X-Test-Tenant": _TENANT_B})
        assert list_b.status_code == 200, list_b.text
        assert all(j["job_id"] != job_id for j in list_b.json()["jobs"])

        list_a = await client.get("/api/review", headers={"X-Test-Tenant": _TENANT_A})
        assert list_a.status_code == 200, list_a.text
        assert any(j["job_id"] == job_id for j in list_a.json()["jobs"])
