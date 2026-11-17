"""RED-proof for scheduled-scan tenant PROVENANCE, end to end through Postgres.

THE DEFECT
----------
``run_scheduler`` polls every 60 seconds. It has no request scope, so it
established identity with ``set_identity_context(tenant_uuid(tid), None, None)``
— three positional arguments, ``tenant_source`` left at its ``None`` default.
``w2v_common``'s outbound hook treats ``None`` provenance as UNVOUCHED and
refuses to set ``X-Effective-Tenant-Id``, so **every** metered hop a scheduled
critique made (worker_management via ``WMClient``) raised
``UnvouchedTenantError``. Scheduled scans could not complete at all.

The tenant was never missing. The proof of where it came from was, and that
proof only exists in request scope. So it is captured at creation
(``routes/schedules.py``) and restored at the tick.

WHY THE LEGACY CASE FAILS CLOSED RATHER THAN GETTING A DEFAULT
---------------------------------------------------------------
``critique_schedules.tenant_source`` is NULLABLE with no server default, and a
NULL row raises :class:`ScheduleProvenanceMissingError` *before* ``create_job``.
Giving those rows a default would be a FORGED vouch: ``tenant_id`` is a plain
column writable by anything holding the tenant's DB grant, and a vouched source
is precisely the permission to spend that tenant's BYO provider credentials at a
metered peer. A forged one would grant that, to whoever wrote the row, forever,
on a timer. ``test_legacy_null_provenance_row_creates_no_job`` is the guard on
that, and it asserts on ``create_job`` never being awaited — no job row, no
worker_management spend — not merely on an exception being raised.

WHY THIS GOES THROUGH REAL POSTGRES
------------------------------------
The value has to survive a round trip the unit tests cannot fake: an ORM INSERT,
then the ``claim_due_critique_schedules`` SECURITY DEFINER resolver, whose
``RETURNS TABLE`` had to be widened (and therefore DROPped and re-created —
Postgres refuses to change a return type under ``CREATE OR REPLACE``). A mocked
claim would prove the Python and skip the exact hop that was broken.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text
from w2v_common.auth import clear_identity_context, set_identity_context
from w2v_common.tenant import VOUCHED_TENANT_SOURCES, TenantSource

from src.domains.critique.models import CritiqueSchedule
from src.domains.critique.routes.schedules import create_schedule
from src.domains.critique.schemas import CreateScheduleRequest
from src.domains.critique.services import scheduler as scheduler_mod
from src.domains.critique.services.scheduler import (
    CLAIM_DUE_SCHEDULES_DDL,
    CLAIM_DUE_SCHEDULES_DROP_DDL,
    ScheduleProvenanceMissingError,
    _fire_schedule,
    check_and_run_schedules,
)

pytestmark = pytest.mark.asyncio

TENANT = "7eed672d-a7ff-5f16-87d7-b6a493fee523"


@contextlib.contextmanager
def _scheduled_scans_enabled():
    with patch("src.config.get_settings") as settings:
        settings.return_value.features.scheduled_scans = True
        yield settings


@contextlib.contextmanager
def _request_scope(source: TenantSource | None):
    """Bind identity the way ``AuthMiddleware`` does for one inbound request."""
    set_identity_context(TENANT, "lic-1", "sub-1", tenant_source=source)
    try:
        yield
    finally:
        clear_identity_context()


def _request() -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = TENANT
    return request


async def _install_claim_fn(session_factory) -> None:
    async with session_factory() as s:
        await s.execute(text(CLAIM_DUE_SCHEDULES_DROP_DDL))
        await s.execute(text(CLAIM_DUE_SCHEDULES_DDL))
        await s.commit()


def _session_cm(session_factory):
    @contextlib.asynccontextmanager
    async def _cm():
        async with session_factory() as s:
            yield s
            await s.commit()

    return _cm


async def _drain_scheduled_tasks() -> None:
    """Await the fire-and-forget ``_process`` tasks the dispatcher spawned."""
    pending = list(scheduler_mod._scheduled_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TestProvenanceIsCapturedInRequestScope:
    async def test_create_schedule_persists_the_requests_tenant_source(
        self, db_session_factory
    ) -> None:
        """The creating request's provenance lands on the row, verbatim."""
        with _scheduled_scans_enabled(), _request_scope(TenantSource.LICENSE_TOKEN):
            async with db_session_factory() as db:
                await create_schedule(
                    CreateScheduleRequest(url="https://ok.example.com/"), _request(), db
                )

        async with db_session_factory() as s:
            row = (await s.execute(select(CritiqueSchedule))).scalars().one()
        assert row.tenant_source == TenantSource.LICENSE_TOKEN.value

    async def test_a_request_with_no_provenance_stores_null_not_a_substitute(
        self, db_session_factory
    ) -> None:
        """⛔ The anti-forgery assertion.

        A request that never established a provenance must leave the column
        NULL. If this ever goes green with a value in it, something is
        manufacturing a vouch — which is the whole vulnerability, not a fix for
        it. Asserted as ``is None`` rather than "not a vouched value" so that
        substituting an UNVOUCHED rung is caught too.
        """
        with _scheduled_scans_enabled(), _request_scope(None):
            async with db_session_factory() as db:
                await create_schedule(
                    CreateScheduleRequest(url="https://ok.example.com/"), _request(), db
                )

        async with db_session_factory() as s:
            row = (await s.execute(select(CritiqueSchedule))).scalars().one()
        assert row.tenant_source is None


