"""Stale-job reaper — the backstop for ``analysis_jobs`` stranded in ``processing``.

The failure this exists to end
-----------------------------
An analysis job is finalized in-band: ``_run_with_timeout`` (``analyze/routes.py``)
awaits ``_process_analysis`` and writes ``completed`` or ``failed`` itself. Every
one of those paths needs the process to still be alive. When it is not — a pod
rollout, a lifespan shutdown that cancels the background task, an OOM kill, a
SIGKILL after the termination grace period — no ``except`` block runs at all and
the row is left at ``status='processing'`` forever.

Before migration 0015 nothing aged those rows out. ``analysis_jobs`` appeared in
the whole migration chain only in 0001 (CREATE/INDEX) and 0013 (tenant cast): no
reaper matched it (0010's predicate is ``'running'`` on ``critique_jobs``, 0014's
is ``'processing'`` on the two *review* tables) and no prune deleted it. This is
the same defect 0014 closed for ``review_jobs``, on the table 0014 did not sweep.

⚠️ The predicate is ``status = 'processing'``. It is NOT ``'running'`` — that is a
critique-only literal — and it is NOT ``'pending'``: the ORM column default is
``'pending'`` but every insert path in ``src/`` passes ``status='processing'``
explicitly (``AnalysisJobRepository.create``, ``routes.py``), so a ``'pending'``
predicate would match zero rows forever while looking like a fix.

Staleness is measured on ``COALESCE(updated_at, created_at)``: ``updated_at``
carries ``server_default now()`` + ``onupdate now()`` and nothing writes the row
between creation and its terminal update, so it is the last sign of life.

Why a SECURITY DEFINER function and not a plain ``UPDATE``
---------------------------------------------------------
Identical to ``critique.services.reaper``: this loop has no request and therefore
no ``app.current_tenant`` GUC, while the sweep it performs is inherently
cross-tenant. A plain ``UPDATE analysis_jobs …`` here would be RLS-scoped to an
UNBOUND GUC under ``TENANT_RLS_MODE=enforce`` — which does not raise. It reports
``UPDATE 0`` and returns cleanly, so a naive sweeper would look healthy in every
log line and reap nothing, forever.

The DDL lives in migration ``0015_sweep_stranded_row_siblings`` and nowhere else
— see that module's "Migration safety" note for why it is not duplicated here.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings

logger = logging.getLogger(__name__)

REAP_FN_NAME = "reap_stale_analysis_jobs"

_REAP_SQL = text(f"SELECT {REAP_FN_NAME}(make_interval(secs => :secs))")


async def reap_stale_analysis_jobs(db: AsyncSession, stale_after_seconds: float) -> int:
    """Move every analysis job stuck in ``processing`` past the threshold to ``failed``.

    Returns the number of rows reaped. Cross-tenant via the SECURITY DEFINER
    resolver — see the module docstring for why a plain UPDATE would silently reap
    nothing under enforce.
    """
    reaped = int((await db.execute(_REAP_SQL, {"secs": stale_after_seconds})).scalar_one())
    await db.commit()
    return reaped


async def run_analyze_reaper_loop() -> None:
    """Background loop that periodically reaps stranded ``processing`` analysis jobs.

    Sweeps FIRST, then sleeps. The single most likely producer of stranded rows is
    the rollout that just replaced the previous pod, so a loop that slept first
    would leave exactly those rows — the ones this exists for — untouched for a
    full interval on every deploy.

    Fail-open: a sweep that raises (DB blip, an environment that has not yet run
    migration 0015) is logged and swallowed so the loop keeps running and never
    takes the process down. Multi-replica safe: the UPDATE is idempotent, so a
    second replica sweeping the same window simply reaps 0.
    """
    settings = get_settings()
    if not settings.analyze_reaper.enabled:
        logger.info("Analysis stale-job reaper is disabled")
        return

    stale_after_seconds = settings.analyze_reaper.stale_after_minutes * 60
    interval = settings.analyze_reaper.interval_minutes * 60
    logger.info(
        "Analysis stale-job reaper started: stale_after=%dm, interval=%dm",
        settings.analyze_reaper.stale_after_minutes,
        settings.analyze_reaper.interval_minutes,
    )

    while True:
        try:
            from src.shared.database.session import get_db_session

            async with get_db_session() as db:
                reaped = await reap_stale_analysis_jobs(db, stale_after_seconds)
            if reaped:
                logger.warning(
                    "Analysis reaper marked %d stranded processing job(s) failed", reaped
                )
        except asyncio.CancelledError:
            logger.info("Analysis stale-job reaper cancelled")
            raise
        except Exception as exc:
            logger.error("Analysis stale-job reaper sweep failed: %s", exc, exc_info=True)

        await asyncio.sleep(interval)
