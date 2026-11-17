"""Postgres-RLS-ENFORCE tenant-isolation test for the single-job review GET.

The async review-pipeline test
(``tests/integration/test_review_pipeline_integration.py:170-178``) explicitly
documents a gap: the single-job ``GET /api/review/{id}`` handler has NO
app-level tenant filter — it relies on the Phase D RLS ``tenant_isolation``
policy to hide another tenant's row — and the bare FastAPI test apps those
tests spin up connect as the owner/superuser DB role, which BYPASSes RLS. So
that test can only assert the *list* route's ``WHERE tenant_id`` filter, not the
production boundary that actually protects the single-GET.

This test closes that gap. It runs the two assertion GETs through an app whose
per-request DB session ``SET LOCAL ROLE``s to a throwaway ``NOBYPASSRLS``
non-owner role (the posture the runtime ``w2v_analysis_app`` role has under
``TENANT_RLS_MODE=enforce``) and binds ``app.current_tenant`` via
``set_config(..., is_local => true)`` from the identity ContextVar. Under that
role the shipped policy — applied by ``register_rls_ddl`` on ``create_all`` in
the ``db_engine`` fixture — makes tenant A's completed review job:

* **visible** to tenant A  → ``GET /api/review/{id}`` returns 200 + results, and
* **invisible** to tenant B → the same GET returns 404, because
  ``JobManager.get_job`` sees zero rows (RLS), NOT because of any app code.

The job is *arranged* through a separate OWNER app (RLS-bypassing), and its
detached ``_process_review`` background task keeps writing as the owner (via
``_patch_background_session``) so the completing UPDATE always matches and the
job never hangs in ``processing``. Only the two assertion GETs are enforced.
A final owner-session SELECT proves the row still exists — so the 404 is
RLS-invisibility, not deletion.

Skips loudly where the test DB user cannot ``CREATE ROLE`` (CI/hook role lacks
superuser/CREATEROLE), exactly like ``tests/test_rls_isolation.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response
from w2v_common.auth import (
    clear_identity_context,
    get_tenant_id_from_context,
    set_identity_context,
)

import src.domains.review.routes as review_routes
from src.domains.review.models import ReviewJob
from src.domains.review.routes import router as review_router
from src.shared.database.session import get_db

_TENANT_A = "b7bd9f3a-9e6e-5626-aaa6-ec88f964a94f"
_TENANT_B = "1612ea95-5759-594b-bd8b-871d800abf34"
_SITE_URL = "https://acme-rls-integration.example.com/"
_CANNED_RESULTS: dict[str, object] = {
    "design": {"summary": "canned design review", "score": 0.9},
    "_token_usage": {"total_tokens": 7},
}


# ---------------------------------------------------------------------------
# Helpers copied verbatim from test_review_pipeline_integration.py
# ---------------------------------------------------------------------------


class _StubReviewService:
    """Stands in for ``ReviewService`` on ``app.state`` — the only thing the
    route touches is ``generate_all`` (the WM/LLM gateway call). A small sleep
    keeps the job in ``processing`` long enough that the poll loop and the
    detached task genuinely hold two connections open at once."""

    async def generate_all(self, **_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.15)
        return dict(_CANNED_RESULTS)


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


@pytest.fixture
def _patch_background_session(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Point the app's ``get_db_session`` (used by the detached ``_process_review``)
    at the test factory so the background task and the poll loop share the same
    per-worker schema — otherwise the task would ``UPDATE`` a row in a different
    schema and the poll would never see ``completed``. The background session is
    deliberately left as the OWNER session (no role/GUC) so the completing
    UPDATE always matches under RLS-bypass and the job never hangs."""

    @contextlib.asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(review_routes, "get_db_session", _fake_get_db_session)


# ---------------------------------------------------------------------------
# Owner app (RLS-bypass) — verbatim clone of the template's _make_app, used
# only to ARRANGE the completed job.
# ---------------------------------------------------------------------------


def _make_owner_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="analysis-service-rls-owner-app")

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


# ---------------------------------------------------------------------------
# Enforcing app (SET LOCAL ROLE + app.current_tenant GUC) — used ONLY for the
# two assertion GETs. This is the app that actually exercises the RLS boundary.
# ---------------------------------------------------------------------------


