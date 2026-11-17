"""Scheduled recurring scan service.

Multi-replica safety: ``run_scheduler`` runs on EVERY replica. If each replica
independently SELECTed due schedules and fired them, N replicas would create N
duplicate critique jobs per tick — duplicate LLM/WM cost. The claim is therefore
made atomic through the ``claim_due_critique_schedules`` SECURITY DEFINER
resolver, which does ``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
LOCKED)`` in a single statement: each due schedule is claimed by exactly one
replica (the losers' ``SKIP LOCKED`` skips the locked rows, and the bumped
``next_run_at`` excludes an already-claimed row from the next tick).

The resolver is also SECURITY DEFINER for a second reason: the sweep is
cross-tenant, but under ``TENANT_RLS_MODE=enforce`` the runtime connects as the
restricted non-owner role with no tenant GUC, so a plain ``SELECT`` on
``critique_schedules`` would fail-closed to 0 rows. The owner-owned resolver
bypasses ENABLE (never FORCE) and returns rows for every tenant, which the
per-schedule job creation then binds before writing (see ``retention.py`` /
``prune_critique_data`` for the same pattern).

SSRF: admission and delivery are screened independently
-------------------------------------------------------
``routes/schedules.py`` screens the URL when the row is written. That is
necessary but it is not sufficient here, and this is the one place in the repo
where the gap is *structural* rather than hypothetical:

* A schedule is **recurring**. Admission happens once; delivery happens every
  hour, forever. A name that resolved to a public address when the row was
  created can be repointed at ``100.64.0.5`` (prod Postgres) the next day and
  every subsequent tick will dereference it. No admission-time check, however
  good, can speak for a fetch that has not happened yet.
* Rows written **before** the admission guard existed are still in the table,
  still enabled, and still due.
* The column is writable by anything holding the tenant's DB grant; the HTTP
  route is not the only way bytes get in there.

So :func:`_fire_schedule` re-runs the full validator -- the same
``validate_callback_url``, not a weaker structural variant -- immediately before
the job is created, and a refusal skips that tick. It runs in a worker thread
because the validator resolves DNS and this loop shares its event loop with the
API server; a blocking ``getaddrinfo`` here would stall request handling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text, update
from w2v_common.auth import clear_identity_context, set_identity_context
from w2v_common.database.rls import bind_session_tenant
from w2v_common.tenant import VOUCHED_TENANT_SOURCES, TenantSource

from src.domains.critique.models import CritiqueSchedule
from src.shared.tenant import tenant_uuid

if TYPE_CHECKING:
    from src.domains.critique.services.critique_service import CritiqueService

logger = logging.getLogger(__name__)

_scheduled_tasks: set[asyncio.Task[None]] = set()

# MVP cron presets (expression -> timedelta). SINGLE SOURCE: the SQL interval
# CASE in the claim resolver is generated from this map (``_cron_interval_case``)
# so Python and SQL can never disagree about a preset's period.
CRON_PRESETS: dict[str, timedelta] = {
    "@hourly": timedelta(hours=1),
    "@every_6h": timedelta(hours=6),
    "@every_12h": timedelta(hours=12),
    "@daily": timedelta(days=1),
    "@weekly": timedelta(weeks=1),
}

# Period applied to an unknown cron expression (matches ``next_run_from_cron``).
_DEFAULT_DELTA: timedelta = timedelta(days=1)


class ScheduleTargetBlockedError(ValueError):
    """A stored schedule's URL failed the SSRF screen at firing time.

    Distinct from the generic failures :func:`check_and_run_schedules` already
    swallows so the caller can log it as what it is -- a refused target, not a
    broken scan -- without a stack trace, and so a test can assert the block
    rather than merely observing that no job appeared.
    """


class ScheduleProvenanceMissingError(ValueError):
    """A claimed schedule carries no provenance this service can stand behind.

    The scheduler tick has no request scope, so the ONLY thing that can say how
    a scheduled scan's tenant was established is the ``tenant_source`` recorded
    on the row when it was created. When that is NULL (a row written before the
    column existed, or by a request that never had a provenance), unrecognised,
    or a rung that is not in ``VOUCHED_TENANT_SOURCES``, this service cannot
    vouch for the tenant -- and every metered hop the resulting job makes
    (worker_management via WMClient) would try to DELEGATE it, unlocking that
    tenant's BYO provider credentials and billing them.

    ⛔ The fix for this error is NEVER to substitute a source. Handing the row
    ``VOUCHED_ONBEHALF`` (or any default) forges exactly the vouch the ladder
    exists to withhold: ``tenant_id`` is a plain column writable by anything
    holding the tenant's DB grant, so a forged vouch lets whoever wrote that row
    spend an arbitrary tenant's credentials, forever, on a timer. The row must be
    re-created through the API by a request that actually has a vouched tenant.

    Raised BEFORE ``create_job``, so a provenance-less schedule produces no job
    row and no worker_management spend at all -- rather than a job that dies at
    its first metered call with a ``running`` row left behind.
    """


def _vouched_source_or_raise(row: dict) -> TenantSource:
    """Resolve a claimed schedule's persisted provenance, or fail closed.

    Args:
        row: One claimed ``critique_schedules`` row (needs ``tenant_source``).

    Returns:
        The recorded :class:`TenantSource`, guaranteed to be in
        :data:`VOUCHED_TENANT_SOURCES`.

    Raises:
        ScheduleProvenanceMissingError: if the column is NULL/blank, holds a
            value that is not a ``TenantSource`` member, or names an unvouched
            rung.
    """
    raw = row.get("tenant_source")
    if not raw:
        raise ScheduleProvenanceMissingError(
            "schedule has no recorded tenant_source; it predates provenance capture "
            "or was created by a request that never established one. Refusing to fire "
            "-- an unvouched tenant may not be delegated to a metered peer."
        )
    try:
        source = TenantSource(raw)
    except ValueError as exc:
        # An unrecognised label is strictly worse than NULL: something wrote a
        # value this build does not understand. Never coerce it to a rung.
        raise ScheduleProvenanceMissingError(
            f"schedule records an unrecognised tenant_source {raw!r}"
        ) from exc
    if source not in VOUCHED_TENANT_SOURCES:
        raise ScheduleProvenanceMissingError(
            f"schedule's tenant_source {source.value!r} is not vouched; the tenant is a "
            "valid DATA SCOPE but was never proven, so it may not be spent as an IDENTITY"
        )
    return source


async def _ensure_schedule_target_allowed(url: str) -> None:
    """Re-screen a stored schedule URL immediately before it is dereferenced.

    Args:
        url: The ``url`` column of the claimed schedule row.

    Raises:
        ScheduleTargetBlockedError: if the URL is empty or fails the SSRF screen.
    """
    from src.shared.security.url_validator import validate_callback_url

    if not url:
        raise ScheduleTargetBlockedError("schedule has no URL")
    try:
        # asyncio.to_thread: the validator calls socket.getaddrinfo, and this
        # loop is the API server's loop. See the module docstring.
        await asyncio.to_thread(validate_callback_url, url)
    except ValueError as exc:
        raise ScheduleTargetBlockedError(f"refusing to scan unsafe URL '{url}': {exc}") from exc


def next_run_from_cron(cron_expression: str, last_run: datetime | None = None) -> datetime:
    """Calculate next run time from a cron preset expression."""
    delta = CRON_PRESETS.get(cron_expression, _DEFAULT_DELTA)
    base = last_run or datetime.now(UTC)
    return base + delta


def _cron_interval_case(column: str) -> str:
    """Render a SQL ``CASE`` mapping ``cron_expression`` to a Postgres interval.

    Generated from :data:`CRON_PRESETS` so the resolver's ``next_run_at`` bump
    uses the exact same periods as :func:`next_run_from_cron`.
    """
    whens = "\n".join(
        f"        WHEN '{expr}' THEN interval '{int(delta.total_seconds())} seconds'"
        for expr, delta in CRON_PRESETS.items()
    )
    return (
        f"CASE {column}\n{whens}\n"
        f"        ELSE interval '{int(_DEFAULT_DELTA.total_seconds())} seconds'\n"
        f"      END"
    )


# SECURITY DEFINER resolver: atomic, cross-tenant, single-replica claim of due
# schedules. Owner-owned → bypasses RLS ENABLE (never FORCE). The interval CASE is
# rendered via ``str.replace`` from the hardcoded CRON_PRESETS constant (never an
# f-string over user input), so this DDL carries no SQL-injection surface.
_CLAIM_DUE_SCHEDULES_TEMPLATE = """
CREATE OR REPLACE FUNCTION claim_due_critique_schedules(p_now timestamptz)
  RETURNS TABLE(schedule_id varchar, tenant_id uuid, tenant_source varchar, url text, categories jsonb)
  LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    UPDATE critique_schedules s
    SET last_run_at = p_now,
        run_count = COALESCE(s.run_count, 0) + 1,
        next_run_at = p_now + (__CRON_INTERVAL_CASE__)
    WHERE s.id IN (
      SELECT id FROM critique_schedules
      WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= p_now
      FOR UPDATE SKIP LOCKED
    )
    RETURNING s.schedule_id, s.tenant_id, s.tenant_source, s.url, s.categories;
  $fn$
