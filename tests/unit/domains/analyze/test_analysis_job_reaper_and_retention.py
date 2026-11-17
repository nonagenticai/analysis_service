"""Real-Postgres proof for the ``analysis_jobs`` reaper and retention (0015).

Every assertion here is on the ROW, never on a mock call count.

The way to ship a vacuous test in this file
-------------------------------------------
``analysis_jobs`` strands at ``'processing'``. ``'running'`` is a critique-only
literal, and while the ORM column default is ``'pending'`` every insert path in
``src/`` passes ``status='processing'`` explicitly — so a reaper on either of
those predicates sweeps zero rows forever while looking perfectly healthy. The
reap test therefore seeds ``'processing'`` rows and asserts they STOP being
``'processing'``, and ``test_the_swept_status_is_processing`` pins the literal in
both directions.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.domains.analyze.models import AnalysisJob
from src.domains.analyze.services.reaper import reap_stale_analysis_jobs
from src.domains.analyze.services.retention import prune_old_analysis_jobs

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
_T1 = "d3c6d2fa-3e4f-4051-ac12-000000000001"
_T2 = "d3c6d2fa-3e4f-4051-ac12-000000000002"


def _load_migration():
    """Import the 0015 module by path: its name starts with a digit."""
    spec = importlib.util.spec_from_file_location("_mig_0015_analyze", _MIGRATION_PATH)
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


async def _job(session_factory, job_id: str) -> AnalysisJob:
    async with session_factory() as s:
        return (
            await s.execute(select(AnalysisJob).where(AnalysisJob.job_id == job_id))
        ).scalar_one()


def _analysis_job(job_id: str, tenant: str, status: str, age_minutes: float) -> AnalysisJob:
    return AnalysisJob(
        job_id=job_id,
        tenant_id=tenant,
        status=status,
        created_at=_ago(age_minutes),
        updated_at=_ago(age_minutes),
    )


class TestAnalysisJobReaper:
    async def test_aged_processing_job_reaches_a_terminal_state(self, db_session_factory):
        """The whole point: ``analysis_jobs`` had NO reaper before 0015.

        Pins the two rows that must NOT move as well — a job that only just
        started, and one already terminal — because a reaper that fails live work
        is worse than no reaper.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._REAP_STALE_ANALYSIS_JOBS_FN)
        await _add(
            db_session_factory,
            _analysis_job("aj-stranded", _T1, "processing", 300),
            _analysis_job("aj-fresh", _T1, "processing", 1),
            _analysis_job("aj-done", _T2, "completed", 300),
        )

        async with db_session_factory() as s:
            reaped = await reap_stale_analysis_jobs(s, stale_after_seconds=3600)

        assert reaped == 1

        stranded = await _job(db_session_factory, "aj-stranded")
        assert stranded.status == "failed"
        assert "reaped" in (stranded.error or "")

        # CONTROL: a genuinely live job and an already-terminal one are untouched.
        assert (await _job(db_session_factory, "aj-fresh")).status == "processing"
        assert (await _job(db_session_factory, "aj-done")).status == "completed"

    async def test_the_swept_status_is_processing(self, db_session_factory):
        """``running``/``pending`` rows are not a thing here — and must not be invented.

        Guards the copy-paste-from-0010 direction and the read-the-ORM-default
        direction at once: widening the predicate to either literal turns this red.
        """
        migration = _load_migration()
        ddl = migration._REAP_STALE_ANALYSIS_JOBS_FN
        assert "status = 'processing'" in ddl
        assert "'running'" not in ddl
        assert "'pending'" not in ddl

        await _install(db_session_factory, ddl)
        await _add(
            db_session_factory,
            _analysis_job("aj-running", _T1, "running", 300),
            _analysis_job("aj-pending", _T1, "pending", 300),
        )

        async with db_session_factory() as s:
            assert await reap_stale_analysis_jobs(s, stale_after_seconds=0) == 0

        assert (await _job(db_session_factory, "aj-running")).status == "running"
        assert (await _job(db_session_factory, "aj-pending")).status == "pending"


class TestAnalysisJobRetention:
    async def test_prune_deletes_every_terminal_status_including_cancelled(
        self, db_session_factory, monkeypatch
    ):
        """``analysis_jobs`` had NO retention, and ``'cancelled'`` had none anywhere.

        ``cancel()`` is the only writer of ``'cancelled'`` in ``src/``, and before
        0015 no retention path in either domain matched it — so a cancelled job was
        as immortal as a stranded one.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._PRUNE_ANALYSIS_JOBS_FN)

        old, recent = 60 * 24 * 200, 1
        await _add(
            db_session_factory,
            _analysis_job("r-old-done", _T1, "completed", old),
            _analysis_job("r-old-failed", _T1, "failed", old),
            _analysis_job("r-old-cancelled", _T2, "cancelled", old),
            _analysis_job("r-old-live", _T1, "processing", old),
            _analysis_job("r-new-done", _T1, "completed", recent),
        )

        from src.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.retention, "job_retention_days", 90)

        async with db_session_factory() as s:
            deleted = await prune_old_analysis_jobs(s)

        assert deleted == 3

        async with db_session_factory() as s:
            surviving = sorted((await s.execute(select(AnalysisJob.job_id))).scalars().all())
        assert surviving == ["r-new-done", "r-old-live"]

    async def test_prune_never_deletes_a_non_terminal_row(self, db_session_factory):
        """A ``processing`` row is live work; age must never be enough to delete it."""
        migration = _load_migration()
        ddl = migration._PRUNE_ANALYSIS_JOBS_FN
        assert "status IN ('completed', 'failed', 'cancelled')" in ddl

        await _install(db_session_factory, ddl)
        await _add(
            db_session_factory, _analysis_job("r-ancient-live", _T1, "processing", 60 * 24 * 999)
        )

        async with db_session_factory() as s:
            deleted = (
                await s.execute(text("SELECT prune_analysis_jobs(:c)"), {"c": datetime.now(UTC)})
            ).scalar_one()
            await s.commit()

        assert deleted == 0
        assert (await _job(db_session_factory, "r-ancient-live")).status == "processing"
