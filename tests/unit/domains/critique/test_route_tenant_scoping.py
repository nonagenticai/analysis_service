"""Every critique by-id route must resolve the job WITHIN the caller's tenant.

Shape of each case is a PAIR on ONE seeded row:

* tenant A (the owner) -> 200 with a real, asserted body field;
* tenant B (a stranger) -> 404 whose body is byte-identical to the body a
  genuinely nonexistent id produces on the same route.

The paired 200 is what stops an empty/unseeded fixture from faking every 404,
and the byte-identical comparison is what stops the mismatch from becoming a
cross-tenant existence oracle (uuid7 ids encode creation time, so a 403 --- or
any distinguishable body --- would leak "this id exists, just not for you").

These are plain route tests connecting as the table OWNER, so Postgres RLS is
inert here by design: they prove the APP-level filter and nothing else. The
separate RLS proof lives in
``tests/integration/test_rls_critique_tenant_isolation_integration.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

from src.domains.critique.models import (
    CritiqueBatch,
    CritiqueFinding,
    CritiqueJob,
    CritiqueSchedule,
    FindingSuppression,
)
from src.domains.critique.routes import router as critique_router
from src.domains.critique.services.critique_service import CritiqueService
from src.shared.database.session import get_db

pytestmark = pytest.mark.unit

_TENANT_A = "1e9e0adb-c8d4-5b05-a3ca-27add3081431"
_TENANT_B = "b2712fdc-51ee-5d40-8cb2-ac5711536776"

_FINDINGS: list[dict[str, Any]] = [
    {
        "finding_id": "f-seo-1",
        "category": "seo",
        "severity": "high",
        "title": "Missing Open Graph tags",
        "description": "No OG tags found",
        "url": "https://scoped.example.com/",
        "recommendation": "Add OG meta tags",
        "check_id": "seo-og-missing",
        "source_tool": "seo_scanner",
    }
]


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="critique-tenant-scoping-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db

    service = CritiqueService()
    service.set_adapter_registry(None)
    app.state.critique_service = service

    @app.middleware("http")
    async def _inject_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        tenant = request.headers.get("X-Test-Tenant")
        if tenant:
            request.state.tenant_id = tenant
        return await call_next(request)

    app.include_router(critique_router)
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
            CritiqueJob(
                job_id=job_id,
                tenant_id=tenant_id,
                url="https://scoped.example.com/",
                domain="scoped.example.com",
                categories=["seo"],
                max_pages=1,
                status=status,
                findings=_FINDINGS,
                category_scores={"seo": 78.5},
                overall_score=78.5,
                summary="Scoped critique summary",
                report_markdown="# Scoped report\n\nbody",
                schema_version="2.0",
                pages_scanned=1,
                completed_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return job_id


# ---------------------------------------------------------------------------
# Per-route drivers. Each returns the response for (job_id, tenant); the
# ``check`` asserts a real body field so a 200 cannot be an empty shell.
# ---------------------------------------------------------------------------


def _h(tenant: str) -> dict[str, str]:
    return {"X-Test-Tenant": tenant}


async def _get(client: AsyncClient, path: str, tenant: str):
    return await client.get(path, headers=_h(tenant))


async def _delete_row(
    factory: async_sessionmaker[AsyncSession],
    row_id: str,
    model: type = CritiqueJob,
    pk: str = "job_id",
) -> None:
    async with factory() as session:
        await session.execute(sa_delete(model).where(getattr(model, pk) == row_id))
        await session.commit()


async def _assert_indistinguishable_from_missing(
    factory: async_sessionmaker[AsyncSession],
    send: Callable[[], Awaitable[Any]],
    row_id: str,
    model: type = CritiqueJob,
    pk: str = "job_id",
) -> None:
    """The stranger's 404 must be byte-identical to a genuinely-missing 404.

    Comparing against a DIFFERENT nonexistent id would be a weaker (and
    unpassable) test, because these bodies echo the id the caller supplied ---
    which the caller already knows. So the same id is used for both halves:
    ask as the stranger while the row exists, delete the row, ask again. If
    those two responses differ in any byte, the mismatch is an existence
    oracle over uuid7 ids that encode creation time.
    """
    while_exists = await send()
    assert while_exists.status_code == 404, (
        f"cross-tenant read returned {while_exists.status_code}, expected 404 "
        "(403 would itself be the oracle; 409/400 would mean a status branch "
        "ran before the tenant-scoped lookup)"
    )

    await _delete_row(factory, row_id, model, pk)

    now_missing = await send()
    assert now_missing.status_code == 404
    assert while_exists.content == now_missing.content, (
        "cross-tenant 404 body differs from the genuine not-found body -- oracle"
    )


_ROUTES: list[tuple[str, str, Callable[[Any, str], None]]] = [
    (
        "get_critique",
        "/api/critique/{jid}",
        lambda body, jid: _assert(body["job_id"] == jid and body["status"] == "completed"),
    ),
    (
        "get_critique_findings",
        "/api/critique/{jid}/findings",
        lambda body, jid: _assert(body["total"] == 1 and body["job_id"] == jid),
    ),
    (
        "get_critique_unified_findings",
        "/api/critique/{jid}/findings/unified",
        lambda body, _jid: _assert(len(body) == 1),
    ),
    (
        "export_critique_sarif",
        "/api/critique/{jid}/export/sarif",
        lambda body, _jid: _assert(body["version"] == "2.1.0"),
    ),
    (
        "get_critique_report",
        "/api/critique/{jid}/report",
        lambda body, jid: _assert(body["job_id"] == jid and "Scoped report" in body["report"]),
    ),
    (
        "get_critique_report_json",
        "/api/critique/{jid}/report?format=json",
        lambda body, _jid: _assert(body["url"] == "https://scoped.example.com/"),
    ),
]


def _assert(cond: object) -> None:
    assert cond


@pytest.mark.parametrize(("name", "template", "check"), _ROUTES, ids=[r[0] for r in _ROUTES])
async def test_by_id_route_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
    name: str,
    template: str,
    check: Callable[[Any, str], None],
) -> None:
    """Owner gets a real 200; a stranger gets the genuine-not-found 404 body."""
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


async def test_export_csv_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/critique/{job_id}/export/csv", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert "seo-og-missing" in ok.text

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/{job_id}/export/csv", _TENANT_B),
            job_id,
        )


async def test_report_pdf_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    with patch(
        "src.domains.critique.services.pdf_generator.html_to_pdf",
        return_value=b"%PDF-1.4 scoped",
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await _get(client, f"/api/critique/{job_id}/report/pdf", _TENANT_A)
            assert ok.status_code == 200, ok.text
            assert ok.content.startswith(b"%PDF")

            await _assert_indistinguishable_from_missing(
                db_session_factory,
                lambda: _get(client, f"/api/critique/{job_id}/report/pdf", _TENANT_B),
                job_id,
            )


async def test_update_tags_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.patch(
            f"/api/critique/{job_id}/tags", json={"tags": ["a"]}, headers=_h(_TENANT_A)
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["job_id"] == job_id

        stranger = await client.patch(
            f"/api/critique/{job_id}/tags", json={"tags": ["hijack"]}, headers=_h(_TENANT_B)
        )
        assert stranger.status_code == 404

        # The stranger's write must not have landed.
        async with db_session_factory() as session:
            row = (
                await session.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
            ).scalar_one()
            assert row.tags == ["a"]

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: client.patch(
                f"/api/critique/{job_id}/tags", json={"tags": ["hijack"]}, headers=_h(_TENANT_B)
            ),
            job_id,
        )


async def test_rerun_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    # Neutralise the detached scan itself, not ``asyncio.create_task`` — patching
    # create_task globally also intercepts the test client's own transport.
    with patch(
        "src.domains.critique.routes.jobs._process_job_background",
        new_callable=AsyncMock,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await client.post(f"/api/critique/{job_id}/rerun", json={}, headers=_h(_TENANT_A))
            assert ok.status_code == 200, ok.text
            assert ok.json()["job_id"] != job_id

            await _assert_indistinguishable_from_missing(
                db_session_factory,
                lambda: client.post(
                    f"/api/critique/{job_id}/rerun", json={}, headers=_h(_TENANT_B)
                ),
                job_id,
            )


async def test_progress_sse_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The SSE gate runs inside the request scope; the generator must not need it.

    The route deliberately does NOT take ``Depends(get_db)`` — a request-scoped
    session would stay open for the whole stream and pin a pgbouncer connection
    ``idle in transaction`` (see
    ``tests/unit/domains/critique/test_sse_progress_holds_no_session.py``). It
    takes a short-lived ``get_db_session()`` for the ownership pre-flight
    instead, so that is what has to be pointed at the test factory here.
    """
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    fake_redis = AsyncMock()
    fake_redis.get = AsyncMock(return_value=json.dumps({"status": "completed"}))

    @asynccontextmanager
    async def _preflight_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    with (
        patch("src.domains.critique.routes.jobs.get_db_session", _preflight_session),
        patch("src.shared.cache.redis_client.get_redis_client", return_value=fake_redis),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ok = await _get(client, f"/api/critique/{job_id}/progress", _TENANT_A)
            assert ok.status_code == 200, ok.text
            assert "completed" in ok.text

            await _assert_indistinguishable_from_missing(
                db_session_factory,
                lambda: _get(client, f"/api/critique/{job_id}/progress", _TENANT_B),
                job_id,
            )


async def test_delete_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stranger = await client.delete(f"/api/critique/{job_id}", headers=_h(_TENANT_B))
        assert stranger.status_code == 404

        # The row survived the stranger's DELETE...
        async with db_session_factory() as session:
            assert (
                await session.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
            ).scalar_one_or_none() is not None

        # ...and the owner can still delete it.
        ok = await client.delete(f"/api/critique/{job_id}", headers=_h(_TENANT_A))
        assert ok.status_code == 204, ok.text

    async with db_session_factory() as session:
        assert (
            await session.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
        ).scalar_one_or_none() is None

    # And now that the row is genuinely gone, the SAME request the stranger made
    # produces a byte-identical body -- so "not yours" and "not there" are
    # indistinguishable.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        gone = await client.delete(f"/api/critique/{job_id}", headers=_h(_TENANT_B))
        assert gone.status_code == 404
        assert gone.content == stranger.content


async def test_compare_scopes_both_jobs(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """compare fetches TWO jobs -- scoping only one leaves the route a leak."""
    app = _make_app(db_session_factory)
    a1 = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    a2 = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    b1 = await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/critique/compare?job_id_1={a1}&job_id_2={a2}", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert ok.json()["job_id_1"] == a1

        # First slot is a foreign job.
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/compare?job_id_1={b1}&job_id_2={a2}", _TENANT_A),
            b1,
        )

    # Second slot is a foreign job (fresh row: the first half deleted b1).
    b2 = await _seed_job(db_session_factory, tenant_id=_TENANT_B)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/compare?job_id_1={a1}&job_id_2={b2}", _TENANT_A),
            b2,
        )