"""

CLAIM_DUE_SCHEDULES_DDL: str = _CLAIM_DUE_SCHEDULES_TEMPLATE.replace(
    "__CRON_INTERVAL_CASE__", _cron_interval_case("s.cron_expression")
)

#: Must be executed BEFORE :data:`CLAIM_DUE_SCHEDULES_DDL` whenever the resolver
#: may already exist with a DIFFERENT shape. ``CREATE OR REPLACE FUNCTION``
#: cannot change a ``RETURNS TABLE`` — Postgres raises ``cannot change return
#: type of existing function`` — so the "or replace" above silently covers only
#: body edits, never signature ones. Adding ``tenant_source`` to the returned
#: columns is exactly such a change, and without this the install fails against
#: any database that still holds the previous four-column definition (migration
#: 0012 does the same DROP for the same reason).
CLAIM_DUE_SCHEDULES_DROP_DDL: str = (
    "DROP FUNCTION IF EXISTS claim_due_critique_schedules(timestamptz)"
)

# ``tenant_id::text``: migration 0013 made the column — and therefore the
# resolver's RETURNS TABLE — a native ``uuid``, and psycopg would hand this raw-SQL
# row back as a ``uuid.UUID`` object. Every consumer downstream of
# ``_fire_schedule`` expects a ``str`` (``create_job(tenant_id=...)``, the log
# fields, and ultimately ``set_config('app.current_tenant', $2, true)``, which is
# ``(text, text, bool)`` and raises ``UndefinedFunction`` on a uuid). The ORM path
# is already covered by ``PgUUID(as_uuid=False)`` on the models; this is the one
# raw-SQL site that selects ``tenant_id``, so it casts here for the same reason.
_CLAIM_SQL = text(
    "SELECT schedule_id, tenant_id::text AS tenant_id, tenant_source, url, categories "
    "FROM claim_due_critique_schedules(:now)"
)


async def _dispatch_job_processing(
    service: CritiqueService,
    job_id: str,
    tenant_id: str,
    tenant_source: TenantSource,
) -> None:
    """Fire-and-forget background processing of a freshly created critique job.

    Args:
        service: The critique service that owns ``process_job``.
        job_id: The freshly created job.
        tenant_id: The claimed schedule's tenant.
        tenant_source: The provenance restored from the schedule row. Required,
            not optional: this task is where the metered outbound calls actually
            happen, and ``None`` here is what made every scheduled critique fail
            with ``UnvouchedTenantError`` at its first worker_management hop.
    """
    from src.shared.database.session import get_db_session

    async def _process(svc: CritiqueService, jid: str, tid: str, src: TenantSource) -> None:
        # Identity ContextVar drives the after_begin RLS re-bind so the GUC
        # survives process_job's many commits (transaction-scoped set_config).
        #
        # ``tenant_source`` is passed KEYWORD and is the whole point of this
        # change. This call used to be ``set_identity_context(tenant_uuid(tid),
        # None, None)`` -- three positional args, no provenance -- so the
        # outbound hook saw a bound tenant that nobody had vouched for and
        # refused to delegate it. The value is the one the CREATING request
        # recorded, restored verbatim; it is never manufactured here.
        set_identity_context(tenant_uuid(tid), None, None, tenant_source=src)
        try:
            async with get_db_session() as process_db:
                await bind_session_tenant(process_db, tenant_uuid(tid))
                await svc.process_job(process_db, jid, tid)
        except asyncio.CancelledError:
            # Shutdown, not a failure. Must keep propagating.
            raise
        except Exception:
            # ⚠️ There was NO `except` here, and that made every failure on this
            # path INVISIBLE. The task is only referenced by
            # `task.add_done_callback(_scheduled_tasks.discard)`, so nothing ever
            # awaits it and nothing retrieves its exception: no log line, no
            # metric, and the job row left `running` forever. A cron-fired scan
            # that raised simply vanished.
            #
            # The HTTP path for the IDENTICAL work has always had one
            # (`routes/_helpers.py`), so this was a gap between two doors into the
            # same room, not a deliberate choice.
            #
            # `logger.exception` (not `.error`) so the traceback survives — the
            # whole point is that this was the only place it could ever appear.
            logger.exception(
                "Scheduled critique job %s FAILED (tenant=%s); row left for the "
                "reaper rather than silently wedged",
                jid,
                tid,
            )
        finally:
            clear_identity_context()

    task = asyncio.create_task(_process(service, job_id, tenant_id, tenant_source))
    _scheduled_tasks.add(task)
    task.add_done_callback(_scheduled_tasks.discard)


async def _fire_schedule(service: CritiqueService, row: dict) -> str:
    """Create the critique job for one claimed schedule and dispatch it (tenant-bound).

    Raises:
        ScheduleTargetBlockedError: if the stored URL fails the delivery-time
            SSRF screen. Raised *before* any job row is written, so a blocked
            schedule produces no job at all rather than one that fails later.
        ScheduleProvenanceMissingError: if the row carries no vouched
            ``tenant_source``. Also raised before any job row is written, for the
            same reason and with the same effect: no job, no worker_management
            spend.
    """
    from src.shared.database.session import get_db_session

    tenant_id = row["tenant_id"]
    schedule_id = row["schedule_id"]

    await _ensure_schedule_target_allowed(row["url"])

    # Fail closed BEFORE create_job. Deliberately ordered after the SSRF screen
    # so a blocked target still reports as blocked, and deliberately before the
    # session is opened so a provenance-less row costs nothing at all.
    tenant_source = _vouched_source_or_raise(row)

    async with get_db_session() as db:
        # Under enforce this INSERT is WITH CHECK'd against the tenant GUC.
        # 0.67.0: bind_session_tenant takes uuid.UUID | None. tenant_id comes
        # off the claimed schedule ROW, so it is a str here; tenant_uuid also
        # refuses the nil sentinel, which must never fire a scheduled scan.
        await bind_session_tenant(db, tenant_uuid(tenant_id))
        job_id = await service.create_job(
            db,
            tenant_id=tenant_id,
            url=row["url"],
            categories=row["categories"],
        )
        # Record the fired job on the (already-claimed) schedule row. The
        # restricted runtime role may UPDATE its own tenant's row once bound.
        await db.execute(
            update(CritiqueSchedule)
            .where(
                CritiqueSchedule.schedule_id == schedule_id,
                CritiqueSchedule.tenant_id == tenant_id,
            )
            .values(last_job_id=job_id)
        )

    await _dispatch_job_processing(service, job_id, tenant_id, tenant_source)
    return job_id


async def check_and_run_schedules(service: CritiqueService) -> int:
    """Atomically claim every due schedule and fire a critique job for each.

    Returns the number of jobs created on this tick. Safe to run concurrently on
    N replicas: each due schedule is claimed by exactly one caller.
    """
    from src.shared.database.session import get_db_session

    now = datetime.now(UTC)

    # 1) Atomic cross-tenant claim (bumps next_run_at inside the resolver).
    async with get_db_session() as claim_db:
        rows = [
            dict(m) for m in (await claim_db.execute(_CLAIM_SQL, {"now": now})).mappings().all()
        ]

    # 2) Create + dispatch a job per claimed schedule, in its own tenant-bound txn.
    jobs_created = 0
    for row in rows:
        try:
            job_id = await _fire_schedule(service, row)
            jobs_created += 1
            logger.info(
                "Scheduled scan fired: schedule=%s url=%s job=%s tenant=%s",
                row["schedule_id"],
                row["url"],
                job_id,
                row["tenant_id"],
            )
        except ScheduleTargetBlockedError as e:
            # A refused target is not a malfunction: log it as the security
            # event it is, without a traceback, and move on. The schedule stays
            # enabled — if the URL stops resolving into the estate it starts
            # working again, and if it does not, every tick says so.
            logger.warning(
                "Scheduled scan %s BLOCKED at delivery (tenant=%s): %s",
                row.get("schedule_id"),
                row.get("tenant_id"),
                e,
            )
        except ScheduleProvenanceMissingError as e:
            # Not a malfunction either: a row this service cannot vouch for. Log
            # it as the security event it is, without a traceback, and move on.
            # The schedule stays enabled but will never fire until it is
            # re-created by a request carrying a vouched tenant -- which is the
            # ONLY correct remedy. Do not "fix" this by writing a tenant_source.
            #
            # It does not flood: the claim resolver bumps next_run_at inside the
            # same UPDATE that returns the row, so a legacy row is claimed once
            # per its own cron period (@daily -> once a day), not once per 60s
            # tick.
            logger.warning(
                "Scheduled scan %s REFUSED for missing/unvouched provenance (tenant=%s): %s",
                row.get("schedule_id"),
                row.get("tenant_id"),
                e,
            )
        except Exception as e:
            # The schedule was already claimed (next_run_at bumped); a failure
            # here just skips this tick's run for that schedule — it retries next
            # period rather than firing twice.
            logger.error(
                "Failed to fire scheduled scan %s: %s",
                row.get("schedule_id"),
                e,
                exc_info=True,
            )

    return jobs_created


async def run_scheduler(service: CritiqueService) -> None:
    """Background loop that polls DB every 60s for due schedules."""
    from src.config import get_settings

    settings = get_settings()
    if not settings.features.scheduled_scans:
        logger.info("Scheduled scans are disabled")
        return

    logger.info("Scheduler started")

    while True:
        try:
            count = await check_and_run_schedules(service)
            if count:
                logger.info("Scheduler fired %d scans", count)
        except Exception as e:
            logger.error("Scheduler error: %s", e, exc_info=True)

        await asyncio.sleep(60)