class TestProvenanceRoundTripsIntoTheTick:
    async def test_created_in_request_scope_then_restored_at_the_tick(
        self, db_session_factory
    ) -> None:
        """The end-to-end proof: route → column → claim resolver → identity.

        ``set_identity_context`` is asserted on directly because it is the exact
        call that was wrong. Asserting only "a job was created" would stay green
        against the original bug — the job WAS created; it died later, at the
        first metered hop, where this test cannot see.
        """
        await _install_claim_fn(db_session_factory)

        # 1) Create through the route, in request scope, as a customer does.
        with _scheduled_scans_enabled(), _request_scope(TenantSource.JWT_CLAIM):
            async with db_session_factory() as db:
                created = await create_schedule(
                    CreateScheduleRequest(url="https://ok.example.com/"), _request(), db
                )

        # 2) Make it due, then let the scheduler claim it — no request scope,
        #    exactly as the 60s poll runs.
        async with db_session_factory() as s:
            await s.execute(
                text("UPDATE critique_schedules SET next_run_at = :past WHERE schedule_id = :sid"),
                {"past": datetime.now(UTC) - timedelta(hours=1), "sid": created.schedule_id},
            )
            await s.commit()

        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-1")
        service.process_job = AsyncMock(return_value=None)

        recorded = MagicMock()
        with (
            patch("src.shared.database.session.get_db_session", _session_cm(db_session_factory)),
            patch.object(scheduler_mod, "set_identity_context", recorded),
            patch.object(scheduler_mod, "bind_session_tenant", new_callable=AsyncMock),
        ):
            fired = await check_and_run_schedules(service)
            await _drain_scheduled_tasks()

        assert fired == 1
        recorded.assert_called_once()
        passed = recorded.call_args.kwargs["tenant_source"]
        assert passed is TenantSource.JWT_CLAIM
        # Non-vacuity: whatever was passed must actually be delegable, which is
        # the property the outbound hook checks. A test asserting only equality
        # would still pass if the ladder were later re-classified.
        assert passed in VOUCHED_TENANT_SOURCES

    async def test_claim_resolver_returns_the_column(self, db_session_factory) -> None:
        """The resolver's widened ``RETURNS TABLE`` actually carries the value.

        Pinned separately because this is the hop that needed a DROP + CREATE
        migration: a ``CREATE OR REPLACE`` silently leaves the OLD four-column
        signature in place, and then the tick reads ``row["tenant_source"]``
        off a row that never had one.
        """
        await _install_claim_fn(db_session_factory)
        async with db_session_factory() as s:
            s.add(
                CritiqueSchedule(
                    schedule_id="sch-claim",
                    tenant_id=TENANT,
                    tenant_source=TenantSource.LICENSE_TOKEN.value,
                    url="https://ok.example.com/",
                    categories=["seo"],
                    cron_expression="@daily",
                    enabled=True,
                    next_run_at=datetime.now(UTC) - timedelta(hours=1),
                    run_count=0,
                )
            )
            await s.commit()

        async with db_session_factory() as s:
            rows = (
                (await s.execute(scheduler_mod._CLAIM_SQL, {"now": datetime.now(UTC)}))
                .mappings()
                .all()
            )
            await s.commit()

        assert [r["tenant_source"] for r in rows] == [TenantSource.LICENSE_TOKEN.value]


