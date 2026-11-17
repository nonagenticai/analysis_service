"""Grant EXECUTE on the four SECURITY DEFINER resolvers to ``w2v_analysis_app``.

Migrations 0005/0007/0010 each ``REVOKE``d EXECUTE from ``PUBLIC`` and never granted
it to the restricted runtime role, so a database rebuilt purely from migrations gives
the runtime a set of resolvers it is not allowed to call. In prod the grants were
applied by hand during the Stage-A cutover and were applied unevenly — twice now a
missed one has produced a background loop that fails on every tick forever
(``claim_due_critique_schedules``, repaired out-of-band; ``reap_stale_critique_jobs``,
the reason for this migration).

This is a pure ``GRANT``: idempotent, safe to re-run, and a no-op wherever the role
does not exist (CI, local test DBs) thanks to the ``pg_roles`` guard. It also makes
the two out-of-band prod grants reproducible rather than snowflakes.

No ``downgrade`` revoke: taking EXECUTE away from the runtime role would re-break the
loops this repairs, and 0005/0007/0010's own ``downgrade`` drops the functions (and
with them their ACLs) anyway.

Revision ID: 0011_grant_resolver_execute
Revises: 0010_reap_stale_critique_jobs
"""

from alembic import op

from src.shared.database.resolver_grants import (
    RESOLVER_SIGNATURES_AS_OF_0011,
    guarded_grant_statement,
)

revision: str = "0011_grant_resolver_execute"
down_revision: str | None = "0010_reap_stale_critique_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PINNED, not the live default. `guarded_grant_statement()` defaults to
    # `RESOLVER_SIGNATURES`, which is a view of TODAY's code — so the moment a
    # fifth resolver is added, this mid-chain revision starts emitting a GRANT for
    # a function that revisions 0012..N have not created yet, and an upgrade from
    # an old database aborts here on `undefined_function`. It aborts only in prod:
    # the whole statement sits inside a `pg_roles` guard on a role that exists
    # nowhere else, so CI runs the empty branch and ships green. See
    # RESOLVER_SIGNATURES_AS_OF_0011 for the full note.
    op.execute(guarded_grant_statement(RESOLVER_SIGNATURES_AS_OF_0011))


def downgrade() -> None:
    """Deliberately a no-op — see the module docstring."""
