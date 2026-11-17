"""RED-proof for the scheduled-scan URL as an SSRF sink.

WHAT WAS UNGUARDED
------------------
``POST /api/critique`` screens its scan target (``test_target_url_ssrf.py``
locks that down). ``POST /api/critique/schedules`` did not screen anything at
all, and neither did ``PUT`` on the same row. The scheduler then turns that row
into an ordinary critique job — same fetchers, same headless browser, same
scanners — on a timer.

So the guarded endpoint and the unguarded one reached the same place, and the
unguarded one reached it *repeatedly*. A tenant refused at ``POST
/api/critique`` could post the identical ``http://100.64.0.5/`` to
``/critique/schedules`` and have the platform dereference it every hour.
``features.scheduled_scans`` defaults to ``True`` (``src/config/infra.py``), so
this was live rather than latent.

``POST /critique/{id}/rerun`` had the near miss of the same shape: it
re-validated the propagated ``callback_url`` but copied ``original.url`` into
the new job untouched.

WHY DELIVERY IS SCREENED SEPARATELY FROM ADMISSION
--------------------------------------------------
:class:`TestSchedulerDeliveryTimeScreen` is not a redundant copy of the
admission tests. A schedule is *recurring*: admission happens once, delivery
happens forever. ``test_name_repointed_after_admission_is_refused_at_delivery``
is the case that cannot be closed at admission by construction — a host that
resolved to a public address when the row was written, and resolves into the
tailnet by the time the scan fires. Rows written before the admission guard
existed, and rows edited straight in the database, land in the same place.

WHY THESE ADDRESSES
-------------------
``ipaddress.ip_address("100.64.0.5").is_private`` is **False**, and so is
``.is_reserved``: CPython classifies RFC 6598 shared address space
(``100.64.0.0/10``) as neither. On this fleet that /10 is the Tailscale tailnet
— prod Postgres, the registry, the k3s control plane. See
``src/shared/security/url_validator.py``.

Hermetic: DNS comes from the autouse ``dns_stub`` fixture
(``tests/unit/domains/critique/conftest.py`` / ``tests/dns_stub.py``); no lookup
leaves the test runner.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from w2v_common.tenant import TenantSource

from src.domains.critique.models import CritiqueSchedule
from src.domains.critique.routes.jobs import rerun_critique
from src.domains.critique.routes.schedules import create_schedule, update_schedule
from src.domains.critique.schemas import (
    CreateScheduleRequest,
    RerunRequest,
    UpdateScheduleRequest,
)
from src.domains.critique.services import scheduler as scheduler_mod
from src.domains.critique.services.scheduler import (
    CLAIM_DUE_SCHEDULES_DDL,
    CLAIM_DUE_SCHEDULES_DROP_DDL,
    ScheduleTargetBlockedError,
    _fire_schedule,
    check_and_run_schedules,
)
from tests.dns_stub import TAILNET_ANSWER, DnsStub

# Estate-reachable URLs a tenant could put in a schedule today. Same vectors as
# ``test_target_url_ssrf.BLOCKED_TARGET_URLS`` -- the point of this file is that
# the two endpoints must agree, so they are screened against the same list.
BLOCKED_SCHEDULE_URLS = (
    "http://100.64.0.5/",  # bare-metal prod Postgres; is_private is False
    "http://[::ffff:100.64.0.5]/",  # ...the same address wearing an IPv6 hat
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://registry.internal/v2/_catalog",  # dies on the NAME, pre-resolver
    "http://localhost:5432/",  # loopback
    "file:///etc/passwd",  # not even HTTP
)

# Must keep working. 100.63.255.255 and 100.128.0.0 bracket the CGNAT /10: if
# either is refused the implementation is prefix-matching "100." rather than
# testing membership.
ALLOWED_SCHEDULE_URLS = (
    "https://public.example.com/",
    "https://8.8.8.8/",
    "https://100.63.255.255/",
    "https://100.128.0.0/",
)


def _mock_request() -> MagicMock:
    """A request carrying a tenant id and an un-called critique service."""
    request = MagicMock()
    request.state.tenant_id = "8e2d7e97-a59d-5711-8d27-6862fe268ca3"
    service = AsyncMock()
    service.create_job = AsyncMock(return_value="job-should-not-exist")
    request.app.state.critique_service = service
    return request


def _mock_db() -> AsyncMock:
    """An ``AsyncSession`` stand-in whose ``add`` is synchronous, as the real one is."""
    db = AsyncMock()
    db.add = MagicMock()
    return db


@contextlib.contextmanager
def _scheduled_scans_enabled():
    with patch("src.config.get_settings") as settings:
        settings.return_value.features.scheduled_scans = True
        yield settings


class TestCreateScheduleTargetUrl:
    """``POST /api/critique/schedules`` must refuse an estate-reachable target."""

    @pytest.mark.parametrize("url", BLOCKED_SCHEDULE_URLS)
    async def test_blocked_target_is_400_and_not_persisted(self, url: str) -> None:
        """Reject with 400 *before* the row is added to the session.

        ``db.add`` staying un-called is the load-bearing assertion. A 400 raised
        after the row was staged would still leave the URL in the unit of work,
        and a schedule that is refused only when it fires is a schedule that
        fires.
        """
        db = _mock_db()
        with _scheduled_scans_enabled(), pytest.raises(HTTPException) as exc:
            await create_schedule(CreateScheduleRequest(url=url), _mock_request(), db)
        assert exc.value.status_code == 400, exc.value
        db.add.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.parametrize("url", ALLOWED_SCHEDULE_URLS)
    async def test_public_target_is_still_accepted(self, url: str) -> None:
        """The guard must not have degraded into a blanket deny."""
        db = _mock_db()
        with _scheduled_scans_enabled():
            result = await create_schedule(CreateScheduleRequest(url=url), _mock_request(), db)
        assert result.url == url
        db.add.assert_called_once()

    async def test_name_resolving_into_the_tailnet_is_400(self, dns_stub: DnsStub) -> None:
        """A public-*looking* name whose A record points into the estate.

        The case a literal-IP screen misses entirely, and the reason the
        validator resolves rather than pattern-matching.
        """
        dns_stub.map("customer-site.example.com", TAILNET_ANSWER)
        db = _mock_db()
        with _scheduled_scans_enabled(), pytest.raises(HTTPException) as exc:
            await create_schedule(
                CreateScheduleRequest(url="https://customer-site.example.com/"),
                _mock_request(),
                db,
            )
        assert exc.value.status_code == 400
        db.add.assert_not_called()


def _existing_schedule_row() -> MagicMock:
    row = MagicMock()
    row.schedule_id = "sch-1"
    row.tenant_id = "8e2d7e97-a59d-5711-8d27-6862fe268ca3"
    row.url = "https://original.example.com/"
    row.categories = ["seo"]
    row.cron_expression = "@daily"
    row.enabled = True
    row.last_run_at = None
    row.next_run_at = datetime(2024, 1, 2, tzinfo=UTC)
    row.last_job_id = None
    row.run_count = 0
    row.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    return row


def _db_returning(row: MagicMock) -> AsyncMock:
    db = _mock_db()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    return db


class TestUpdateScheduleTargetUrl:
    """``PUT`` is a second write path to the same column, and needs the same screen."""

    @pytest.mark.parametrize("url", BLOCKED_SCHEDULE_URLS)
    async def test_blocked_target_is_400_and_row_unchanged(self, url: str) -> None:
        row = _existing_schedule_row()
        db = _db_returning(row)
        with pytest.raises(HTTPException) as exc:
            await update_schedule("sch-1", UpdateScheduleRequest(url=url), _mock_request(), db)
        assert exc.value.status_code == 400, exc.value
        assert row.url == "https://original.example.com/", (
            "the ORM object was mutated before the screen ran; a later rollback "
            "is not something this handler does"
        )
        db.commit.assert_not_awaited()

    async def test_public_target_is_still_applied(self) -> None:
        row = _existing_schedule_row()
        db = _db_returning(row)
        await update_schedule(
            "sch-1",
            UpdateScheduleRequest(url="https://public.example.com/"),
            _mock_request(),
            db,
        )
        assert row.url == "https://public.example.com/"
        db.commit.assert_awaited()

    async def test_other_fields_still_update_without_a_url(self) -> None:
        """A body with no ``url`` must not be dragged into the URL screen."""
        row = _existing_schedule_row()
        db = _db_returning(row)
        await update_schedule("sch-1", UpdateScheduleRequest(enabled=False), _mock_request(), db)
        assert row.enabled is False
        assert row.url == "https://original.example.com/"


class TestRerunPropagatedTargetUrl:
    """``POST /critique/{id}/rerun`` re-validated the callback URL but not the target."""

    @pytest.mark.parametrize("url", BLOCKED_SCHEDULE_URLS)
    async def test_blocked_original_target_is_400_and_no_job_created(self, url: str) -> None:
        """A job persisted before the admission guard must not be rerunnable."""
        request = _mock_request()
        original = MagicMock()
        original.url = url
        original.callback_url = None
        original.categories = ["seo"]
        original.max_pages = 1
        original.instructions = None
        original.github_repo = None
        request.app.state.critique_service.get_job = AsyncMock(return_value=original)

        with pytest.raises(HTTPException) as exc:
            await rerun_critique("job-1", RerunRequest(), request, AsyncMock())
        assert exc.value.status_code == 400, exc.value
        request.app.state.critique_service.create_job.assert_not_called()


async def _seed(
    session_factory,
    *,
    schedule_id: str,
    tenant_id: str,
    url: str,
    due: bool = True,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(
            CritiqueSchedule(
                schedule_id=schedule_id,
                tenant_id=tenant_id,
                tenant_source=TenantSource.LICENSE_TOKEN.value,
                url=url,
                categories=["seo"],
                cron_expression="@daily",
                enabled=True,
                next_run_at=now - timedelta(hours=1) if due else now + timedelta(hours=1),
                run_count=0,
            )
        )
        await s.commit()


class TestSchedulerDeliveryTimeScreen:
    """Admission and delivery resolve independently. Both have to hold."""

    async def test_fire_schedule_refuses_before_creating_a_job(self) -> None:
        """``_fire_schedule`` screens before it touches the database at all."""
        service = AsyncMock()
        with pytest.raises(ScheduleTargetBlockedError):
            await _fire_schedule(
                service,
                {
                    "schedule_id": "sch-x",
                    "tenant_id": "7eed672d-a7ff-5f16-87d7-b6a493fee523",
                    # Vouched, so the refusal below can only be the SSRF screen
                    # — this test must keep proving the URL guard, not the
                    # provenance guard that now sits behind it.
                    "tenant_source": TenantSource.LICENSE_TOKEN.value,
                    "url": "http://100.64.0.5/",
                    "categories": ["seo"],
                },
            )
        service.create_job.assert_not_called()

    async def test_row_written_before_the_guard_never_fires(self, db_session_factory) -> None:
        """The pre-existing-row case, end to end against real Postgres.

        Both rows are seeded straight into the table — which is exactly the
        state a row written before the admission guard (or edited in the DB) is
        in. The tick must fire the public one and refuse the tailnet one, and
        keep going: one poisoned schedule must not stop the sweep.
        """
        async with db_session_factory() as s:
            await s.execute(text(CLAIM_DUE_SCHEDULES_DROP_DDL))
            await s.execute(text(CLAIM_DUE_SCHEDULES_DDL))
            await s.commit()
        await _seed(
            db_session_factory,
            schedule_id="legacy-bad",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="http://100.64.0.5/",
        )
        await _seed(
            db_session_factory,
            schedule_id="good",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://good.example.com/",
        )

        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-good")

        @contextlib.asynccontextmanager
        async def _cm():
            async with db_session_factory() as s:
                yield s
                await s.commit()

        with (
            patch("src.shared.database.session.get_db_session", _cm),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock),
        ):
            fired = await check_and_run_schedules(service)

        assert fired == 1, "the blocked schedule was fired, or the good one was not"
        assert [c.kwargs["url"] for c in service.create_job.await_args_list] == [
            "https://good.example.com/"
        ]

    async def test_name_repointed_after_admission_is_refused_at_delivery(
        self, db_session_factory, dns_stub: DnsStub
    ) -> None:
        """The case admission cannot close, and the reason this check exists.

        ``repointed.example.com`` was public when the schedule was created —
        admission would have let it through, correctly. By the time the scan
        fires, its A record answers with a tailnet address. Only a screen at
        delivery sees that.
        """
        async with db_session_factory() as s:
            await s.execute(text(CLAIM_DUE_SCHEDULES_DROP_DDL))
            await s.execute(text(CLAIM_DUE_SCHEDULES_DDL))
            await s.commit()
        await _seed(
            db_session_factory,
            schedule_id="repointed",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://repointed.example.com/",
        )

        dns_stub.map("repointed.example.com", TAILNET_ANSWER)

        # A real string, not a bare AsyncMock: without it, removing the guard
        # makes this test fail on "psycopg cannot adapt AsyncMock" rather than
        # on the job being created. A test that fails for the wrong reason
        # cannot tell "refused" from "tried and broke".
        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-repointed")

        @contextlib.asynccontextmanager
        async def _cm():
            async with db_session_factory() as s:
                yield s
                await s.commit()

        with (
            patch("src.shared.database.session.get_db_session", _cm),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock),
        ):
            fired = await check_and_run_schedules(service)

        assert fired == 0
        service.create_job.assert_not_called()

    async def test_public_schedule_still_fires(self, db_session_factory) -> None:
        """Non-vacuity: the delivery screen has not simply disabled the scheduler."""
        async with db_session_factory() as s:
            await s.execute(text(CLAIM_DUE_SCHEDULES_DROP_DDL))
            await s.execute(text(CLAIM_DUE_SCHEDULES_DDL))
            await s.commit()
        await _seed(
            db_session_factory,
            schedule_id="ok",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://ok.example.com/",
        )

        service = AsyncMock()
        service.create_job = AsyncMock(return_value="job-ok")

        @contextlib.asynccontextmanager
        async def _cm():
            async with db_session_factory() as s:
                yield s
                await s.commit()

        with (
            patch("src.shared.database.session.get_db_session", _cm),
            patch.object(scheduler_mod, "_dispatch_job_processing", new_callable=AsyncMock),
        ):
            fired = await check_and_run_schedules(service)

        assert fired == 1
        assert [c.kwargs["url"] for c in service.create_job.await_args_list] == [
            "https://ok.example.com/"
        ]