async def test_mismatch_on_incomplete_job_is_404_not_409(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Check-ordering guard.

    The report route answers 409 for a job that exists but is not completed.
    If that branch ran before the tenant-scoped lookup, a stranger would learn
    the job's status. The tenant filter must come first: 404, always.
    """
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A, status="running")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _get(client, f"/api/critique/{job_id}/report", _TENANT_A)
        assert owner.status_code == 409, "owner should see the real 409 for a running job"

        stranger = await _get(client, f"/api/critique/{job_id}/report", _TENANT_B)
        assert stranger.status_code == 404, (
            f"status leaked: stranger got {stranger.status_code} on another tenant's running job"
        )
        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/{job_id}/report", _TENANT_B),
            job_id,
        )


# ---------------------------------------------------------------------------
# Second sweep: by-id routes on the sibling tables (batch, suppression,
# finding, schedule). Three of these are DESTRUCTIVE writes, so a 404 status
# is not sufficient evidence -- each one re-reads the row afterwards and
# asserts it survived. A refused DELETE that still deleted would otherwise
# pass a status-only assertion.
# ---------------------------------------------------------------------------


async def _seed_batch(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: str, job_ids: list[str]
) -> str:
    batch_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            CritiqueBatch(
                batch_id=batch_id,
                tenant_id=tenant_id,
                job_ids=job_ids,
                total=len(job_ids),
                status="pending",
            )
        )
        await session.commit()
    return batch_id


async def _seed_suppression(factory: async_sessionmaker[AsyncSession], *, tenant_id: str) -> str:
    suppression_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            FindingSuppression(
                suppression_id=suppression_id,
                tenant_id=tenant_id,
                check_id="seo-og-missing",
                reason="accepted risk",
                status="acknowledged",
            )
        )
        await session.commit()
    return suppression_id


async def _seed_finding(
    factory: async_sessionmaker[AsyncSession], *, job_id: str, tenant_id: str
) -> str:
    finding_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            CritiqueFinding(
                job_id=job_id,
                tenant_id=tenant_id,
                finding_id=finding_id,
                category="seo",
                severity="high",
                title="Missing Open Graph tags",
                description="No OG tags found",
                check_id="seo-og-missing",
            )
        )
        await session.commit()
    return finding_id


async def _seed_schedule(factory: async_sessionmaker[AsyncSession], *, tenant_id: str) -> str:
    schedule_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            CritiqueSchedule(
                schedule_id=schedule_id,
                tenant_id=tenant_id,
                url="https://scoped.example.com/",
                categories=["seo"],
                cron_expression="@daily",
                enabled=True,
            )
        )
        await session.commit()
    return schedule_id


async def test_batch_status_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The batch body carries status, totals AND the full job_ids list."""
    app = _make_app(db_session_factory)
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    batch_id = await _seed_batch(db_session_factory, tenant_id=_TENANT_A, job_ids=[job_id])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/critique/batch/{batch_id}", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert ok.json()["batch_id"] == batch_id
        assert ok.json()["job_ids"] == [job_id]

        stranger = await _get(client, f"/api/critique/batch/{batch_id}", _TENANT_B)
        assert stranger.status_code == 404, (
            f"cross-tenant batch read returned {stranger.status_code} and leaked: {stranger.text}"
        )

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/batch/{batch_id}", _TENANT_B),
            batch_id,
            model=CritiqueBatch,
            pk="batch_id",
        )


