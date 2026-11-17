"""Reap review jobs stranded in ``processing``; give the analyze + batch tables retention.

Three defects, one revision, all in the review-job lifecycle.

1. ``review_jobs`` rows stranded in ``processing`` are IMMORTAL
--------------------------------------------------------------
The only reaper in this service is ``reap_stale_critique_jobs`` (0010), and it
touches ``critique_jobs`` alone. ``review_jobs`` is created at ``processing``
and reaches a terminal status only in-band, so a pod rollout / OOM / SIGKILL
leaves it at ``processing`` with nothing that will ever move it — and
``prune_review_jobs`` (0005) deletes only ``('completed','failed')``, so it is
not swept and not pruned either. ``analyze_review_jobs`` (0007) has the same
in-band-only finalizer (``asyncio.create_task(_process_review(...))``) and had
neither a reaper nor ANY retention.

``reap_stale_review_jobs(interval)`` below sweeps both tables.

⚠️ The predicate is ``status = 'processing'``, NOT ``'running'``. ``'running'``
is a critique-only literal; copying 0010's predicate here would sweep zero rows
forever while looking perfectly healthy. Verified against
``src/domains/review/models.py`` and ``src/domains/analyze/review_job_models.py``.

Neither table has ``started_at``/``completed_at`` (those are critique columns),
so staleness is measured on ``COALESCE(updated_at, created_at)``: ``updated_at``
carries ``server_default now()`` + ``onupdate now()`` on both models and nothing
writes it mid-flight, so it is the last sign of life.

2. Retention covered only ONE of the three review tables
--------------------------------------------------------
``prune_review_jobs`` is replaced (same signature, so its owner and its 0011
EXECUTE grant are preserved) to also prune:

* ``analyze_review_jobs`` — terminal ``('completed','failed')``, aged on
  ``updated_at``. It had no retention job at all; every row leaked forever
  regardless of status.
* ``review_batches`` — terminal ``('completed','partial')``, aged on
  ``completed_at``. Also no retention at all.

⚠️ A note on ``'partial'``, because it is easy to put in the wrong place:
``'partial'`` is written at ``job_manager.py:332`` to ``ReviewBatch.status``, and
``git grep`` finds that as the ONLY occurrence of the literal in ``src/``. It is
a **batch** status; ``review_jobs.status`` is only ever
``processing``/``completed``/``failed``. Adding ``'partial'`` to the
``review_jobs`` predicate would have been dead SQL that matches nothing and
still reads as a fix — so it is applied to ``review_batches``, where it is real.
Rows in a non-terminal batch state have ``completed_at IS NULL`` and are
excluded by the comparison, which is the intended behaviour.

3. The DDL here is LITERAL, on purpose
--------------------------------------
0010 imports its function body from the reaper module. That is fine for an
idempotent ``CREATE OR REPLACE`` but it is the same shape as the 0011 grant
defect this branch also fixes: a migration must be a historical record, not a
view of today's code. The body below is written out; ``tests/unit/domains/review/
test_stale_review_job_reaper.py`` pins it against the module constant so the two
cannot drift silently — a body change is meant to force a NEW migration.

Revision ID: 0014_review_reaper_retention
Revises: 0013_tenant_id_uuid
"""

from alembic import op

from src.shared.database.resolver_grants import guarded_grant_statement

revision: str = "0014_review_reaper_retention"
down_revision: str | None = "0013_tenant_id_uuid"
branch_labels = None
depends_on = None


# SECURITY DEFINER for the same reason as every other resolver in this service
# (0005/0007/0010): the background loop has no request and so no
# ``app.current_tenant`` GUC, and under ``TENANT_RLS_MODE=enforce`` a plain UPDATE
# on the restricted runtime role is scoped to an UNBOUND GUC — which does not
# raise, it reports ``UPDATE 0``. Owner-owned, the body bypasses RLS ENABLE (never
# FORCE) and sees every tenant's rows. EXECUTE is revoked from PUBLIC below.
_REAP_STALE_REVIEW_JOBS_FN = """
CREATE OR REPLACE FUNCTION reap_stale_review_jobs(p_stale_after interval)
  RETURNS jsonb
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE r integer; a integer;
  BEGIN
    UPDATE review_jobs
       SET status = 'failed',
           error = 'reaped: review job was left processing with no live worker (pod rollout, shutdown or crash before it could be finalized)',
           updated_at = now()
     WHERE status = 'processing'
       AND COALESCE(updated_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS r = ROW_COUNT;

    UPDATE analyze_review_jobs
       SET status = 'failed',
           error = 'reaped: review job was left processing with no live worker (pod rollout, shutdown or crash before it could be finalized)',
           updated_at = now()
     WHERE status = 'processing'
       AND COALESCE(updated_at, created_at) < now() - p_stale_after;
    GET DIAGNOSTICS a = ROW_COUNT;

    RETURN jsonb_build_object('review_jobs', r, 'analyze_review_jobs', a);
  END $fn$
"""

# CREATE OR REPLACE keeps the function's owner and ACL, so the EXECUTE grant 0011
# gave ``w2v_analysis_app`` on ``prune_review_jobs(timestamptz)`` survives this and
# no new grant is needed. LANGUAGE moves sql -> plpgsql (allowed: the signature and
# return type are unchanged) because three DELETEs now have to be counted together.
_PRUNE_REVIEW_FN_V2 = """
CREATE OR REPLACE FUNCTION prune_review_jobs(p_cutoff timestamptz) RETURNS bigint
  LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
  DECLARE j bigint; a bigint; b bigint;
  BEGIN
    WITH d AS (DELETE FROM review_jobs
               WHERE status IN ('completed', 'failed') AND updated_at < p_cutoff
               RETURNING 1) SELECT count(*) INTO j FROM d;
    WITH d AS (DELETE FROM analyze_review_jobs
               WHERE status IN ('completed', 'failed') AND updated_at < p_cutoff
               RETURNING 1) SELECT count(*) INTO a FROM d;
    WITH d AS (DELETE FROM review_batches
               WHERE status IN ('completed', 'partial') AND completed_at < p_cutoff
               RETURNING 1) SELECT count(*) INTO b FROM d;
    RETURN j + a + b;
  END $fn$
"""

# The pre-0014 body, restored verbatim on downgrade so a rollback does not leave a
# database pruning tables the code of that era never expected it to touch.
_PRUNE_REVIEW_FN_V1 = """
CREATE OR REPLACE FUNCTION prune_review_jobs(p_cutoff timestamptz) RETURNS bigint
  LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH d AS (
      DELETE FROM review_jobs
      WHERE status IN ('completed', 'failed') AND updated_at < p_cutoff
      RETURNING 1
    ) SELECT count(*) FROM d;
  $fn$
"""


def upgrade() -> None:
    op.execute(_REAP_STALE_REVIEW_JOBS_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION reap_stale_review_jobs(interval) FROM PUBLIC")
    # Pinned tuple, never the live `RESOLVER_SIGNATURES` default — this revision
    # grants exactly the one resolver it just created. See 0011's note.
    op.execute(guarded_grant_statement(("reap_stale_review_jobs(interval)",)))
    op.execute(_PRUNE_REVIEW_FN_V2)


def downgrade() -> None:
    op.execute(_PRUNE_REVIEW_FN_V1)
    op.execute("DROP FUNCTION IF EXISTS reap_stale_review_jobs(interval)")
