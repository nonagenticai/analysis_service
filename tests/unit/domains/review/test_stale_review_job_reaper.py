"""Real-Postgres proof for the review-job reaper and the extended retention prune.

Every assertion here is on the ROW, never on a mock call count.

The single most likely way to ship a vacuous test in this file
--------------------------------------------------------------
``review_jobs`` and ``analyze_review_jobs`` hold ``'processing'``. ``critique_jobs``
holds ``'running'``. A reaper that copied 0010's predicate sweeps zero rows on
every tick forever and logs nothing wrong. So the reap tests seed rows that are
``'processing'`` and assert they STOP being ``'processing'`` — flipping the
predicate to ``'running'`` (or to any other literal) turns
``test_aged_processing_review_job_reaches_a_terminal_state`` red on the reaped
count and on both status assertions. Mutation-verified before this file landed.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from src.domains.analyze.review_job_models import AnalyzeReviewJob
from src.domains.review.models import ReviewBatch, ReviewJob
from src.domains.review.services.reaper import (
    REAP_STALE_REVIEW_JOBS_DDL,
    STRANDED_STATUS,
    reap_stale_review_jobs,
)

pytestmark = pytest.mark.asyncio

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "shared"
    / "database"
    / "migrations"
    / "versions"
    / "0014_review_reaper_retention.py"
)

# Tenants are a native ``uuid`` column (0013) — a non-uuid string does not fail a
# type check, it fails at the driver.
_T1 = "b1a4b0f8-1c2d-4e3f-8a90-000000000001"
_T2 = "b1a4b0f8-1c2d-4e3f-8a90-000000000002"


def _load_migration():
    """Import the 0014 module by path: its name starts with a digit."""
    spec = importlib.util.spec_from_file_location("_mig_0014", _MIGRATION_PATH)
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


async def _status(session_factory, model, id_col, value: str) -> str:
    async with session_factory() as s:
        return (await s.execute(select(model).where(id_col == value))).scalar_one().status


class TestReviewReaper:
    async def test_aged_processing_review_job_reaches_a_terminal_state(self, db_session_factory):
        """The whole point: a row nothing will ever finalize stops being ``processing``.

        Covers BOTH tables in one sweep, and pins the two things that must NOT
        move — a job that only just started, and one already terminal — because a
        reaper that fails live work is worse than no reaper.
        """
        await _install(db_session_factory, REAP_STALE_REVIEW_JOBS_DDL)
        await _add(
            db_session_factory,
            ReviewJob(
                job_id="rj-stranded",
                tenant_id=_T1,
                status="processing",
                created_at=_ago(300),
                updated_at=_ago(300),
            ),
            ReviewJob(
                job_id="rj-fresh",
                tenant_id=_T1,
                status="processing",
                created_at=_ago(1),
                updated_at=_ago(1),
            ),
            ReviewJob(
                job_id="rj-done",
                tenant_id=_T2,
                status="completed",
                created_at=_ago(300),
                updated_at=_ago(300),
            ),
            AnalyzeReviewJob(
                review_job_id="arj-stranded",
                tenant_id=_T1,
                status="processing",
                created_at=_ago(300),
                updated_at=_ago(300),
            ),
            AnalyzeReviewJob(
                review_job_id="arj-fresh",
                tenant_id=_T2,
                status="processing",
                created_at=_ago(1),
                updated_at=_ago(1),
            ),
        )

        async with db_session_factory() as s:
            reaped = await reap_stale_review_jobs(s, stale_after_seconds=7200)

        assert reaped == {"review_jobs": 1, "analyze_review_jobs": 1}

        async with db_session_factory() as s:
            stranded = (
                await s.execute(select(ReviewJob).where(ReviewJob.job_id == "rj-stranded"))
            ).scalar_one()
        assert stranded.status == "failed"
        assert "reaped" in (stranded.error or "")

        assert await _status(db_session_factory, ReviewJob, ReviewJob.job_id, "rj-fresh") == (
            "processing"
        )
        assert await _status(db_session_factory, ReviewJob, ReviewJob.job_id, "rj-done") == (
            "completed"
        )
        assert (
            await _status(
                db_session_factory,
                AnalyzeReviewJob,
                AnalyzeReviewJob.review_job_id,
                "arj-stranded",
            )
            == "failed"
        )
        assert (
            await _status(
                db_session_factory, AnalyzeReviewJob, AnalyzeReviewJob.review_job_id, "arj-fresh"
            )
            == "processing"
        )

    async def test_the_swept_status_is_processing_not_running(self, db_session_factory):
        """A ``running`` row in these tables is not a thing — and must not be invented.

        Guards the copy-paste-from-0010 direction explicitly: if the predicate were
        widened to also match ``running``, this row would be reaped and the test
        goes red. ``STRANDED_STATUS`` is asserted so the constant and the DDL
        cannot disagree about what is being swept.
        """
        assert STRANDED_STATUS == "processing"
        assert "status = 'processing'" in REAP_STALE_REVIEW_JOBS_DDL
        assert "status = 'running'" not in REAP_STALE_REVIEW_JOBS_DDL

        await _install(db_session_factory, REAP_STALE_REVIEW_JOBS_DDL)
        await _add(
            db_session_factory,
            ReviewJob(
                job_id="rj-running",
                tenant_id=_T1,
                status="running",
                created_at=_ago(300),
                updated_at=_ago(300),
            ),
        )

        async with db_session_factory() as s:
            reaped = await reap_stale_review_jobs(s, stale_after_seconds=0)

        assert reaped == {"review_jobs": 0, "analyze_review_jobs": 0}
        assert await _status(db_session_factory, ReviewJob, ReviewJob.job_id, "rj-running") == (
            "running"
        )

    async def test_migration_ddl_matches_the_module_constant(self):
        """0014 writes the function body out literally; it must still mean the module's.

        A migration is a historical record, so it does not import the live constant
        — but the runtime and the tests install the module's copy, so silent drift
        between the two would mean prod runs a body no test ever exercised. If you
        change the function body, add a NEW migration and repoint this test at it.
        """
        literal = _load_migration()._REAP_STALE_REVIEW_JOBS_FN
        normalize = lambda sql: " ".join(sql.split())  # noqa: E731
        assert normalize(literal) == normalize(REAP_STALE_REVIEW_JOBS_DDL)


class TestExtendedRetentionPrune:
    async def test_prune_covers_all_three_review_tables(self, db_session_factory):
        """``analyze_review_jobs`` and ``review_batches`` had NO retention at all.

        Terminal rows past the cutoff go, in every one of the three tables; live
        rows and recent rows stay. ``'partial'`` is asserted where it actually
        exists — on a BATCH, never on a job.
        """
        migration = _load_migration()
        await _install(db_session_factory, migration._PRUNE_REVIEW_FN_V2)

        old, recent = _ago(60 * 100), _ago(1)
        await _add(
            db_session_factory,
            ReviewJob(job_id="p-old-done", tenant_id=_T1, status="completed", updated_at=old),
            ReviewJob(job_id="p-old-failed", tenant_id=_T1, status="failed", updated_at=old),
            ReviewJob(job_id="p-old-live", tenant_id=_T1, status="processing", updated_at=old),
            ReviewJob(job_id="p-new-done", tenant_id=_T1, status="completed", updated_at=recent),
            AnalyzeReviewJob(
                review_job_id="p-arj-old-done", tenant_id=_T1, status="completed", updated_at=old
            ),
            AnalyzeReviewJob(
                review_job_id="p-arj-old-live", tenant_id=_T1, status="processing", updated_at=old
            ),
            ReviewBatch(
                batch_id="p-b-old-partial", tenant_id=_T1, status="partial", completed_at=old
            ),
            ReviewBatch(
                batch_id="p-b-old-done", tenant_id=_T1, status="completed", completed_at=old
            ),
            ReviewBatch(batch_id="p-b-live", tenant_id=_T1, status="processing", completed_at=None),
        )

        cutoff = _ago(60 * 72)
        async with db_session_factory() as s:
            deleted = (
                await s.execute(text("SELECT prune_review_jobs(:c)"), {"c": cutoff})
            ).scalar_one()
            await s.commit()

        # 2 review_jobs + 1 analyze_review_jobs + 2 review_batches
        assert deleted == 5

        async with db_session_factory() as s:
            assert sorted((await s.execute(select(ReviewJob.job_id))).scalars().all()) == [
                "p-new-done",
                "p-old-live",
            ]
            assert (await s.execute(select(AnalyzeReviewJob.review_job_id))).scalars().all() == [
                "p-arj-old-live"
            ]
            assert (await s.execute(select(ReviewBatch.batch_id))).scalars().all() == ["p-b-live"]
