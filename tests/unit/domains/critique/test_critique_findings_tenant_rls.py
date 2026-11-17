"""``critique_findings`` is tenant-owned: write stamp, RLS isolation, read paths.

The table used to have no ``tenant_id`` at all, which left it outside the
service's RLS enrolment (``w2v_common.database.rls.tenant_table_names`` selects
by that column) — the one tenant-owned table in an otherwise-enforced service.

These tests drive the service/manager layer DIRECTLY. The write predicate
(``_save_findings`` stamping the parent job's tenant) is invisible from the
route, because every route read is already tenant-scoped: a finding stamped with
the WRONG tenant simply disappears from its owner's view instead of surfacing as
a leak, so a route-level test would stay green with the stamp removed entirely.

Deleted here: ``test_finalize_stamps_findings_with_the_parent_jobs_tenant``. It
passed a different tenant as ``_phase_finalize_completed``'s ``tenant_id``
argument to "prove" the stamp came from ``job.tenant_id`` — but ``service.py``
loads the job with ``_get_job(..., tenant_id=tenant_id)``, so job and argument can
never diverge on any reachable path. It pinned no bug. The write stamp is covered
by ``test_save_findings_persists_parent_job_tenant`` against a real DB.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.domains.critique.models import CritiqueFinding, CritiqueJob
from src.domains.critique.services.analytics_service import AnalyticsService
from src.domains.critique.services.critique_service import CritiqueService

_TENANT_A = "098c2fd8-7fb9-509e-b9db-965f41fda572"
_TENANT_B = "40d79be7-25c7-5b4f-9bac-fd67a0241850"


def _finding(title: str, *, check_id: str = "seo-og-missing") -> dict[str, Any]:
    return {
        "finding_id": str(uuid.uuid7()),
        "category": "seo",
        "severity": "high",
        "title": title,
        "description": "d",
        "check_id": check_id,
    }


async def _seed_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    domain: str = "ex.test",
    status: str = "completed",
) -> str:
    job_id = str(uuid.uuid7())
    async with factory() as session:
        session.add(
            CritiqueJob(
                job_id=job_id,
                tenant_id=tenant_id,
                url=f"https://{domain}/",
                domain=domain,
                status=status,
                overall_score=80.0,
            )
        )
        await session.commit()
    return job_id


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


async def test_save_findings_persists_parent_job_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rows written by ``_save_findings`` carry the tenant, not NULL."""
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    svc = object.__new__(CritiqueService)

    async with db_session_factory() as session:
        await svc._save_findings(
            session, job_id, [_finding("a"), _finding("b")], tenant_id=_TENANT_A
        )
        await session.commit()

    async with db_session_factory() as session:
        rows = (
            (await session.execute(select(CritiqueFinding).where(CritiqueFinding.job_id == job_id)))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {_TENANT_A}, "findings were not stamped with the tenant"


# ---------------------------------------------------------------------------
# Read paths still work for the owner
# ---------------------------------------------------------------------------


async def test_analytics_top_findings_scoped_to_own_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``get_top_findings`` applies BOTH guards, and each one alone is insufficient.

    ``db_session_factory`` connects as the OWNER, so RLS is inert here — the
    application predicate is the only defence, which is exactly what this pins.
    The seed makes the two ``where`` clauses in ``AnalyticsService.get_top_findings``
    separable, so each can be mutated away independently:

    * ``cross-stamp`` — a finding on tenant A's own completed job, but stamped
      with tenant B. Only ``CritiqueFinding.tenant_id == tenant_id`` excludes it;
      it IS in A's ``job_ids`` list. Deleting that clause makes ``mine`` contain
      ``cross-stamp`` → this test fails. (In the old fixture — one job per tenant,
      every finding stamped consistently — deleting it changed nothing.)
    * ``stale-job`` — a finding stamped tenant A on a job of A's that the job
      query does not select (status ``pending``). Only
      ``CritiqueFinding.job_id.in_(job_ids)`` excludes it. Deleting that clause
      makes ``mine`` contain ``stale-job`` → this test fails.
    """
    job_a = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    job_b = await _seed_job(db_session_factory, tenant_id=_TENANT_B)
    job_a_pending = await _seed_job(db_session_factory, tenant_id=_TENANT_A, status="pending")
    svc = object.__new__(CritiqueService)

    async with db_session_factory() as session:
        await svc._save_findings(
            session, job_a, [_finding("mine", check_id="mine")], tenant_id=_TENANT_A
        )
        await svc._save_findings(
            session, job_b, [_finding("theirs", check_id="theirs")], tenant_id=_TENANT_B
        )
        # In A's job_ids list, but owned by B → only the tenant guard drops it.
        await svc._save_findings(
            session, job_a, [_finding("cross", check_id="cross-stamp")], tenant_id=_TENANT_B
        )
        # Owned by A, but on a job outside the completed-job list → only the
        # job_id guard drops it.
        await svc._save_findings(
            session, job_a_pending, [_finding("stale", check_id="stale-job")], tenant_id=_TENANT_A
        )
        await session.commit()

    analytics = AnalyticsService()
    async with db_session_factory() as session:
        mine = await analytics.get_top_findings(session, _TENANT_A)
        theirs = await analytics.get_top_findings(session, _TENANT_B)

    assert [r["check_id"] for r in mine] == ["mine"], (
        "get_top_findings returned a finding it should have excluded — "
        "'cross-stamp' means the tenant_id guard is gone, 'stale-job' the job_id guard"
    )
    # B owns two findings but only one sits on a job of B's own.
    assert [r["check_id"] for r in theirs] == ["theirs"]


# ---------------------------------------------------------------------------
# Delete path — findings must not outlive their job
# ---------------------------------------------------------------------------


async def test_delete_job_also_deletes_its_findings(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``delete_job`` removes the job's findings in the same transaction.

    ``critique_findings.job_id`` has no FK and no ``ON DELETE CASCADE``, so
    ``delete_job`` used to do ``db.delete(job)`` alone — every job any tenant ever
    deleted left its findings on disk forever, unreachable and (before this table
    had a ``tenant_id``) outside RLS. That is the dominant source of the orphans
    migration 0008 sweeps, and without this fix the orphan set starts regrowing
    the moment the migration finishes.

    Kills the mutation: delete the ``db.execute(delete(CritiqueFinding)...)``
    statement from ``delete_job`` → ``leftover`` is 1 and this fails. The other
    tenant's finding proves the delete is not a table-wide wipe.
    """
    job_id = await _seed_job(db_session_factory, tenant_id=_TENANT_A)
    other_job = await _seed_job(db_session_factory, tenant_id=_TENANT_B)
    svc = object.__new__(CritiqueService)

    async with db_session_factory() as session:
        await svc._save_findings(session, job_id, [_finding("doomed")], tenant_id=_TENANT_A)
        await svc._save_findings(session, other_job, [_finding("kept")], tenant_id=_TENANT_B)
        await session.commit()

    async with db_session_factory() as session:
        assert await svc.delete_job(session, job_id, tenant_id=_TENANT_A) is True

    async with db_session_factory() as session:
        leftover = (
            (await session.execute(select(CritiqueFinding).where(CritiqueFinding.job_id == job_id)))
            .scalars()
            .all()
        )
        survivor = (
            (
                await session.execute(
                    select(CritiqueFinding).where(CritiqueFinding.job_id == other_job)
                )
            )
            .scalars()
            .all()
        )
    assert leftover == [], f"delete_job orphaned {len(leftover)} finding(s)"
    assert len(survivor) == 1, "delete_job deleted another job's findings"
