"""Real-Postgres tests for the atomic, multi-replica-safe schedule claim.

These exercise ``claim_due_critique_schedules`` (SECURITY DEFINER, FOR UPDATE
SKIP LOCKED) and ``check_and_run_schedules`` against the CI Postgres, proving:

* two concurrent "replicas" claim each due schedule exactly ONCE (no
  duplicate-cost double-fire), and
* a schedule fired on one tick is not re-fired on the next (``next_run_at`` is
  bumped inside the claim), across multiple tenants.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, text
from w2v_common.tenant import TenantSource

from src.domains.critique.models import CritiqueSchedule
from src.domains.critique.services import scheduler as scheduler_mod
from src.domains.critique.services.scheduler import (
    CLAIM_DUE_SCHEDULES_DDL,
    CLAIM_DUE_SCHEDULES_DROP_DDL,
    check_and_run_schedules,
)

pytestmark = pytest.mark.asyncio


async def _install_claim_fn(session_factory) -> None:
    async with session_factory() as s:
        await s.execute(text(CLAIM_DUE_SCHEDULES_DROP_DDL))
        await s.execute(text(CLAIM_DUE_SCHEDULES_DDL))
        await s.commit()


async def _seed_schedule(session_factory, *, schedule_id: str, tenant_id: str, due: bool) -> None:
    now = datetime.now(UTC)
    next_run = now - timedelta(hours=1) if due else now + timedelta(hours=1)
    async with session_factory() as s:
        s.add(
            CritiqueSchedule(
                schedule_id=schedule_id,
                tenant_id=tenant_id,
                # Vouched provenance, as a row created through the API in
                # request scope now carries. Without it the tick fails closed
                # (``ScheduleProvenanceMissingError``) and nothing fires —
                # which is a separate invariant, proven in
                # ``test_schedule_tenant_source_provenance.py``.
                tenant_source=TenantSource.LICENSE_TOKEN.value,
                url=f"https://{schedule_id}.example.com",
                categories=["seo"],
                cron_expression="@daily",
                enabled=True,
                next_run_at=next_run,
                run_count=0,
            )
        )
        await s.commit()


class TestAtomicClaim:
    async def test_claim_is_single_shot(self, db_session_factory):
        """A due schedule is claimed once and cannot be re-claimed.

        This is the anti-double-fire invariant the ``UPDATE ... WHERE id IN
        (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`` claim provides across
        replicas: whoever runs it first takes the row and bumps ``next_run_at``
        to the future in the same statement, so any other replica/tick finds
        nothing due. (Asserted with one open connection at a time so it stays
        deterministic under the parallel test runner instead of racing two live
        transactions.)
        """
        await _install_claim_fn(db_session_factory)
        await _seed_schedule(
            db_session_factory,
            schedule_id="s1",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            due=True,
        )
        await _seed_schedule(
            db_session_factory,
            schedule_id="s2",
            tenant_id="88a7e767-d967-5ff4-8c14-dad0fa6af6f7",
            due=True,
        )

        claim_sql = text("SELECT schedule_id, tenant_id FROM claim_due_critique_schedules(:now)")

        # First claim ("replica A" tick) takes both due rows.
        async with db_session_factory() as a:
            rows_a = (await a.execute(claim_sql, {"now": datetime.now(UTC)})).mappings().all()
            await a.commit()
        assert {r["schedule_id"] for r in rows_a} == {"s1", "s2"}

        # A second claim ("replica B" / next tick) finds nothing — the rows were
        # already claimed and their next_run_at bumped to the future.
        async with db_session_factory() as b:
            rows_b = (await b.execute(claim_sql, {"now": datetime.now(UTC)})).mappings().all()
            await b.commit()
        assert rows_b == []

        # Each schedule was bumped forward exactly once (run_count incremented once).
        async with db_session_factory() as c:
            bumped = (
                await c.execute(
                    text(
                        "SELECT count(*) FROM critique_schedules "
                        "WHERE next_run_at > now() AND run_count = 1"
                    )
                )
            ).scalar()
        assert bumped == 2

    async def test_claim_skips_disabled_and_future(self, db_session_factory):
        await _install_claim_fn(db_session_factory)
        await _seed_schedule(
            db_session_factory,
            schedule_id="due",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            due=True,
        )
        await _seed_schedule(
            db_session_factory,
            schedule_id="future",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            due=False,
        )

        claim_sql = text("SELECT schedule_id FROM claim_due_critique_schedules(:now)")
        async with db_session_factory() as s:
            rows = (await s.execute(claim_sql, {"now": datetime.now(UTC)})).mappings().all()
            await s.commit()
        assert {r["schedule_id"] for r in rows} == {"due"}


class TestCheckAndRunSchedules:
    async def test_fires_each_due_once_across_ticks(self, db_session_factory):
        """Each due schedule fires exactly once; a second tick fires nothing."""
        await _install_claim_fn(db_session_factory)
        await _seed_schedule(
            db_session_factory,
            schedule_id="a",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            due=True,
        )
        await _seed_schedule(
            db_session_factory,
            schedule_id="b",
            tenant_id="88a7e767-d967-5ff4-8c14-dad0fa6af6f7",
            due=True,
        )

        service = AsyncMock()
        service.create_job = AsyncMock(side_effect=["job-a", "job-b", "job-c", "job-d"])

        @contextlib.asynccontextmanager
        async def _cm():
            async with db_session_factory() as s:
                yield s
                await s.commit()

        with (
            patch("src.shared.database.session.get_db_session", _cm),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock),
        ):
            first = await check_and_run_schedules(service)
            second = await check_and_run_schedules(service)

        assert first == 2
        assert second == 0
        assert service.create_job.await_count == 2

        # Jobs were created for both tenants with their own URL.
        fired = {
            call.kwargs["tenant_id"]: call.kwargs["url"]
            for call in service.create_job.await_args_list
        }
        assert fired == {
            "7eed672d-a7ff-5f16-87d7-b6a493fee523": "https://a.example.com",
            "88a7e767-d967-5ff4-8c14-dad0fa6af6f7": "https://b.example.com",
        }

        # last_job_id was recorded on each claimed schedule.
        async with db_session_factory() as s:
            schedules = (await s.execute(select(CritiqueSchedule))).scalars().all()
        assert {sch.last_job_id for sch in schedules} == {"job-a", "job-b"}
