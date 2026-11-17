"""Persist tenant provenance on ``critique_schedules`` and widen the claim resolver.

THE DEFECT
----------
``run_scheduler`` fires from a 60-second cron poll. It has NO request scope, so
it established identity with ``set_identity_context(tenant_uuid(tid), None,
None)`` — three positional arguments, ``tenant_source`` left at its default
``None``. ``w2v_common``'s outbound hook treats ``None`` provenance as UNVOUCHED
(correctly: a caller that never stated where its tenant came from is exactly the
caller that must not delegate). So every metered outbound hop a scheduled
critique made — worker_management via ``WMClient`` — raised
``UnvouchedTenantError``. Scheduled scans could not complete at all.

The tenant was never the problem; the *provenance* was, and it is only knowable
in request scope. This migration gives the row somewhere to keep it.

``tenant_source`` — NULLABLE, NO SERVER DEFAULT
-----------------------------------------------
Deliberate, and the single most important line here. Rows that already exist
genuinely have unknown provenance. A ``server_default`` would be a FORGED VOUCH:
``critique_schedules.tenant_id`` is an ordinary column writable by anything
holding the tenant's DB grant, so stamping every legacy row with a vouched rung
would let whoever wrote those rows spend an arbitrary tenant's BYO provider
credentials, forever, on a timer. NULL means "not recorded", and
``scheduler._vouched_source_or_raise`` fails closed on it: no job row, no spend.
The remedy for a refused legacy row is to re-create it through the API from a
request that actually carries a vouched tenant.

WHY THE RESOLVER IS DROPPED AND RE-CREATED
-------------------------------------------
``claim_due_critique_schedules`` gains a column in its ``RETURNS TABLE``.
Postgres refuses that under ``CREATE OR REPLACE FUNCTION`` —
``cannot change return type of existing function`` — so the function must be
DROPped first. Dropping it also drops its ACL, which is why the 0011 grant is
re-applied below; without that the scheduler loop would fail with
``permission denied for function`` on every tick, which is the exact outage 0011
was written to end.

The DDL below is a FROZEN LITERAL, not
``scheduler.CLAIM_DUE_SCHEDULES_DDL``. 0007 imported that constant and this
revision is precisely what broke it (see the note in 0007): a migration is
immutable history and must not re-render itself from code that keeps moving.

Revision ID: 0012_schedule_tenant_source
Revises: 0011_grant_resolver_execute
"""

import sqlalchemy as sa
from alembic import op

from src.shared.database.resolver_grants import guarded_grant_statement

revision: str = "0012_schedule_tenant_source"
down_revision: str | None = "0011_grant_resolver_execute"
branch_labels = None
depends_on = None

#: Resolver body WITH ``tenant_source``. Frozen literal — never regenerate.
_CLAIM_WITH_TENANT_SOURCE = """
CREATE FUNCTION claim_due_critique_schedules(p_now timestamptz)
  RETURNS TABLE(schedule_id varchar, tenant_id varchar, tenant_source varchar, url text, categories jsonb)
  LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    UPDATE critique_schedules s
    SET last_run_at = p_now,
        run_count = COALESCE(s.run_count, 0) + 1,
        next_run_at = p_now + (CASE s.cron_expression
        WHEN '@hourly' THEN interval '3600 seconds'
        WHEN '@every_6h' THEN interval '21600 seconds'
        WHEN '@every_12h' THEN interval '43200 seconds'
        WHEN '@daily' THEN interval '86400 seconds'
        WHEN '@weekly' THEN interval '604800 seconds'
        ELSE interval '86400 seconds'
      END)
    WHERE s.id IN (
      SELECT id FROM critique_schedules
      WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= p_now
      FOR UPDATE SKIP LOCKED
    )
    RETURNING s.schedule_id, s.tenant_id, s.tenant_source, s.url, s.categories;
  $fn$
"""

#: Resolver body WITHOUT ``tenant_source`` — restored by ``downgrade``. Frozen.
_CLAIM_WITHOUT_TENANT_SOURCE = """
CREATE FUNCTION claim_due_critique_schedules(p_now timestamptz)
  RETURNS TABLE(schedule_id varchar, tenant_id varchar, url text, categories jsonb)
  LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    UPDATE critique_schedules s
    SET last_run_at = p_now,
        run_count = COALESCE(s.run_count, 0) + 1,
        next_run_at = p_now + (CASE s.cron_expression
        WHEN '@hourly' THEN interval '3600 seconds'
        WHEN '@every_6h' THEN interval '21600 seconds'
        WHEN '@every_12h' THEN interval '43200 seconds'
        WHEN '@daily' THEN interval '86400 seconds'
        WHEN '@weekly' THEN interval '604800 seconds'
        ELSE interval '86400 seconds'
      END)
    WHERE s.id IN (
      SELECT id FROM critique_schedules
      WHERE enabled AND next_run_at IS NOT NULL AND next_run_at <= p_now
      FOR UPDATE SKIP LOCKED
    )
    RETURNING s.schedule_id, s.tenant_id, s.url, s.categories;
  $fn$
"""

_DROP_CLAIM = "DROP FUNCTION IF EXISTS claim_due_critique_schedules(timestamptz)"
_REVOKE_CLAIM = "REVOKE EXECUTE ON FUNCTION claim_due_critique_schedules(timestamptz) FROM PUBLIC"


def upgrade() -> None:
    op.add_column(
        "critique_schedules",
        # No server_default. See the module docstring — a default here is a
        # forged vouch, not a convenience.
        sa.Column("tenant_source", sa.String(length=32), nullable=True),
    )

    # DROP, not CREATE OR REPLACE: the return type changes.
    op.execute(_DROP_CLAIM)
    op.execute(_CLAIM_WITH_TENANT_SOURCE)
    # The DROP took the ACL with it. Re-apply 0007's REVOKE and 0011's GRANT so
    # the resolver comes back with exactly the ACL it had, in both directions.
    op.execute(_REVOKE_CLAIM)
    op.execute(guarded_grant_statement(("claim_due_critique_schedules(timestamptz)",)))


def downgrade() -> None:
    # Restore the narrow resolver FIRST. Postgres does not track dependencies
    # inside a SQL function's string body, so the DROP COLUMN would technically
    # succeed either way — but in the other order the resolver would spend that
    # window naming a column that no longer exists, and any scheduler tick
    # landing in it errors. Ordered this way there is no such window.
    op.execute(_DROP_CLAIM)
    op.execute(_CLAIM_WITHOUT_TENANT_SOURCE)
    op.execute(_REVOKE_CLAIM)
    op.execute(guarded_grant_statement(("claim_due_critique_schedules(timestamptz)",)))
    op.drop_column("critique_schedules", "tenant_source")
