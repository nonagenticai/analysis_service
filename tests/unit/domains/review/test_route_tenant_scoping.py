"""Every review by-id route must resolve the job WITHIN the caller's tenant.

Same contract as the critique twin: each case is a PAIR on ONE seeded row --
tenant A gets a 200 with an asserted body field, tenant B gets a 404 whose body
is byte-identical to the genuine not-found body. A 403 is explicitly wrong: it
would be a cross-tenant existence oracle over uuid7 ids that encode creation
time.

The Redis result cache gets its own case, because it is read BEFORE the DB and
Redis has no RLS backstop: the key itself must carry the tenant. That test also
asserts the OWNER still gets a cache HIT (via the ``review_cache_hits``
counter) -- otherwise "tenant B got a 404" could just mean the fixture had the
cache switched off and proved nothing about the key.

These tests connect as the table OWNER, so Postgres RLS is inert by design;
they prove the APP-level filter. The RLS proof is
``tests/integration/test_rls_tenant_isolation_integration.py``.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

import src.domains.review.routes as review_routes
from src.domains.review.models import ReviewBatch, ReviewJob
from src.domains.review.routes import router as review_router
from src.shared.database.session import get_db

pytestmark = pytest.mark.unit

_TENANT_A = "08d9e193-c944-57f1-a88d-77f4db6c1f3b"
_TENANT_B = "f5384426-2439-535f-a4db-c2e63834af83"

_RESULTS: dict[str, Any] = {
    "design_review": {
        "overall_score": 5.5,
        "categories": {
            "layout_consistency": {"score": 5, "observations": ["Grid drifts on mobile"]},
        },
        "strengths": ["Strong typography"],
        "weaknesses": ["Inconsistent spacing"],
        "recommendations": ["Normalise the spacing scale"],
    },
}


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="review-tenant-scoping-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db
    app.state.review_service = MagicMock()

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


async def _seed_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    status: str = "completed",
) -> str:
    job_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            ReviewJob(
                job_id=job_id,
                tenant_id=tenant_id,
                status=status,
                review_types=["design_review"],
                results=_RESULTS if status == "completed" else None,
                url="https://scoped-review.example.com/",
                domain="scoped-review.example.com",
            )
        )
        await session.commit()
    return job_id


def _h(tenant: str) -> dict[str, str]:
    return {"X-Test-Tenant": tenant}


async def _get(client: AsyncClient, path: str, tenant: str):
    return await client.get(path, headers=_h(tenant))


def _assert(cond: object) -> None:
    assert cond


async def _delete_row(
    factory: async_sessionmaker[AsyncSession],
    row_id: str,
    model: type = ReviewJob,
    pk: str = "job_id",
) -> None:
    async with factory() as session:
        await session.execute(sa_delete(model).where(getattr(model, pk) == row_id))
        await session.commit()


async def _assert_indistinguishable_from_missing(
    factory: async_sessionmaker[AsyncSession],
    send: Callable[[], Awaitable[Any]],
    row_id: str,
    model: type = ReviewJob,
    pk: str = "job_id",
) -> None:
    """The stranger's 404 must be byte-identical to a genuinely-missing 404.

    The same id is used for both halves --- ask as the stranger while the row
    exists, delete the row, ask again --- because some of these bodies echo the
    id the caller supplied, which the caller already knows. Any byte of
    difference between the two responses is an existence oracle over uuid7 ids
    that encode creation time.
    """
    while_exists = await send()
    assert while_exists.status_code == 404, (
        f"cross-tenant read returned {while_exists.status_code}, expected 404 "
        "(403 would itself be the oracle; 400 would mean the status branch ran "
        "before the tenant-scoped lookup)"
    )

    await _delete_row(factory, row_id, model, pk)

    now_missing = await send()
    assert now_missing.status_code == 404
    assert while_exists.content == now_missing.content, (
        "cross-tenant 404 body differs from the genuine not-found body -- oracle"
    )


_ROUTES: list[tuple[str, str, Callable[[Any, str], None]]] = [
    (
        "get_review",
        "/api/review/{jid}",
        lambda body, jid: _assert(
            body["job_id"] == jid and body["results"]["design_review"]["overall_score"] == 5.5
        ),
    ),
    (
        "get_review_summary",
        "/api/review/{jid}/summary",
        lambda body, jid: _assert(body["job_id"] == jid and body["aggregate_score"] is not None),
    ),
    (
        "get_review_findings",
        "/api/review/{jid}/findings",
        lambda body, jid: _assert(body["job_id"] == jid and body["total"] > 0),
    ),
    (
        "get_review_report",
        "/api/review/{jid}/report",
        lambda body, jid: _assert(body["job_id"] == jid and body["format"] == "markdown"),
    ),
]


@pytest.mark.parametrize(("name", "template", "check"), _ROUTES, ids=[r[0] for r in _ROUTES])
async def test_by_id_route_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
    name: str,
    template: str,
    check: Callable[[Any, str], None],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, template.format(jid=job_id), _TENANT_A)
        assert ok.status_code == 200, f"{name}: owner was denied: {ok.text}"
        check(ok.json(), job_id)

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, template.format(jid=job_id), _TENANT_B),
            job_id,
        )


async def test_video_request_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    fake_client = AsyncMock()
    fake_client.request_findings_video = AsyncMock(
        return_value={"job_id": "vid-1", "poll_url": "/api/videos/vid-1"}
    )
    fake_client.close = AsyncMock()

    with (
        patch(
            "src.domains.review.services.video_client.VideoCompilerClient",
            return_value=fake_client,
        ),
        patch("w2v_common.discovery.service_url", return_value="http://video.test.invalid"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await client.post(f"/api/review/{job_id}/video", headers=_h(_TENANT_A))
            assert ok.status_code == 200, ok.text
            assert ok.json()["video_job_id"] == "vid-1"

            await _assert_indistinguishable_from_missing(
                db_session_factory,
                lambda: client.post(f"/api/review/{job_id}/video", headers=_h(_TENANT_B)),
                job_id,
            )


async def test_progress_sse_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE generator runs on a fresh session AFTER the request scope.

    It must carry a tenant captured into a local before the generator was
    defined -- never reach into ``request.state`` from inside it.
    """

    @contextlib.asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(review_routes, "get_db_session", _fake_get_db_session)

    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/review/{job_id}/progress", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert '"status": "completed"' in ok.text

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/review/{job_id}/progress", _TENANT_B),
            job_id,
        )


