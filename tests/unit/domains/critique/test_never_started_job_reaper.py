"""Real-Postgres proof for the never-started critique reaper and the batch prune (0015).

Every assertion here is on the ROW, never on a mock call count.

The two ways to ship a vacuous test in this file
------------------------------------------------
1. ``critique_jobs`` strands at ``'pending'``, not ``'running'`` — 0010 already
   owns ``'running'``. A predicate that copied 0010's would sweep zero rows on
   every tick forever while logging nothing wrong, so the reap test seeds
   ``'pending'`` rows and asserts they STOP being ``'pending'``.

2. ``'pending'`` is ALSO the legitimate queued state. A test that only proved
   "stranded rows get reaped" would stay green if someone collapsed the two
   cutoffs onto the 60-minute ``running`` budget — and that change reaps live
   work. ``test_collapsing_the_two_cutoffs_reaps_live_work`` is the regression
   guard for exactly that edit, and it is the point of the whole file.
"""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.domains.critique.models import CritiqueBatch, CritiqueJob
from src.domains.critique.services.reaper import (
    NEVER_STARTED_REAP_FN_NAME,
    REAP_STALE_CRITIQUE_JOBS_DDL,
    reap_never_started_jobs,
    reap_stale_jobs,
)

pytestmark = pytest.mark.asyncio

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "shared"
    / "database"
    / "migrations"
    / "versions"
    / "0015_sweep_stranded_row_siblings.py"
)

# Tenants are a native ``uuid`` column (0013) — a non-uuid string does not fail a
# type check, it fails at the driver.
_T1 = "c2b5c1f9-2d3e-4f40-9b01-000000000001"
_T2 = "c2b5c1f9-2d3e-4f40-9b01-000000000002"


