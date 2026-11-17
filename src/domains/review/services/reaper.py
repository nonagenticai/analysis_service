"""Stale-job reaper for review jobs stranded in ``processing``.

The failure this exists to end
-----------------------------
Two tables hold review-job state and BOTH are finalized only in-band, by a
process that has to still be alive:

``review_jobs`` (this domain)
    ``JobManager.create_job`` inserts at ``status='processing'`` and only
    ``mark_completed`` / ``mark_failed`` ever move it. Both run inside the
    request's own task.

``analyze_review_jobs`` (the analyze domain's ``/api/analyze/review`` store)
    ``AnalyzeReviewJobRepository.create`` inserts at ``status='processing'`` and
    the only writer of a terminal status is ``_persist_review_result``, called
    from the ``asyncio.create_task(_process_review(...))`` background task in
    ``domains/analyze/routes.py``.

When that process dies mid-flight — a pod rollout, the lifespan shutdown that
cancels every background task, an OOM kill, a SIGKILL after the grace period —
no ``except`` block runs at all and the row is left at ``processing`` forever.
Neither table had any backstop before this module: the ONLY reaper in the
service is ``domains/critique/services/reaper.py``, and its resolver touches
``critique_jobs`` alone.

⚠️ ``processing``, NOT ``running``
----------------------------------
The critique reaper's predicate is ``status = 'running'``. Neither table here
ever holds that literal — ``git grep`` over ``src/`` finds ``'running'`` only in
the critique domain. Copy-pasting that predicate would produce a reaper that
sweeps zero rows on every tick, forever, while every log line and every green
test says it is working. The status literals below are read from the models:
``review/models.py:39`` and ``analyze/review_job_models.py:63``.

Why ``COALESCE(updated_at, created_at)`` and not ``started_at``
--------------------------------------------------------------
Neither table has a ``started_at`` or a ``completed_at`` column — that is a
critique-only shape. ``updated_at`` carries ``server_default=now()`` plus
``onupdate=now()`` on both models and nothing writes it mid-flight (only the two
terminal writers do), so it is exactly "the last sign of life". The COALESCE is
belt-and-braces for a row that predates the server default.

Why ONE resolver and ONE loop across the two tables
---------------------------------------------------
The two tables have the identical stranding mode, the identical remedy and the
identical staleness question, so a second loop would be a second background task
and a second DB session per tick to run the same sweep. ``prune_critique_data``
(0005) already sets the in-repo precedent for a single SECURITY DEFINER resolver
that spans several tables and returns per-table counts as jsonb.

The analyze table's module docstring asks that ``analyze_review_jobs`` stay out
of the analyze domain's ``models.py`` — that constraint is about migration 0005
importing live ``Base.metadata`` and the fresh-bootstrap ordering hazard it
creates, not about which loop may sweep the table. Nothing here imports its
model; the DDL names the table literally.

Why a SECURITY DEFINER function and not a plain ``UPDATE``
---------------------------------------------------------
Same reasoning as the critique reaper, ``prune_review_jobs`` (0005) and
``claim_due_critique_schedules`` (0007): in prod the runtime connects as the
restricted, non-owner role under ``TENANT_RLS_MODE=enforce`` and every request
binds ``app.current_tenant``. This loop has no request and therefore no tenant
GUC, while the sweep it must perform is inherently cross-tenant. A plain
``UPDATE review_jobs …`` here would be RLS-scoped to an UNBOUND GUC — which does
not raise. It reports ``UPDATE 0`` and returns cleanly, so a naive sweeper would
look healthy in every log line and reap nothing, forever.

The DDL lives next to the loop that calls it so the migration and the tests share
one source of truth for the function name and body.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings

logger = logging.getLogger(__name__)

REAP_FN_NAME = "reap_stale_review_jobs"

#: The non-terminal status BOTH tables are created in. Read from the models, not
#: from the critique reaper — see the ``processing``/``running`` note above.
STRANDED_STATUS = "processing"

#: Persisted into ``error`` so an operator (and the orchestrator's error surface)
#: can tell a reaped job from one that failed on its own merits.
REAPED_ERROR_MESSAGE = (
    "reaped: review job was left processing with no live worker "
    "(pod rollout, shutdown or crash before it could be finalized)"
)

# Rendered with ``str.replace`` from module constants rather than an f-string (the
# same shape as ``critique.services.reaper._REAP_DDL_TEMPLATE`` and
# ``scheduler.CLAIM_DUE_SCHEDULES_DDL``): nothing user-supplied reaches this DDL,
# and keeping the template a plain literal means the SQL body is readable as SQL.
# The apostrophe-doubling below is here so an editor who adds one to the message
# does not silently break the migration.
_REAP_DDL_TEMPLATE = """
CREATE OR REPLACE FUNCTION __FN__(p_stale_after interval)
  RETURNS jsonb
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE r integer; a integer;
  BEGIN
    UPDATE review_jobs
       SET status = 'failed',
           error = '__MESSAGE__',
           updated_at = now()
     WHERE status = '__STRANDED__'
       AND COALESCE(updated_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS r = ROW_COUNT;

    UPDATE analyze_review_jobs
       SET status = 'failed',
           error = '__MESSAGE__',
           updated_at = now()
     WHERE status = '__STRANDED__'
       AND COALESCE(updated_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS a = ROW_COUNT;

    RETURN jsonb_build_object('review_jobs', r, 'analyze_review_jobs', a);
  END $fn$
"""

REAP_STALE_REVIEW_JOBS_DDL: str = (
    _REAP_DDL_TEMPLATE.replace("__FN__", REAP_FN_NAME)
    .replace("__MESSAGE__", REAPED_ERROR_MESSAGE.replace("'", "''"))
    .replace("__STRANDED__", STRANDED_STATUS.replace("'", "''"))
)

_REAP_SQL = text(f"SELECT {REAP_FN_NAME}(make_interval(secs => :secs))")


async def reap_stale_review_jobs(db: AsyncSession, stale_after_seconds: float) -> dict[str, int]:
    """Move every review job stuck in ``processing`` past the threshold to ``failed``.

    Returns per-table reaped counts, e.g. ``{"review_jobs": 2,
    "analyze_review_jobs": 0}``. Cross-tenant via the SECURITY DEFINER resolver —
    see the module docstring for why a plain UPDATE would silently reap nothing
    under enforce.
    """
    raw = (await db.execute(_REAP_SQL, {"secs": stale_after_seconds})).scalar_one()
    await db.commit()
    return {str(k): int(v) for k, v in dict(raw).items()}


async def run_review_reaper_loop() -> None:
    """Background loop that periodically reaps stranded ``processing`` review jobs.

    Sweeps FIRST, then sleeps. The single most likely producer of stranded rows is
    the rollout that just replaced the previous pod, so a loop that slept first
    would leave exactly those rows — the ones this exists for — untouched for a
    full interval on every deploy. (The review domain's ``cleanup_old_jobs`` loop
    already carries the same note for the same reason.)

    Fail-open: a sweep that raises (DB blip, an environment that has not yet run
    the migration that creates the resolver) is logged and swallowed so the loop
    keeps running and never takes the process down. Multi-replica safe: the
    UPDATEs are idempotent, so a second replica sweeping the same window reaps 0.
    """
    settings = get_settings()
    if not settings.review_reaper.enabled:
        logger.info("Review stale-job reaper is disabled")
        return

    stale_after_seconds = settings.review_reaper.stale_after_minutes * 60
    interval = settings.review_reaper.interval_minutes * 60
    logger.info(
        "Review stale-job reaper started: stale_after=%dm, interval=%dm",
        settings.review_reaper.stale_after_minutes,
        settings.review_reaper.interval_minutes,
    )

    while True:
        try:
            from src.shared.database.session import get_db_session

            async with get_db_session() as db:
                reaped = await reap_stale_review_jobs(db, stale_after_seconds)
            if any(reaped.values()):
                logger.warning(
                    "Review reaper marked stranded processing job(s) failed: %s",
                    reaped,
                )
        except asyncio.CancelledError:
            logger.info("Review stale-job reaper cancelled")
            raise
        except Exception as exc:
            logger.error("Review stale-job reaper sweep failed: %s", exc, exc_info=True)

        await asyncio.sleep(interval)
