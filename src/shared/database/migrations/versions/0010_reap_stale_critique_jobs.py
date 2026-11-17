"""Stale critique-job reaper: ``reap_stale_critique_jobs`` SECURITY DEFINER resolver.

A critique job that loses its process mid-flight (pod rollout, lifespan shutdown,
OOM/SIGKILL) leaves ``critique_jobs.status = 'running'`` with nothing that will
ever change it, and the orchestrator polls such a row forever. This adds the
cross-tenant resolver the background reaper loop calls.

SECURITY DEFINER for the same reason as ``prune_critique_data`` (0005) and
``claim_due_critique_schedules`` (0007): the loop runs with no request and so no
``app.current_tenant`` GUC, and under ``TENANT_RLS_MODE=enforce`` a plain UPDATE
on the restricted runtime role would be scoped to an unbound GUC — which does not
raise, it reports ``UPDATE 0``. Owner-owned, the body bypasses RLS ENABLE (never
FORCE) and sees every tenant's rows. EXECUTE is revoked from PUBLIC.

The DDL is imported from the reaper module so the function body has exactly one
source of truth, shared by this migration and the tests.

Revision ID: 0010_reap_stale_critique_jobs
Revises: 0009_critique_tenant_not_null
"""

from alembic import op

from src.domains.critique.services.reaper import REAP_STALE_CRITIQUE_JOBS_DDL

revision: str = "0010_reap_stale_critique_jobs"
down_revision: str | None = "0009_critique_tenant_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(REAP_STALE_CRITIQUE_JOBS_DDL)
    op.execute("REVOKE EXECUTE ON FUNCTION reap_stale_critique_jobs(interval) FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reap_stale_critique_jobs(interval)")
