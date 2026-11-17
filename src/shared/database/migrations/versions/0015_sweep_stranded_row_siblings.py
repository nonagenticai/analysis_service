"""Sweep the stranded-row siblings 0014 left behind: ``pending`` critique jobs, ``analysis_jobs``, ``critique_batches``.

0014 closed the stranded-row class for ``review_jobs`` + ``analyze_review_jobs``
and stopped there. Three siblings were never swept. All three are the same
defect: a row in a non-terminal state that no code path will ever move, matched
by no reaper and by no retention, so it lives forever.

1. ``critique_jobs`` stranded at ``'pending'``
----------------------------------------------
``CritiqueJobLifecycle.create_job`` writes ``status='pending'`` and **commits**
(``critique_service/lifecycle.py``) BEFORE the route hands the work to
``asyncio.create_task`` (``critique/routes/jobs.py``). The row only becomes
``'running'`` later, in ``_phase_mark_running`` (``critique_service/phases.py``).
A pod that dies inside that window leaves a committed ``'pending'`` row that
matches NOTHING: ``reap_stale_critique_jobs`` (0010) is ``status='running'``
only, and ``prune_critique_data`` (0005) deletes only ``('completed','failed')``.

⚠️ ``'pending'`` is ALSO the legitimate queued state, which is why this is a
SECOND function on a SECOND, much longer cutoff rather than a widened predicate
on 0010's. Reusing 0010's 60-minute ``running`` budget for ``pending`` would
reap live work. The extra predicate ``started_at IS NULL`` is belt-and-braces:
``started_at`` is written only by ``_phase_mark_running``, so a genuinely
never-started row always has it NULL, and a row that somehow carries one is left
for the ``running`` reaper.

Anchored on ``created_at``, not ``COALESCE(started_at, created_at)``: for a row
this function can match, ``started_at`` is NULL by predicate, so the COALESCE
would be pure noise.

2. ``analysis_jobs`` had NO reaper and NO retention at all
----------------------------------------------------------
It appears in the whole migration chain only in 0001 (CREATE/INDEX) and 0013
(tenant cast). ``AnalysisJobRepository.create`` writes ``'processing'`` and the
route finalizes it in-band from ``asyncio.create_task`` (``analyze/routes.py``),
so it strands exactly like ``review_jobs`` did — and nothing has ever deleted a
terminal row, so every analysis ever run is still on disk.

``'processing'``, not ``'running'`` and not ``'pending'``: ``'running'`` is a
critique-only literal, and while the ORM column default is ``'pending'`` every
insert path in ``src/`` passes ``status='processing'`` explicitly, so a
``'pending'`` predicate here would be dead SQL that matches nothing forever
while reading as a fix. Verified with ``git grep`` over ``src/domains/analyze``.

Staleness on ``COALESCE(updated_at, created_at)`` — ``updated_at`` carries
``server_default now()`` + ``onupdate now()`` and nothing writes the row
mid-flight, so it is the last sign of life (same reasoning as 0014).

3. ``critique_batches``, and the ``'cancelled'`` hole
------------------------------------------------------
``critique_batches`` is in no prune body either. ``prune_critique_data`` is
replaced below — SAME signature, so its owner and its 0011 EXECUTE grant are
preserved and no new grant is needed — to also delete terminal batches.

⚠️ Terminal for a batch is ``'completed'`` ALONE. ``update_batch_status``
(``critique_service/lifecycle.py``) writes only ``'completed'`` and ``'running'``
and ``create_batch`` writes ``'pending'``; there is no ``'failed'`` batch and no
``'partial'`` (that is a *review*-batch literal, applied to ``review_batches`` by
0014 — putting it here would be dead SQL). Aged on ``completed_at``, which is
NULL for every non-terminal batch and so excludes them by comparison.

⚠️ ``'cancelled'`` is an **analysis_jobs**-only literal
(``analyze/.../job_repository.py`` ``cancel()``); ``git grep`` finds no critique
or review path that writes it. So it belongs in ``prune_analysis_jobs`` below,
NOT in ``prune_critique_data`` — adding it to the critique predicate would have
been another line that reads as a fix and matches nothing.

Migration safety
----------------
Everything here is ADDITIVE. Three NEW function names are created; no existing
signature changes arity, and the only existing function touched
(``prune_critique_data``) is replaced at its identical signature, so the ``GRANT
EXECUTE`` lines below all name signatures this revision leaves resident and an
old pod overlapping a new one during the rollout keeps calling functions that
still exist. A new name also sidesteps the empty-EXECUTE-ACL trap a new overload
of an existing name would create.

The DDL is written out LITERALLY here rather than imported from the runtime
modules (0010's shape): a migration is a historical record, not a view of
today's code. Unlike 0014 the bodies are NOT duplicated into a module constant —
the runtime only ever issues ``SELECT fn(...)``, and the tests load this module
by path, so there is exactly one copy of each body and nothing to drift.

Revision ID: 0015_sweep_stranded_row_siblings
Revises: 0014_review_reaper_retention
"""

from alembic import op

from src.shared.database.resolver_grants import guarded_grant_statement

revision: str = "0015_sweep_stranded_row_siblings"
down_revision: str | None = "0014_review_reaper_retention"
branch_labels = None
depends_on = None