def _load_migration():
    """Import the 0015 module by path: its name starts with a digit."""
    spec = importlib.util.spec_from_file_location("_mig_0015", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


async def _install(session_factory, ddl: str) -> None:
    async with session_factory() as s:
        await s.execute(text(ddl))
        await s.commit()


async def _add(session_factory, *rows) -> None:
    async with session_factory() as s:
        for row in rows:
            s.add(row)
        await s.commit()


async def _job(session_factory, job_id: str) -> CritiqueJob:
    async with session_factory() as s:
        return (
            await s.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
        ).scalar_one()


def _pending_job(job_id: str, tenant: str, age_minutes: float) -> CritiqueJob:
    """A row exactly as ``create_job`` commits it: pending, no ``started_at``."""
    return CritiqueJob(
        job_id=job_id,
        tenant_id=tenant,
        url=f"https://example.test/{job_id}",
        status="pending",
        created_at=_ago(age_minutes),
        started_at=None,
    )


class TestNeverStartedCritiqueReaper:
    async def test_aged_pending_job_reaches_a_terminal_state(self, db_session_factory):
        """The whole point: a row nothing will ever finalize stops being ``pending``.

        ``create_job`` COMMITS at ``pending`` before ``routes/jobs.py`` calls
        ``asyncio.create_task``; a process that dies in that window leaves a row
        0010's reaper (``running``) and 0005's prune (terminal only) both miss.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._REAP_NEVER_STARTED_CRITIQUE_JOBS_FN)
        await _add(
            db_session_factory,
            _pending_job("cj-stranded", _T1, 60 * 24),
            _pending_job("cj-queued", _T2, 1),
        )

        async with db_session_factory() as s:
            reaped = await reap_never_started_jobs(s, stale_after_seconds=6 * 3600)

        assert reaped == 1

        stranded = await _job(db_session_factory, "cj-stranded")
        assert stranded.status == "failed"
        assert "reaped" in (stranded.error_message or "")
        assert stranded.completed_at is not None

        # CONTROL: a legitimately-queued recent row is untouched.
        assert (await _job(db_session_factory, "cj-queued")).status == "pending"

    async def test_a_row_that_recorded_a_start_is_left_to_the_running_reaper(
        self, db_session_factory
    ):
        """``started_at IS NULL`` is a real predicate, not decoration.

        A row carrying a ``started_at`` is by definition not never-started, and
        belongs to whichever sweep owns its status — never to this one.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._REAP_NEVER_STARTED_CRITIQUE_JOBS_FN)
        job = _pending_job("cj-started", _T1, 60 * 24)
        job.started_at = _ago(60 * 24)
        await _add(db_session_factory, job)

        async with db_session_factory() as s:
            reaped = await reap_never_started_jobs(s, stale_after_seconds=0)

        assert reaped == 0
        assert (await _job(db_session_factory, "cj-started")).status == "pending"

    async def test_collapsing_the_two_cutoffs_reaps_live_work(self, db_session_factory):
        """⛔ REGRESSION GUARD. The two cutoffs must never become one.

        A job queued 90 minutes ago is LIVE work. Under the ``running`` budget
        (60m) it would be failed; under the ``pending`` budget (6h) it is left
        alone. This test drives the resolver at BOTH budgets against the same row
        and asserts the outcomes DIFFER — so any edit that makes the pending sweep
        use ``stale_after_minutes`` turns it red on the second assertion.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._REAP_NEVER_STARTED_CRITIQUE_JOBS_FN)
        await _add(db_session_factory, _pending_job("cj-queued-90m", _T1, 90))

        # The pending budget (6h) leaves it alone -- it is merely waiting.
        async with db_session_factory() as s:
            assert await reap_never_started_jobs(s, stale_after_seconds=6 * 3600) == 0
        assert (await _job(db_session_factory, "cj-queued-90m")).status == "pending"

        # The running budget (1h) would kill it. That is why they are two settings.
        async with db_session_factory() as s:
            assert await reap_never_started_jobs(s, stale_after_seconds=3600) == 1
        assert (await _job(db_session_factory, "cj-queued-90m")).status == "failed"

    async def test_the_two_reapers_sweep_disjoint_statuses(self, db_session_factory):
        """A genuine ``running`` stall is still reaped — by 0010, never by this one.

        Pins the split in both directions, because the cheap wrong fix here is one
        function that matches ``status IN ('pending','running')``.
        """
        assert NEVER_STARTED_REAP_FN_NAME != "reap_stale_critique_jobs"
        migration = _load_migration()
        never_started_ddl = migration._REAP_NEVER_STARTED_CRITIQUE_JOBS_FN
        assert "status = 'pending'" in never_started_ddl
        assert "'running'" not in never_started_ddl
        assert "status = 'running'" in REAP_STALE_CRITIQUE_JOBS_DDL
        assert "'pending'" not in REAP_STALE_CRITIQUE_JOBS_DDL

        await _install(db_session_factory, never_started_ddl)
        await _install(db_session_factory, REAP_STALE_CRITIQUE_JOBS_DDL)

        running = _pending_job("cj-running-stall", _T1, 60 * 24)
        running.status = "running"
        running.started_at = _ago(60 * 24)
        await _add(db_session_factory, running, _pending_job("cj-pending", _T1, 60 * 24))

        async with db_session_factory() as s:
            assert await reap_never_started_jobs(s, stale_after_seconds=6 * 3600) == 1
            assert await reap_stale_jobs(s, stale_after_seconds=3600) == 1

        assert (await _job(db_session_factory, "cj-running-stall")).status == "failed"
        assert (await _job(db_session_factory, "cj-pending")).status == "failed"


class TestTheLoopSpendsTwoBudgets:
    """The DB-level guard above cannot see a collapse made in the LOOP.

    ``test_collapsing_the_two_cutoffs_reaps_live_work`` proves the resolver
    behaves differently at the two budgets, but it drives the resolver directly.
    The other place the collapse can be written is ``run_reaper_loop`` passing
    ``stale_after_minutes`` to BOTH sweeps — which no row-level test can see. This
    captures what the loop actually spends.
    """

    async def test_run_reaper_loop_passes_the_pending_budget_to_the_pending_sweep(
        self, monkeypatch
    ):
        from src.config import get_settings
        from src.domains.critique.services import reaper as reaper_module

        settings = get_settings()
        monkeypatch.setattr(settings.reaper, "enabled", True)
        monkeypatch.setattr(settings.reaper, "stale_after_minutes", 60)
        monkeypatch.setattr(settings.reaper, "pending_stale_after_minutes", 360)
        monkeypatch.setattr(settings.reaper, "interval_minutes", 10)
        monkeypatch.setattr(reaper_module, "get_settings", lambda: settings)

        spent: dict[str, float] = {}

        class _StopLoopError(Exception):
            pass

        @asynccontextmanager
        async def _fake_session():
            yield object()

        async def _fake_stale(_db, seconds):
            spent["running"] = seconds
            return 0

        async def _fake_never_started(_db, seconds):
            spent["pending"] = seconds
            return 0

        async def _stop(_seconds):
            raise _StopLoopError

        monkeypatch.setattr(reaper_module, "reap_stale_jobs", _fake_stale)
        monkeypatch.setattr(reaper_module, "reap_never_started_jobs", _fake_never_started)
        monkeypatch.setattr(reaper_module.asyncio, "sleep", _stop)
        monkeypatch.setattr(
            "src.shared.database.session.get_db_session", lambda *a, **k: _fake_session()
        )

        with pytest.raises(_StopLoopError):
            await reaper_module.run_reaper_loop()

        # The two budgets are DIFFERENT numbers and each sweep gets its own.
        assert spent == {"running": 3600.0, "pending": 21600.0}


class TestCritiqueBatchRetention:
    async def test_prune_deletes_terminal_batches_and_keeps_live_ones(self, db_session_factory):
        """``critique_batches`` was in NO prune body before 0015.

        Terminal for a batch is ``'completed'`` ALONE — ``update_batch_status``
        writes only ``'completed'``/``'running'`` and ``create_batch`` writes
        ``'pending'``. A non-terminal batch has ``completed_at IS NULL`` and is
        excluded by the comparison, which is the intended behaviour.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._PRUNE_CRITIQUE_FN_V2)

        old, recent = _ago(60 * 24 * 200), _ago(1)
        await _add(
            db_session_factory,
            CritiqueBatch(
                batch_id="b-old-done", tenant_id=_T1, status="completed", completed_at=old
            ),
            CritiqueBatch(
                batch_id="b-new-done", tenant_id=_T1, status="completed", completed_at=recent
            ),
            CritiqueBatch(batch_id="b-running", tenant_id=_T1, status="running", completed_at=None),
            CritiqueBatch(batch_id="b-pending", tenant_id=_T2, status="pending", completed_at=None),
        )

        cutoff = _ago(60 * 24 * 90)
        async with db_session_factory() as s:
            counts = (
                await s.execute(text("SELECT prune_critique_data(:c, :c)"), {"c": cutoff})
            ).scalar_one()
            await s.commit()

        assert counts["batches"] == 1

        async with db_session_factory() as s:
            surviving = sorted((await s.execute(select(CritiqueBatch.batch_id))).scalars().all())
        assert surviving == ["b-new-done", "b-pending", "b-running"]

    async def test_prune_v2_still_does_everything_v1_did(self, db_session_factory):
        """The replacement is ADDITIVE — it must not have dropped a table on the way.

        ``prune_critique_data`` is replaced at its identical signature so its owner
        and its 0011 EXECUTE grant survive; the risk of a same-signature replace is
        silently losing one of the three original DELETEs.
        """
        migration = _load_migration()
        v1, v2 = migration._PRUNE_CRITIQUE_FN_V1, migration._PRUNE_CRITIQUE_FN_V2
        for table in ("critique_jobs", "critique_findings", "audit_logs"):
            # Not a query -- a substring search over the migration's DDL text.
            clause = "DELETE FROM " + table  # noqa: S608
            assert clause in v1
            assert clause in v2
        assert "DELETE FROM critique_batches" in v2
        assert "DELETE FROM critique_batches" not in v1

        await _install(db_session_factory, v2)
        old = _ago(60 * 24 * 200)
        await _add(
            db_session_factory,
            CritiqueJob(
                job_id="pj-old-done",
                tenant_id=_T1,
                url="https://example.test/pj",
                status="completed",
                created_at=old,
            ),
            CritiqueJob(
                job_id="pj-old-live",
                tenant_id=_T1,
                url="https://example.test/pj2",
                status="running",
                created_at=old,
            ),
        )

        cutoff = _ago(60 * 24 * 90)
        async with db_session_factory() as s:
            counts = (
                await s.execute(text("SELECT prune_critique_data(:c, :c)"), {"c": cutoff})
            ).scalar_one()
            await s.commit()

        assert counts["jobs"] == 1
        async with db_session_factory() as s:
            assert (await s.execute(select(CritiqueJob.job_id))).scalars().all() == ["pj-old-live"]