def _make_rls_app(factory: async_sessionmaker[AsyncSession], role: str) -> FastAPI:
    app = FastAPI(title="analysis-service-rls-enforce-app")
    # ``role`` is a trusted per-worker constant, but it is an SQL identifier and
    # cannot be a bind parameter — quote it as an identifier, never interpolate
    # user input here (same posture as tests/test_rls_isolation.py).
    set_role_stmt = text(f'SET LOCAL ROLE "{role}"')

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            # Transaction-scoped, in this exact order:
            # 1. drop to the restricted NOBYPASSRLS role so RLS is enforced,
            # 2. read the tenant bound onto the ContextVar by the middleware,
            # 3. bind app.current_tenant for the RLS policy (is_local => true),
            # 4. hand the session to the route, then commit.
            await session.execute(set_role_stmt)
            # w2v_common 0.67.0: this ContextVar is a ``uuid.UUID | None``.
            # ``str(...)`` is load-bearing — psycopg adapts a bare UUID to the
            # postgres ``uuid`` type and ``set_config(text, text, bool)`` has no
            # such overload, so the bind dies with "function set_config(unknown,
            # uuid, boolean) does not exist". The GUC is text and the tenant
            # columns are varchar, so the tenant must cross as text.
            #
            # An absent tenant is NOT bound to "": that is the blank-tenant
            # substitution 0.67.0 removed, and under RLS it would silently widen
            # this assertion from "isolated" to "matched nothing". The route is
            # given an unbound session instead, which fails closed.
            tenant = get_tenant_id_from_context()
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.current_tenant', :t, true)"),
                    {"t": str(tenant)},
                )
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
            # override_db resolves the tenant from the identity ContextVar, so it
            # MUST be set before call_next. Cleared in finally so the ContextVar
            # never leaks across requests/tests under ``-n4``.
            set_identity_context(tenant, None, None)
        try:
            return await call_next(request)
        finally:
            clear_identity_context()

    app.include_router(review_router)
    return app


# ---------------------------------------------------------------------------
# Restricted-role fixture (skip-gated) — mirrors tests/test_rls_isolation.py's
# role lifecycle, but with a per-worker name so it does not collide with that
# test's fixed role name under ``-n4``.
# ---------------------------------------------------------------------------


@pytest.fixture
async def restricted_get_role(db_engine) -> AsyncIterator[str]:
    role = f"w2v_analysis_rls_get_{os.environ.get('PYTEST_XDIST_WORKER', 'gw')}"
    created = False
    try:
        async with db_engine.begin() as conn:
            try:
                await conn.execute(text(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOINHERIT'))
            except ProgrammingError as exc:  # CI/hook user lacks CREATEROLE
                pytest.skip(f"cannot CREATE ROLE (needs superuser/CREATEROLE): {exc.orig}")
            created = True
            # ``review_jobs`` resolves via the per-worker search_path set by the
            # conftest ``connect`` listener; USAGE on the schema is already
            # granted TO PUBLIC there.
            await conn.execute(
                text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON review_jobs TO "{role}"')
            )
        yield role
    finally:
        if created:
            async with db_engine.begin() as conn:
                await conn.execute(text("RESET ROLE"))
                await conn.execute(text(f'DROP OWNED BY "{role}"'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_single_review_get_is_rls_isolated_across_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    _patch_background_session: None,
    restricted_get_role: str,
) -> None:
    """Tenant A creates + completes a review job (owner app, RLS-bypass). Then,
    through the ENFORCING app (restricted role + tenant GUC), tenant A's single
    ``GET /api/review/{id}`` returns 200 while tenant B's returns 404 — the row
    is invisible under RLS, not filtered by app code. Row still exists after."""
    # 1. ARRANGE: create + complete the job for tenant A via the owner app.
    owner_app = _make_owner_app(db_session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=owner_app), base_url="http://test"
    ) as owner_client:
        start = await owner_client.post(
            "/api/review", json=_review_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        final = await _poll_until_terminal(owner_client, job_id, _TENANT_A)
        assert final["status"] == "completed", final
        assert final["results"]["design"]["summary"] == "canned design review"

    # 2. ACT / ASSERT: the two single-GETs run under the ENFORCING app.
    rls_app = _make_rls_app(db_session_factory, restricted_get_role)
    async with AsyncClient(
        transport=ASGITransport(app=rls_app), base_url="http://test"
    ) as rls_client:
        # Tenant A: the RLS policy makes A's own row visible → 200 + results.
        resp_a = await rls_client.get(f"/api/review/{job_id}", headers={"X-Test-Tenant": _TENANT_A})
        assert resp_a.status_code == 200, resp_a.text
        body_a = resp_a.json()
        assert body_a["status"] == "completed", body_a
        assert body_a["results"]["design"]["summary"] == "canned design review"

        # Tenant B: the row is INVISIBLE under RLS → get_job sees nothing → 404.
        resp_b = await rls_client.get(f"/api/review/{job_id}", headers={"X-Test-Tenant": _TENANT_B})
        assert resp_b.status_code == 404, resp_b.text

    # 3. Prove the 404 was RLS-invisibility, not deletion: the owner (RLS-bypass)
    #    session still sees the row, tenant-scoped to A.
    async with db_session_factory() as verify:
        row = (
            await verify.execute(select(ReviewJob).where(ReviewJob.job_id == job_id))
        ).scalar_one()
        assert row.tenant_id == _TENANT_A
        assert row.status == "completed"
