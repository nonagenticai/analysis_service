"""Real-Postgres proof that stranded ``running`` critique jobs reach a terminal state.

Both halves of the stranding defect are covered here, and both assert on the ROW,
never on a mock call count:

1. :func:`reap_stale_jobs` — the ``reap_stale_critique_jobs`` SECURITY DEFINER
   resolver actually flips an aged ``running`` row to ``failed`` (and leaves a
   fresh one and an already-terminal one alone).
2. ``routes/_helpers._process_job_background`` — cancelling the background task
   (what a pod rollout does to every task in ``asgi._shutdown``) finalizes the
   row as ``failed`` instead of abandoning it at ``running``, and still
   propagates the cancellation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from src.domains.critique.models import CritiqueJob
from src.domains.critique.routes import _helpers
from src.domains.critique.services.reaper import (
    REAP_STALE_CRITIQUE_JOBS_DDL,
    reap_stale_jobs,
)

pytestmark = pytest.mark.asyncio


async def _install_reap_fn(session_factory) -> None:
    async with session_factory() as s:
        await s.execute(text(REAP_STALE_CRITIQUE_JOBS_DDL))
        await s.commit()


async def _seed_job(
    session_factory,
    *,
    job_id: str,
    tenant_id: str,
    status: str,
    started_minutes_ago: float | None,
) -> None:
    started = (
        None
        if started_minutes_ago is None
        else datetime.now(UTC) - timedelta(minutes=started_minutes_ago)
    )
    async with session_factory() as s:
        s.add(
            CritiqueJob(
                job_id=job_id,
                tenant_id=tenant_id,
                url=f"https://{job_id}.example.com",
                status=status,
                started_at=started,
            )
        )
        await s.commit()


async def _load(session_factory, job_id: str) -> CritiqueJob:
    async with session_factory() as s:
        return (
            await s.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
        ).scalar_one()


class TestReaper:
    async def test_aged_running_job_reaches_a_terminal_state(self, db_session_factory):
        """The whole point: a row nothing will ever finalize stops being ``running``.

        Also pins the two things that must NOT move — a job that only just
        started, and one that already reached a terminal state — because a reaper
        that fails live work is worse than no reaper.
        """
        await _install_reap_fn(db_session_factory)
        await _seed_job(
            db_session_factory,
            job_id="reap-stranded",
            tenant_id="ea7512e0-cb04-59f9-b7f1-ee799e1762fb",
            status="running",
            started_minutes_ago=180,
        )
        await _seed_job(
            db_session_factory,
            job_id="reap-fresh",
            tenant_id="c88ee342-11e3-5105-9a23-68e085a071b5",
            status="running",
            started_minutes_ago=1,
        )
        await _seed_job(
            db_session_factory,
            job_id="reap-done",
            tenant_id="7e90a845-c505-57b3-bc10-4a139b6d6c03",
            status="completed",
            started_minutes_ago=180,
        )

        async with db_session_factory() as s:
            reaped = await reap_stale_jobs(s, stale_after_seconds=3600)

        assert reaped == 1

        stranded = await _load(db_session_factory, "reap-stranded")
        assert stranded.status == "failed"
        assert stranded.completed_at is not None
        assert "reaped" in (stranded.error_message or "")

        assert (await _load(db_session_factory, "reap-fresh")).status == "running"
        assert (await _load(db_session_factory, "reap-done")).status == "completed"

    async def test_running_job_with_no_started_at_still_ages_out(self, db_session_factory):
        """``COALESCE(started_at, created_at)`` — a row that never recorded a start
        is exactly the kind most likely to be broken; it must not be invisible."""
        await _install_reap_fn(db_session_factory)
        await _seed_job(
            db_session_factory,
            job_id="reap-nostart",
            tenant_id="81261fc0-13b0-5b10-af0b-c947684c9e17",
            status="running",
            started_minutes_ago=None,
        )

        async with db_session_factory() as s:
            # created_at is `now()`, so a zero-second threshold is what makes an
            # unstarted row reachable at all within a test.
            reaped = await reap_stale_jobs(s, stale_after_seconds=0)

        assert reaped >= 1
        assert (await _load(db_session_factory, "reap-nostart")).status == "failed"


class TestCancelledBackgroundJobIsFinalized:
    async def test_cancel_marks_the_row_failed_and_reraises(self, db_session_factory, monkeypatch):
        """A rollout cancels the task; the row must not be left ``running``.

        This is the defect: ``asyncio.CancelledError`` is a BaseException, so the
        route helper's ``except Exception`` did not catch it, ``mark_failed``
        never ran, and the job stayed ``running`` while the orchestrator polled
        it forever.
        """
        import src.shared.database.session as session_mod

        monkeypatch.setattr(session_mod, "get_db_session", db_session_factory)

        await _seed_job(
            db_session_factory,
            job_id="cancel-me",
            tenant_id="72339add-28df-564e-99a5-fb0175b37826",
            status="running",
            started_minutes_ago=1,
        )

        started = asyncio.Event()

        class _HangingService:
            async def process_job(self, db, job_id, tenant_id, **kwargs):
                started.set()
                await asyncio.Event().wait()  # never completes

            async def mark_failed(self, db, job_id, error, *, tenant_id):
                job = (
                    await db.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
                ).scalar_one()
                job.status = "failed"
                job.error_message = error
                job.completed_at = datetime.now(UTC)
                await db.commit()

        task = asyncio.create_task(
            _helpers._process_job_background(
                _HangingService(),  # ty: ignore[invalid-argument-type]
                "cancel-me",
                "t-cancel",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        job = await _load(db_session_factory, "cancel-me")
        assert job.status == "failed"
        assert "CancelledError" in (job.error_message or "")

    async def test_write_survives_a_cancel_landing_mid_write(self, db_session_factory, monkeypatch):
        """The absorb-and-retry, proven where a bare ``await`` would lose the write.

        The cancellation arrives while the UPDATE is already in flight. A plain
        ``await self._mark_failed(...)`` would take the CancelledError there and
        abandon the row at ``running``; the shielded, bounded retry goes back to
        waiting and the row still reaches ``failed``.
        """
        import src.shared.database.session as session_mod

        monkeypatch.setattr(session_mod, "get_db_session", db_session_factory)

        await _seed_job(
            db_session_factory,
            job_id="cancel-mid-write",
            tenant_id="6d00a416-983a-57a4-bed5-d96602e35ade",
            status="running",
            started_minutes_ago=1,
        )

        in_write = asyncio.Event()
        release = asyncio.Event()

        class _SlowService:
            async def mark_failed(self, db, job_id, error, *, tenant_id):
                in_write.set()
                await release.wait()
                job = (
                    await db.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
                ).scalar_one()
                job.status = "failed"
                job.error_message = error
                job.completed_at = datetime.now(UTC)
                await db.commit()

        task = asyncio.create_task(
            _helpers._mark_failed_during_cancel(
                _SlowService(),  # ty: ignore[invalid-argument-type]
                "cancel-mid-write",
                "t-midwrite",
                "CancelledError: job cancelled before completion (service shutdown)",
            )
        )
        await asyncio.wait_for(in_write.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)  # let the cancellation be delivered and absorbed
        release.set()
        await asyncio.wait_for(task, timeout=5)

        job = await _load(db_session_factory, "cancel-mid-write")
        assert job.status == "failed"
