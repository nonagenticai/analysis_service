"""Real HTTP + real-Postgres ASYNC job-poll integration test for the analyze domain.

Async companion to ``test_review_pipeline_integration.py`` (which drives the
review sub-pipeline): this drives the *primary* analysis pipeline —
``POST /api/analyze`` (create job, commit the initial row, dispatch the detached
``_run_with_timeout``/``_process_analysis``) → poll ``GET /api/analyze/{id}``
until terminal → assert the persisted ``AnalysisJob`` row and tenant-scoped list
isolation.

It exercises two things a strictly-sequential test cannot:

1. **Two DB connections held open at once** — the detached ``_process_analysis``
   task and the poll loop, each a fresh ``NullPool`` connect on the same worker
   schema. This is the class of two-connection flow that made the per-connect
   ``CREATE SCHEMA``/``GRANT`` DDL race the ``pg_namespace`` tuple under xdist
   (``tuple concurrently updated``) and motivated the per-process schema-DDL
   guard in ``tests/conftest.py``. This test simply runs green under
   ``-n4 --reruns 0`` — no rerun masking.
2. **The create-commit ordering** — ``start_analysis`` commits the initial
   ``processing`` row *before* ``asyncio.create_task``; the detached task opens
   its own session (via ``app.state.db_session_factory``) and ``UPDATE``s that
   row to ``completed``, which only matches because of the commit.

Unlike the review flow, the analyze pipeline resolves its background session
straight off ``app.state.db_session_factory`` (there is no module-level
``get_db_session`` on this path), so ``_make_app`` simply points that at the test
factory — no monkeypatch. The ``AnalyzerService`` (the WM/LLM + page-analysis
surface) is stubbed to canned results so the job reaches a terminal state
deterministically with no network, and the optional pipeline stages
(language-dedup / SPA-detection / context-enrichment / narrative) are disabled
via the request ``config`` so only ``analyze_pages`` + ``analyze_site`` (+ the
pure-python ``BrandExtractor``) run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

from src.domains.analyze.models import AnalysisJob
from src.domains.analyze.review_job_models import AnalyzeReviewJob
from src.domains.analyze.routes import router as analyze_router
from src.domains.analyze.services.analyzer_service import SiteAnalysisResult

_TENANT_A = "05779826-72a2-5149-a75c-ffca0de815b7"
_TENANT_B = "54fbf657-0a69-5161-a28a-622c8a83c8c9"
_SITE_URL = "https://acme-analyze-integration.example.com/"


class _StubAnalyzerService:
    """Stands in for ``AnalyzerService`` on ``app.state`` — implements only the
    methods ``_process_analysis`` calls. ``analyze_pages`` echoes the input URLs
    as minimal page dicts (they must round-trip ``PageAnalysisResponse``, so a
    ``url`` is mandatory); ``analyze_site`` returns a canned ``SiteAnalysisResult``
    after a small sleep, which keeps the job in ``processing`` long enough that
    the poll loop and the detached task genuinely hold two connections open at
    once. The remaining methods are only reached when their config flag is on
    (disabled here) but are stubbed defensively."""

    async def analyze_pages(self, pages_input: list[dict], workers: int = 4) -> list[dict]:
        return [
            {"url": p["url"], "title": p.get("title", ""), "page_type": "homepage"}
            for p in pages_input
        ]

    async def analyze_site(
        self, analyzed: list[dict], effective_context: dict | None = None, **_kwargs: object
    ) -> SiteAnalysisResult:
        await asyncio.sleep(0.15)
        return SiteAnalysisResult(analysis={"description": "canned"}, llm_calls=1, artifacts=[])

    def filter_language_duplicates(
        self, analyzed: list[dict], keep_lang: str = "en"
    ) -> tuple[list[dict], int]:
        return analyzed, 0

    def annotate_spa_sections(self, analyzed: list[dict]) -> list[dict]:
        return analyzed

    def extract_narrative(self, analyzed: list[dict]) -> dict:
        return {}


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="analysis-service-analyze-integration-test")

    # The analyze routes + the detached _process_analysis/_persist_* helpers all
    # resolve their session via getattr(app.state, "db_session_factory", None),
    # so pointing it at the test factory is the whole injection — no monkeypatch.
    app.state.db_session_factory = factory
    app.state.analyzer_service = _StubAnalyzerService()

    @app.middleware("http")
    async def _inject_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        tenant = request.headers.get("X-Test-Tenant")
        if tenant:
            request.state.tenant_id = tenant
        return await call_next(request)

    app.include_router(analyze_router)
    return app


def _analyze_payload() -> dict[str, object]:
    return {
        "pages": [{"url": _SITE_URL, "title": "Acme analyze home"}],
        "config": {
            "enable_language_dedup": False,
            "enable_spa_detection": False,
            "enable_context_enrichment": False,
            "include_narrative": False,
        },
    }


async def _poll_until_terminal(
    client: AsyncClient, job_id: str, tenant: str, timeout_s: float = 8.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/analyze/{job_id}", headers={"X-Test-Tenant": tenant})
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") in {"completed", "failed"}:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach a terminal state in {timeout_s}s: {last}")


async def test_analyze_pipeline_completes_and_is_tenant_isolated(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST analyze → detached task completes it → poll sees completed →
    persisted row + list isolation, all under real HTTP + real Postgres."""
    app = _make_app(db_session_factory)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Start an analysis job for tenant A (async → returns a job_id).
        start = await client.post(
            "/api/analyze", json=_analyze_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        # 2. Poll until the detached _process_analysis marks it terminal.
        final = await _poll_until_terminal(client, job_id, _TENANT_A)
        assert final["status"] == "completed", final
        assert final["site_analysis"]["description"] == "canned"

        # 3. Direct DB assertion: the row is persisted, tenant-scoped to A,
        #    completed, with the canned site analysis the detached task wrote.
        #    The poll's terminal signal comes from the in-memory job cache, which
        #    the background task sets around the same time it commits the DB row;
        #    retry the DB read briefly so this assertion never races that
        #    write-through under a slow/contended CI Postgres.
        row = None
        for _ in range(50):
            async with db_session_factory() as verify:
                row = (
                    await verify.execute(select(AnalysisJob).where(AnalysisJob.job_id == job_id))
                ).scalar_one_or_none()
            if row is not None and row.status == "completed":
                break
            await asyncio.sleep(0.1)
        assert row is not None, "analysis job row was never persisted"
        assert row.tenant_id == _TENANT_A
        assert row.status == "completed"
        assert row.site_analysis is not None
        assert row.site_analysis["description"] == "canned"

        # 4. App-level list scoping: list_analysis_jobs filters on tenant_id, so
        #    tenant B sees none of tenant A's jobs and tenant A sees its own.
        list_b = await client.get("/api/analyze", headers={"X-Test-Tenant": _TENANT_B})
        assert list_b.status_code == 200, list_b.text
        assert all(j["job_id"] != job_id for j in list_b.json()["jobs"])

        list_a = await client.get("/api/analyze", headers={"X-Test-Tenant": _TENANT_A})
        assert list_a.status_code == 200, list_a.text
        assert any(j["job_id"] == job_id for j in list_a.json()["jobs"])


async def test_single_analyze_get_is_tenant_isolated_across_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant A creates + completes an analyze job, then tenant B's single
    ``GET /api/analyze/{id}`` returns 404.

    This asserts the APP-LEVEL repo ``tenant_id`` filter, NOT Postgres RLS:
    ``get_analysis`` reads straight off ``app.state.db_session_factory`` (it does
    NOT use ``Depends(get_db)``), so no restricted role / GUC is in play. The
    404 is produced by ``AnalysisJobRepository.get(session, job_id, tenant_id=B)``
    returning None because of its explicit ``WHERE tenant_id`` clause.

    The in-memory ``app.state.analysis_jobs`` cache the background task populates
    for tenant A cannot short-circuit this: ``get_analysis`` only honours a
    cached entry when its ``tenant_id`` matches the caller (or is None), so a
    tenant-B request always falls through past the in-memory + Redis caches to
    the tenant-scoped DB lookup. ``CACHE_ENABLED`` defaults to ``false`` in the
    conftest, so the Redis path is inert here regardless.
    """
    app = _make_app(db_session_factory)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Arrange: create + complete an analyze job for tenant A.
        start = await client.post(
            "/api/analyze", json=_analyze_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        final = await _poll_until_terminal(client, job_id, _TENANT_A)
        assert final["status"] == "completed", final

        # Tenant A can read its own job (sanity: the row/caches are readable).
        get_a = await client.get(f"/api/analyze/{job_id}", headers={"X-Test-Tenant": _TENANT_A})
        assert get_a.status_code == 200, get_a.text

        # Tenant B is refused: the repo's tenant_id filter yields None → 404.
        get_b = await client.get(f"/api/analyze/{job_id}", headers={"X-Test-Tenant": _TENANT_B})
        assert get_b.status_code == 404, get_b.text

        # Prove the 404 was the tenant filter, not deletion: the row still exists,
        # tenant-scoped to A, when read directly (no tenant predicate).
        async with db_session_factory() as verify:
            row = (
                await verify.execute(select(AnalysisJob).where(AnalysisJob.job_id == job_id))
            ).scalar_one()
            assert row.tenant_id == _TENANT_A
            assert row.status == "completed"


class _StubReviewGenerator:
    """Stands in for ``ReviewGenerator`` so ``_process_review`` reaches a terminal
    state with NO worker_management / LLM call.

    ``_process_review`` imports the class *function-locally* from
    ``src.domains.analyze.services.review_generator``, so the monkeypatch must
    target that SOURCE module (patching the ``routes`` namespace would miss it).
    The return shape mirrors the real ``generate_reviews`` — a
    ``{review_type: {...}}`` map whose values each carry an ``llm_calls`` int
    (summed into the completion record by ``_process_review``) and a
    ``review_type`` key (coerced into ``ReviewResult`` on the GET response; the
    ``result``/``artifacts`` extras are ignored by pydantic exactly as the real
    payload's extras are)."""

    def __init__(self, *args: object, **kwargs: object) -> None: ...

    async def generate_reviews(
        self,
        *,
        review_types: list[str],
        analysis_result: dict | None = None,
        user_context: dict | None = None,
        job_id: str = "",
        llm_config: object | None = None,
    ) -> dict[str, dict]:
        return {
            rt: {"review_type": rt, "result": {"score": 90}, "llm_calls": 0, "artifacts": []}
            for rt in review_types
        }


async def test_single_review_get_is_tenant_isolated_across_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A creates + completes an analyze *review* sub-job, then tenant B's
    single ``GET /api/analyze/review/{id}`` returns 404 — closing the last
    un-asserted single-GET isolation gap in the analyze domain.

    This asserts the APP-LEVEL repo ``tenant_id`` filter, NOT Postgres RLS:
    ``get_review`` reads straight off ``app.state.db_session_factory`` (it does
    NOT use ``Depends(get_db)``, so no restricted role / GUC is in play). The 404
    is produced by ``AnalyzeReviewJobRepository.get(session, review_job_id,
    tenant_id=B)`` returning None because of its explicit ``WHERE tenant_id``
    clause.

    The in-memory ``app.state.review_jobs`` fast path the background task
    populates for tenant A cannot short-circuit this: ``get_review`` only honours
    a cached entry when its ``tenant_id`` matches the caller (or is None), so a
    tenant-B request always falls through past the in-mem + Redis caches to the
    tenant-scoped DB lookup. ``CACHE_ENABLED`` defaults to ``false`` in the
    conftest, so the Redis path is inert here regardless.
    """
    # No WM/LLM: stub the review generator at its SOURCE module (function-local
    # import inside _process_review), so completion is synchronous + deterministic.
    monkeypatch.setattr(
        "src.domains.analyze.services.review_generator.ReviewGenerator",
        _StubReviewGenerator,
    )

    app = _make_app(db_session_factory)
    # start_review resolves the parent analyze job from app.state.analysis_jobs
    # (in-mem) FIRST, honouring it only for the owning tenant — so a seeded,
    # already-completed parent for tenant A is the minimal reliable arrange; no
    # second analyze-pipeline run is needed to produce the review's prerequisite.
    # review_jobs is the write-through in-mem fast path the background task + GET
    # share; seeding it empty (rather than leaving it unset, which hands out a
    # throwaway dict per access) exercises its tenant gate so tenant B provably
    # falls through it to the tenant-scoped DB lookup.
    parent_id = "analyze-parent-job-for-review-isolation"
    app.state.analysis_jobs = {
        parent_id: {
            "status": "completed",
            "tenant_id": _TENANT_A,
            "pages": [],
            "site_analysis": {"description": "canned"},
            "narrative_extraction": {},
        }
    }
    app.state.review_jobs = {}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Tenant A starts a review of its completed parent analyze job.
        start = await client.post(
            "/api/analyze/review",
            json={"analysis_job_id": parent_id, "review_types": ["design"]},
            headers={"X-Test-Tenant": _TENANT_A},
        )
        assert start.status_code == 200, start.text
        review_id = start.json()["review_job_id"]
        assert review_id

        # Poll the single-GET until the detached _process_review marks it terminal
        # (path differs from _poll_until_terminal's /api/analyze/{id}).
        final: dict = {}
        deadline = asyncio.get_event_loop().time() + 8.0
        while asyncio.get_event_loop().time() < deadline:
            resp = await client.get(
                f"/api/analyze/review/{review_id}", headers={"X-Test-Tenant": _TENANT_A}
            )
            assert resp.status_code == 200, resp.text
            final = resp.json()
            if final.get("status") in {"completed", "failed"}:
                break
            await asyncio.sleep(0.1)
        assert final.get("status") == "completed", final

        # Wait for the durable write-through (_process_review sets the in-mem
        # completed state BEFORE it commits the DB row) so the tenant-B 404 below
        # is provably the tenant filter — a committed row that B cannot see — and
        # not merely a not-yet-persisted row.
        row = None
        for _ in range(50):
            async with db_session_factory() as verify:
                row = (
                    await verify.execute(
                        select(AnalyzeReviewJob).where(AnalyzeReviewJob.review_job_id == review_id)
                    )
                ).scalar_one_or_none()
            if row is not None and row.status == "completed":
                break
            await asyncio.sleep(0.1)
        assert row is not None, "review job row was never persisted"
        assert row.tenant_id == _TENANT_A
        assert row.status == "completed"

        # Tenant A can read its own review job (sanity: the row/caches are readable).
        get_a = await client.get(
            f"/api/analyze/review/{review_id}", headers={"X-Test-Tenant": _TENANT_A}
        )
        assert get_a.status_code == 200, get_a.text

        # Tenant B is refused: the app-level AnalyzeReviewJobRepository.get(tenant_id=...)
        # filter yields None → 404 (NOT RLS). The row still exists, tenant-scoped
        # to A, as the direct read above proved.
        get_b = await client.get(
            f"/api/analyze/review/{review_id}", headers={"X-Test-Tenant": _TENANT_B}
        )
        assert get_b.status_code == 404, get_b.text