class TestFailsClosedOnMissingProvenance:
    async def test_legacy_null_provenance_row_creates_no_job(self, db_session_factory) -> None:
        """A row written before this column existed must not fire. At all.

        The load-bearing assertion is ``create_job.assert_not_awaited()``: the
        point is not that an error is raised, it is that no job row and no
        worker_management spend happen. Seeded straight into the table, which is
        exactly the state every pre-existing production row is in.
        """
        await _install_claim_fn(db_session_factory)
        async with db_session_factory() as s:
            s.add(
                CritiqueSchedule(
                    schedule_id="legacy",
                    tenant_id=TENANT,
                    tenant_source=None,  # pre-migration row
                    url="https://ok.example.com/",
                    categories=["seo"],
                    cron_expression="@daily",
                    enabled=True,
                    next_run_at=datetime.now(UTC) - timedelta(hours=1),
                    run_count=0,
                )
            )
            await s.commit()

        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-should-not-exist")

        with (
            patch("src.shared.database.session.get_db_session", _session_cm(db_session_factory)),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock) as disp,
        ):
            fired = await check_and_run_schedules(service)

        assert fired == 0
        service.create_job.assert_not_awaited()
        disp.assert_not_awaited()

    @pytest.mark.parametrize(
        "stored",
        [
            None,
            "",
            "not-a-real-source",  # unrecognised label
            TenantSource.UNVOUCHED_HEADER.value,  # recognised but NOT vouched
            TenantSource.TRUSTED_NETWORK_DEFAULT.value,
            TenantSource.AUTH_DISABLED.value,
        ],
    )
    async def test_fire_schedule_refuses_every_unvouchable_value(self, stored) -> None:
        """``_fire_schedule`` raises before it touches the database at all.

        ``UNVOUCHED_HEADER`` is the case that matters most: it is a perfectly
        valid DATA SCOPE, so a check that merely asked "is this a known
        TenantSource?" would let it through and delegate a bare header as a
        spending identity.
        """
        service = AsyncMock()
        with pytest.raises(ScheduleProvenanceMissingError):
            await _fire_schedule(
                service,
                {
                    "schedule_id": "sch-x",
                    "tenant_id": TENANT,
                    "tenant_source": stored,
                    "url": "https://ok.example.com/",
                    "categories": ["seo"],
                },
            )
        service.create_job.assert_not_awaited()

    async def test_vouched_row_still_fires(self) -> None:
        """Non-vacuity: the guard has not simply disabled the scheduler."""
        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-ok")
        with (
            patch("src.shared.database.session.get_db_session"),
            patch.object(scheduler_mod, "bind_session_tenant", new_callable=AsyncMock),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock),
        ):
            job_id = await _fire_schedule(
                service,
                {
                    "schedule_id": "sch-ok",
                    "tenant_id": TENANT,
                    "tenant_source": TenantSource.LICENSE_TOKEN.value,
                    "url": "https://ok.example.com/",
                    "categories": ["seo"],
                },
            )
        assert job_id == "job-ok"