async def test_delete_suppression_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DESTRUCTIVE: a refused DELETE must leave the row in place."""
    app = _make_app(db_session_factory)
    suppression_id = await _seed_suppression(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stranger = await client.delete(
            f"/api/critique/suppressions/{suppression_id}", headers=_h(_TENANT_B)
        )
        assert stranger.status_code == 404, f"cross-tenant DELETE returned {stranger.status_code}"

        # Status alone proves nothing -- re-read the row.
        async with db_session_factory() as session:
            row = (
                await session.execute(
                    select(FindingSuppression).where(
                        FindingSuppression.suppression_id == suppression_id
                    )
                )
            ).scalar_one_or_none()
            assert row is not None, "tenant B DELETED tenant A's suppression row"
            assert row.tenant_id == _TENANT_A

        # Paired own-tenant success on the same fixture.
        owner = await client.delete(
            f"/api/critique/suppressions/{suppression_id}", headers=_h(_TENANT_A)
        )
        assert owner.status_code == 204, owner.text

    async with db_session_factory() as session:
        assert (
            await session.execute(
                select(FindingSuppression).where(
                    FindingSuppression.suppression_id == suppression_id
                )
            )
        ).scalar_one_or_none() is None


async def test_acknowledge_finding_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DESTRUCTIVE: acknowledging flips is_suppressed on the parent's finding.

    ``critique_findings`` now carries its own ``tenant_id`` (RLS-enrolled), so
    the scope is a direct predicate — plus the route's own ``{job_id}`` path
    param, which it previously ignored entirely.
    """
    app = _make_app(db_session_factory)
    job_a = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    finding_id = await _seed_finding(db_session_factory, job_id=job_a, tenant_id=_TENANT_A)
    job_b = await _seed_job(db_session_factory, tenant_id=_TENANT_B)

    body = {"reason": "hijacked"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # (a) stranger, naming the real parent job.
        stranger = await client.post(
            f"/api/critique/{job_a}/findings/{finding_id}/acknowledge",
            json=body,
            headers=_h(_TENANT_B),
        )
        assert stranger.status_code == 404, (
            f"cross-tenant acknowledge returned {stranger.status_code}: {stranger.text}"
        )

        # (b) stranger, laundering it through a job it DOES own -- the route
        #     used to ignore {job_id}, so this is the same write by another door.
        laundered = await client.post(
            f"/api/critique/{job_b}/findings/{finding_id}/acknowledge",
            json=body,
            headers=_h(_TENANT_B),
        )
        assert laundered.status_code == 404, (
            f"finding reachable via a foreign job_id: {laundered.status_code}"
        )

        # (c) owner, but naming the WRONG job -- a mismatched pair is not found,
        #     which also closes the 200-vs-404 existence oracle on finding ids.
        mismatched = await client.post(
            f"/api/critique/{job_b}/findings/{finding_id}/acknowledge",
            json=body,
            headers=_h(_TENANT_A),
        )
        assert mismatched.status_code == 404

        # The write must not have landed in any of the three.
        async with db_session_factory() as session:
            row = (
                await session.execute(
                    select(CritiqueFinding).where(CritiqueFinding.finding_id == finding_id)
                )
            ).scalar_one()
            assert row.is_suppressed is False, "a refused acknowledge still suppressed the finding"
            assert row.suppression_id is None

        # Paired own-tenant success with the correct job_id.
        owner = await client.post(
            f"/api/critique/{job_a}/findings/{finding_id}/acknowledge",
            json=body,
            headers=_h(_TENANT_A),
        )
        assert owner.status_code == 200, owner.text
        assert owner.json()["status"] == "acknowledged"
        assert owner.json()["finding_id"] == finding_id

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(CritiqueFinding).where(CritiqueFinding.finding_id == finding_id)
            )
        ).scalar_one()
        assert row.is_suppressed is True