# SECURITY DEFINER for the same reason as every other resolver in this service
# (0005/0007/0010/0014): the background loop has no request and so no
# ``app.current_tenant`` GUC, and under ``TENANT_RLS_MODE=enforce`` a plain UPDATE
# or DELETE on the restricted runtime role is scoped to an UNBOUND GUC — which does
# not raise, it reports 0 rows. Owner-owned, the body bypasses RLS ENABLE (never
# FORCE) and sees every tenant's rows. EXECUTE is revoked from PUBLIC below.
_REAP_NEVER_STARTED_CRITIQUE_JOBS_FN = """
CREATE OR REPLACE FUNCTION reap_never_started_critique_jobs(p_stale_after interval)
  RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE n integer;
  BEGIN
    UPDATE critique_jobs
       SET status = 'failed',
           error_message = 'reaped: job was never started -- it was committed as pending and the process died before it could be picked up (pod rollout, shutdown or crash)',
           completed_at = now()
     WHERE status = 'pending'
       AND started_at IS NULL
       AND created_at < now() - p_stale_after;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
  END $fn$
"""

_REAP_STALE_ANALYSIS_JOBS_FN = """
CREATE OR REPLACE FUNCTION reap_stale_analysis_jobs(p_stale_after interval)
  RETURNS integer
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE n integer;
  BEGIN
    UPDATE analysis_jobs
       SET status = 'failed',
           error = 'reaped: analysis job was left processing with no live worker (pod rollout, shutdown or crash before it could be finalized)',
           updated_at = now()
     WHERE status = 'processing'
       AND COALESCE(updated_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
  END $fn$
"""

# ``'cancelled'`` is in the terminal set on purpose — see the module docstring.
# It is the one status ``AnalysisJobRepository.cancel()`` writes, and before this
# revision no retention path in EITHER domain matched it.
_PRUNE_ANALYSIS_JOBS_FN = """
CREATE OR REPLACE FUNCTION prune_analysis_jobs(p_cutoff timestamptz) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE n bigint;
  BEGIN
    WITH d AS (DELETE FROM analysis_jobs
               WHERE status IN ('completed', 'failed', 'cancelled') AND updated_at < p_cutoff
               RETURNING 1) SELECT count(*) INTO n FROM d;
    RETURN n;
  END $fn$
"""

# CREATE OR REPLACE keeps the function's owner and ACL, so the EXECUTE grant 0011
# gave ``w2v_analysis_app`` on ``prune_critique_data(timestamptz, timestamptz)``
# survives this and no new grant is needed. Signature and return type unchanged.
_PRUNE_CRITIQUE_FN_V2 = """
CREATE OR REPLACE FUNCTION prune_critique_data(p_job_cutoff timestamptz, p_audit_cutoff timestamptz)
  RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE j bigint; f bigint; a bigint; b bigint;
  BEGIN
    WITH d AS (DELETE FROM critique_jobs WHERE status IN ('completed','failed')
               AND created_at < p_job_cutoff RETURNING 1) SELECT count(*) INTO j FROM d;
    WITH d AS (DELETE FROM critique_findings WHERE created_at < p_job_cutoff RETURNING 1)
      SELECT count(*) INTO f FROM d;
    WITH d AS (DELETE FROM audit_logs WHERE created_at < p_audit_cutoff RETURNING 1)
      SELECT count(*) INTO a FROM d;
    WITH d AS (DELETE FROM critique_batches WHERE status = 'completed'
               AND completed_at < p_job_cutoff RETURNING 1) SELECT count(*) INTO b FROM d;
    RETURN jsonb_build_object('jobs', j, 'findings', f, 'audit_logs', a, 'batches', b);
  END $fn$
"""

# The pre-0015 body, restored verbatim on downgrade so a rollback does not leave a
# database pruning a table the code of that era never expected it to touch.
_PRUNE_CRITIQUE_FN_V1 = """
CREATE OR REPLACE FUNCTION prune_critique_data(p_job_cutoff timestamptz, p_audit_cutoff timestamptz)
  RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE j bigint; f bigint; a bigint;
  BEGIN
    WITH d AS (DELETE FROM critique_jobs WHERE status IN ('completed','failed')
               AND created_at < p_job_cutoff RETURNING 1) SELECT count(*) INTO j FROM d;
    WITH d AS (DELETE FROM critique_findings WHERE created_at < p_job_cutoff RETURNING 1)
      SELECT count(*) INTO f FROM d;
    WITH d AS (DELETE FROM audit_logs WHERE created_at < p_audit_cutoff RETURNING 1)
      SELECT count(*) INTO a FROM d;
    RETURN jsonb_build_object('jobs', j, 'findings', f, 'audit_logs', a);
  END $fn$
"""

#: Pinned tuple, never the live ``RESOLVER_SIGNATURES`` default — this revision
#: grants exactly the three resolvers it just created. See 0011's note for the
#: outage shape a live-default render produces.
_GRANTED_BY_0015: tuple[str, ...] = (
    "reap_never_started_critique_jobs(interval)",
    "reap_stale_analysis_jobs(interval)",
    "prune_analysis_jobs(timestamptz)",
)


def upgrade() -> None:
    op.execute(_REAP_NEVER_STARTED_CRITIQUE_JOBS_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION reap_never_started_critique_jobs(interval) FROM PUBLIC")
    op.execute(_REAP_STALE_ANALYSIS_JOBS_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION reap_stale_analysis_jobs(interval) FROM PUBLIC")
    op.execute(_PRUNE_ANALYSIS_JOBS_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_analysis_jobs(timestamptz) FROM PUBLIC")
    op.execute(guarded_grant_statement(_GRANTED_BY_0015))
    op.execute(_PRUNE_CRITIQUE_FN_V2)


def downgrade() -> None:
    op.execute(_PRUNE_CRITIQUE_FN_V1)
    op.execute("DROP FUNCTION IF EXISTS prune_analysis_jobs(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS reap_stale_analysis_jobs(interval)")
    op.execute("DROP FUNCTION IF EXISTS reap_never_started_critique_jobs(interval)")