async def test_compare_scopes_both_jobs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    a1 = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    a2 = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    b1 = await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/review/compare?job_id_1={a1}&job_id_2={a2}", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert ok.json()["job_id_1"] == a1

        # First slot is a foreign job.
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/review/compare?job_id_1={b1}&job_id_2={a2}", _TENANT_A),
            b1,
        )

    # Second slot is a foreign job (fresh row: the first half deleted b1).
    b2 = await _seed_job(db_session_factory, tenant_id=_TENANT_B)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/review/compare?job_id_1={a1}&job_id_2={b2}", _TENANT_A),
            b2,
        )


async def test_mismatch_on_incomplete_job_is_404_not_400(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Check-ordering guard: the tenant filter must precede the status branch."""
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A, status="processing")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _get(client, f"/api/review/{job_id}/summary", _TENANT_A)
        assert owner.status_code == 400, "owner should see the real 400 for a processing job"

        stranger = await _get(client, f"/api/review/{job_id}/summary", _TENANT_B)
        assert stranger.status_code == 404, (
            f"status leaked: stranger got {stranger.status_code} on another tenant's job"
        )
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/review/{job_id}/summary", _TENANT_B),
            job_id,
        )


# ---------------------------------------------------------------------------
# The Redis result cache -- read BEFORE the DB, with no RLS backstop.
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Dict-backed stand-in exposing only the two calls review_cache uses."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    async def get(self, key: str) -> dict | None:
        return self.store.get(key)

    async def set(self, key: str, value: dict, ttl: int | None = None) -> bool:
        self.store[key] = value
        return True


async def test_review_cache_key_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A's cached result must not be served to tenant B.

    The counter assertion is load-bearing: without it, "tenant B got 404" is
    also what you would see if the cache were simply disabled in the fixture,
    so the test would pass for the wrong reason.
    """
    from src.config import clear_settings_cache

    monkeypatch.setenv("CACHE_ENABLED", "true")
    clear_settings_cache()

    from src.domains.review.services.review_cache import set_cached_result
    from src.metrics import review_cache_hits

    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    fake = _FakeRedis()
    with patch(
        "src.domains.review.services.review_cache.get_redis_client",
        return_value=fake,
    ):
        # Seed the cache the way the completion path does -- under tenant A.
        await set_cached_result(
            _TENANT_A,
            job_id,
            {"job_id": job_id, "status": "completed", "results": _RESULTS, "error": None},
        )
        assert any(_TENANT_A in k for k in fake.store), (
            f"cache key does not carry the tenant: {list(fake.store)}"
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            before = review_cache_hits._value.get()

            ok = await _get(client, f"/api/review/{job_id}", _TENANT_A)
            assert ok.status_code == 200, ok.text
            assert ok.json()["results"]["design_review"]["overall_score"] == 5.5

            after = review_cache_hits._value.get()
            assert after == before + 1, (
                "owner did not get a CACHE hit -- the cache was inert, so this "
                "test proves nothing about the key"
            )

            # Tenant B: no key of its own, and the DB lookup is scoped -> 404.
            stranger = await _get(client, f"/api/review/{job_id}", _TENANT_B)
            assert stranger.status_code == 404, (
                f"tenant B read tenant A's cached review ({stranger.status_code})"
            )
            assert review_cache_hits._value.get() == after, (
                "tenant B scored a cache hit on tenant A's key"
            )

            await _assert_indistinguishable_from_missing(
                db_session_factory,
                lambda: _get(client, f"/api/review/{job_id}", _TENANT_B),
                job_id,
            )


async def _seed_batch(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: str, job_ids: list[str]
) -> str:
    batch_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            ReviewBatch(
                batch_id=batch_id,
                tenant_id=tenant_id,
                job_ids=job_ids,
                total=len(job_ids),
                status="processing",
            )
        )
        await session.commit()
    return batch_id


async def test_batch_status_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The batch body carries status, totals AND the full job_ids list."""
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    batch_id = await _seed_batch(db_session_factory, tenant_id=_TENANT_A, job_ids=[job_id])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/review/batch/{batch_id}", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert ok.json()["batch_id"] == batch_id
        assert ok.json()["job_ids"] == [job_id]

        stranger = await _get(client, f"/api/review/batch/{batch_id}", _TENANT_B)
        assert stranger.status_code == 404, (
            f"cross-tenant batch read returned {stranger.status_code} and leaked: {stranger.text}"
        )

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/review/batch/{batch_id}", _TENANT_B),
            batch_id,
            model=ReviewBatch,
            pk="batch_id",
        )


# ---------------------------------------------------------------------------
# The LIST routes -- the two that resolved no tenant at all.
# ---------------------------------------------------------------------------
#
# ``list_reviews`` and ``get_domain_history`` read the tenant as
# ``getattr(request.state, "tenant_id", None)`` and handed that ``None`` to
# ``JobManager.list_jobs`` / ``get_domain_history``, whose filters were applied
# only ``if tenant_id``. An unresolved tenant therefore listed EVERY tenant's
# review jobs -- and the paginated ``total`` counted them too. RLS was the only
# thing standing in front of it in the cluster.
#
# ``_no_tenant`` sends no ``X-Test-Tenant`` header, so ``_inject_tenant`` leaves
# ``request.state.tenant_id`` unset: exactly the state these tests are about.


async def _no_tenant(client: AsyncClient, path: str):
    return await client.get(path)


async def test_list_reviews_without_a_tenant_lists_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No tenant on ``request.state`` -> a 401, never everyone's jobs.

    This used to assert ``200`` with an empty page: the route substituted the
    literal ``"anonymous"`` for a missing tenant, and the scope predicate then
    matched nothing. Since migration 0013 made ``tenant_id`` a native ``uuid``
    column that fallback is gone — feeding a slug into a uuid predicate is an
    ``InvalidTextRepresentation`` 500, not an empty page — so the route refuses
    at the boundary instead. Refusing is the stronger property: "empty" depended
    on no row ever holding the sentinel, which is exactly the condition 302 dirty
    rows in prod had already violated.
    """
    app = _make_app(db_session_factory)
    a = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    b = await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Owner still sees exactly its own job -- otherwise "empty" below could
        # just mean the route is broken for everybody and proves nothing.
        owner = await _get(client, "/api/review", _TENANT_A)
        assert owner.status_code == 200, owner.text
        assert [j["job_id"] for j in owner.json()["jobs"]] == [a]
        assert owner.json()["total"] == 1

        anon = await _no_tenant(client, "/api/review")
        assert anon.status_code == 401, (
            f"a request with no tenant must be refused, not served: {anon.status_code} {anon.text}"
        )
        # Belt and braces: nothing of the other tenant's job may appear in the
        # refusal body either (an error page that echoes a leaked id is still a leak).
        assert b not in anon.text


async def test_domain_history_without_a_tenant_returns_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same for the domain-history route: no tenant -> 401, not an empty history.

    See ``test_list_reviews_without_a_tenant_lists_nothing`` for why this moved
    from "200 with an empty list" to "refused".
    """
    app = _make_app(db_session_factory)
    domain = "scoped-review.example.com"
    await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _get(client, f"/api/domain/{domain}/history", _TENANT_A)
        assert owner.status_code == 200, owner.text
        assert len(owner.json()["reviews"]) == 1, "owner lost its own history"

        anon = await _no_tenant(client, f"/api/domain/{domain}/history")
        assert anon.status_code == 401, (
            f"a request with no tenant must be refused, not served: {anon.status_code} {anon.text}"
        )


async def test_domain_history_does_not_span_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant's domain history excludes another tenant's review of it."""
    app = _make_app(db_session_factory)
    domain = "scoped-review.example.com"
    a = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    b = await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        seen = await _get(client, f"/api/domain/{domain}/history", _TENANT_A)
        assert seen.status_code == 200, seen.text
        ids = [r["job_id"] for r in seen.json()["reviews"]]
        assert ids == [a]
        assert b not in ids