async def test_get_schedule_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GET leaks the victim's own tenant_id in the response body."""
    app = _make_app(db_session_factory)
    schedule_id = await _seed_schedule(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await _get(client, f"/api/critique/schedules/{schedule_id}", _TENANT_A)
        assert ok.status_code == 200, ok.text
        assert ok.json()["schedule_id"] == schedule_id
        assert ok.json()["tenant_id"] == _TENANT_A

        stranger = await _get(client, f"/api/critique/schedules/{schedule_id}", _TENANT_B)
        assert stranger.status_code == 404, (
            f"cross-tenant schedule read returned {stranger.status_code} "
            f"and leaked the owner's tenant_id: {stranger.text}"
        )

        await _assert_indistinguishable_from_missing(
            db_session_factory,
            lambda: _get(client, f"/api/critique/schedules/{schedule_id}", _TENANT_B),
            schedule_id,
            model=CritiqueSchedule,
            pk="schedule_id",
        )


async def test_update_schedule_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DESTRUCTIVE: a refused PUT must leave every column unchanged."""
    app = _make_app(db_session_factory)
    schedule_id = await _seed_schedule(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stranger = await client.put(
            f"/api/critique/schedules/{schedule_id}",
            json={"enabled": False, "cron_expression": "@hourly"},
            headers=_h(_TENANT_B),
        )
        assert stranger.status_code == 404, (
            f"cross-tenant PUT returned {stranger.status_code}: {stranger.text}"
        )

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    select(CritiqueSchedule).where(CritiqueSchedule.schedule_id == schedule_id)
                )
            ).scalar_one()
            assert row.enabled is True, "tenant B disabled tenant A's schedule"
            assert row.cron_expression == "@daily", "tenant B rewrote tenant A's cron"

        owner = await client.put(
            f"/api/critique/schedules/{schedule_id}",
            json={"cron_expression": "@hourly"},
            headers=_h(_TENANT_A),
        )
        assert owner.status_code == 200, owner.text
        assert owner.json()["cron_expression"] == "@hourly"


async def test_delete_schedule_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DESTRUCTIVE: a refused DELETE must leave the row in place."""
    app = _make_app(db_session_factory)
    schedule_id = await _seed_schedule(db_session_factory, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stranger = await client.delete(
            f"/api/critique/schedules/{schedule_id}", headers=_h(_TENANT_B)
        )
        assert stranger.status_code == 404, f"cross-tenant DELETE returned {stranger.status_code}"

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    select(CritiqueSchedule).where(CritiqueSchedule.schedule_id == schedule_id)
                )
            ).scalar_one_or_none()
            assert row is not None, "tenant B DELETED tenant A's schedule row"
            assert row.tenant_id == _TENANT_A

        owner = await client.delete(f"/api/critique/schedules/{schedule_id}", headers=_h(_TENANT_A))
        assert owner.status_code == 204, owner.text

    async with db_session_factory() as session:
        assert (
            await session.execute(
                select(CritiqueSchedule).where(CritiqueSchedule.schedule_id == schedule_id)
            )
        ).scalar_one_or_none() is None
