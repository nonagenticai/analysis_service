"""Stale-job reaper — the backstop for critique jobs stranded in ``running``.

The failure this exists to end
-----------------------------
A critique job is finalized in-band: ``process_job`` succeeds and the row goes
``completed``, or it raises and ``routes/_helpers.py`` marks it ``failed``. Both
paths need the process to still be alive. When it is not — a pod rollout, a
lifespan shutdown that cancels the background task, an OOM kill, a SIGKILL after
the termination grace period — no ``except`` block runs at all, and the row is
left at ``status='running'`` **forever**. Nothing ages it out: ``git grep
"stale\\|reconcile"`` over ``src/domains/critique`` returned nothing before this
module. That in-band finalizer is also the ONLY finalizer: the Redis job queue
that once looked like a second path was deleted as unreachable — it had no
producer anywhere in this repo, the w2v fleet or gitops — so no other code path
exists that could ever move a stranded row to a terminal state.

A row stuck ``running`` is not a cosmetic leak. The orchestrator polls a critique
job until it reaches a terminal state, so one stranded row is one poll loop that
never ends — it never fails over, never retries, never reports. The in-band
``CancelledError`` handling added alongside this module stops *creating* new
strandings; this loop clears the ones already there and the ones a hard kill will
always be able to create.

Why a SECURITY DEFINER function and not a plain ``UPDATE``
---------------------------------------------------------
In prod the runtime connects as the restricted, non-owner role under
``TENANT_RLS_MODE=enforce``, and every request binds ``app.current_tenant``. This
loop has no request and therefore no tenant GUC, while the sweep it must perform
is inherently cross-tenant. A plain ``UPDATE critique_jobs …`` here would be
RLS-scoped to an UNBOUND GUC — which does not raise. It reports ``UPDATE 0`` and
returns cleanly. A naive sweeper would look healthy in every log line and reap
nothing, forever.

``reap_stale_critique_jobs`` is therefore owned by the migration user and marked
SECURITY DEFINER, so its body bypasses RLS ENABLE (never FORCE) and sees every
tenant's rows — the same pattern as ``prune_critique_data`` (0005),
``prune_review_jobs`` (0005) and ``claim_due_critique_schedules`` (0007). EXECUTE
is revoked from PUBLIC by the migration.

The DDL lives next to the loop that calls it so migration 0010 and the tests
share one source of truth for the function name and body.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings

logger = logging.getLogger(__name__)

REAP_FN_NAME = "reap_stale_critique_jobs"

#: Persisted into ``error_message`` so an operator (and the orchestrator's error
#: surface) can tell a reaped job from one that failed on its own merits.
REAPED_ERROR_MESSAGE = (
    "reaped: job was left running with no live worker "
    "(pod rollout, shutdown or crash before it could be finalized)"
)

# Rendered with ``str.replace`` from module constants rather than an f-string
# (the same shape as ``scheduler.CLAIM_DUE_SCHEDULES_DDL``): nothing user-supplied
# reaches this DDL, and keeping the template a plain literal means the SQL body is
# readable as SQL. The apostrophe-doubling is here so an editor who adds one to the
# message does not silently break the migration.
#
# ``COALESCE(started_at, created_at)``: ``started_at`` is set when the worker picks
# the job up, but a row that went ``running`` without recording it must still age
# out — otherwise the exact rows most likely to be broken are the ones the reaper
# can never see.
_REAP_DDL_TEMPLATE = """
CREATE OR REPLACE FUNCTION __FN__(p_stale_after interval)
  RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE n integer;
  BEGIN
    UPDATE critique_jobs
       SET status = 'failed',
           error_message = '__MESSAGE__',
           completed_at = now()
     WHERE status = 'running'
       AND COALESCE(started_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
  END $fn$
"""

REAP_STALE_CRITIQUE_JOBS_DDL: str = _REAP_DDL_TEMPLATE.replace("__FN__", REAP_FN_NAME).replace(
    "__MESSAGE__", REAPED_ERROR_MESSAGE.replace("'", "''")
)

_REAP_SQL = text(f"SELECT {REAP_FN_NAME}(make_interval(secs => :secs))")

#: The SECOND stranded state, closed by migration 0015. ``create_job`` COMMITS the
#: row at ``'pending'`` before ``routes/jobs.py`` hands the work to
#: ``asyncio.create_task``, and ``_phase_mark_running`` only later moves it to
#: ``'running'``. A process that dies in that window leaves a committed
#: ``'pending'`` row matched by neither the reaper above (``status='running'``) nor
#: ``prune_critique_data`` (terminal statuses only) — immortal.
NEVER_STARTED_REAP_FN_NAME = "reap_never_started_critique_jobs"

_REAP_NEVER_STARTED_SQL = text(f"SELECT {NEVER_STARTED_REAP_FN_NAME}(make_interval(secs => :secs))")


async def reap_stale_jobs(db: AsyncSession, stale_after_seconds: float) -> int:
    """Move every job stuck in ``running`` past the threshold to ``failed``.

    Returns the number of rows reaped. Cross-tenant via the SECURITY DEFINER
    resolver — see the module docstring for why a plain UPDATE would silently
    reap nothing under enforce.
    """
    reaped = int((await db.execute(_REAP_SQL, {"secs": stale_after_seconds})).scalar_one())
    await db.commit()
    return reaped


async def reap_never_started_jobs(db: AsyncSession, stale_after_seconds: float) -> int:
    """Move every job still ``pending`` (never started) past the threshold to ``failed``.

    ⚠️ ``stale_after_seconds`` here is a DIFFERENT, deliberately much longer budget
    than :func:`reap_stale_jobs` takes, and the two must never be collapsed into
    one. ``'running'`` is unambiguously a live worker's state, so an hour of
    silence is a dead worker. ``'pending'`` is ALSO the legitimate queued state, so
    the same cutoff would fail work that is merely waiting. The resolver additionally
    requires ``started_at IS NULL``, so a row that recorded a start is left to the
    ``running`` sweep.

    Returns the number of rows reaped. Cross-tenant via the SECURITY DEFINER
    resolver — see the module docstring for why a plain UPDATE would silently reap
    nothing under enforce.
    """
    reaped = int(
        (await db.execute(_REAP_NEVER_STARTED_SQL, {"secs": stale_after_seconds})).scalar_one()
    )
    await db.commit()
    return reaped


async def run_reaper_loop() -> None:
    """Background loop that periodically reaps stranded ``running`` jobs.

    Sweeps FIRST, then sleeps. The single most likely producer of stranded rows
    is the rollout that just replaced the previous pod, so a loop that slept
    first would leave exactly those rows — the ones this exists for — untouched
    for a full interval on every deploy.

    Fail-open: a sweep that raises (DB blip, an environment that never ran
    migration 0010) is logged and swallowed so the loop keeps running and never
    takes the process down. Multi-replica safe: the UPDATE is idempotent, so a
    second replica sweeping the same window simply reaps 0.
    """
    settings = get_settings()
    if not settings.reaper.enabled:
        logger.info("Critique stale-job reaper is disabled")
        return

    stale_after_seconds = settings.reaper.stale_after_minutes * 60
    pending_stale_after_seconds = settings.reaper.pending_stale_after_minutes * 60
    interval = settings.reaper.interval_minutes * 60
    logger.info(
        "Critique stale-job reaper started: stale_after=%dm, pending_stale_after=%dm, interval=%dm",
        settings.reaper.stale_after_minutes,
        settings.reaper.pending_stale_after_minutes,
        settings.reaper.interval_minutes,
    )

    while True:
        try:
            from src.shared.database.session import get_db_session

            async with get_db_session() as db:
                reaped = await reap_stale_jobs(db, stale_after_seconds)
                never_started = await reap_never_started_jobs(db, pending_stale_after_seconds)
            if reaped:
                logger.warning("Critique reaper marked %d stranded running job(s) failed", reaped)
            if never_started:
                logger.warning(
                    "Critique reaper marked %d never-started pending job(s) failed", never_started
                )
        except asyncio.CancelledError:
            logger.info("Critique stale-job reaper cancelled")
            raise
        except Exception as exc:
            logger.error("Critique stale-job reaper sweep failed: %s", exc, exc_info=True)

        await asyncio.sleep(interval)
