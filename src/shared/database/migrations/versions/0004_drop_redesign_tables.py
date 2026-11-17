"""Drop the redesign domain tables.

The redesign domain (AI-powered HTML redesign + Playwright screenshots) was
lifted out of analysis_service into the standalone ``html_render_service``
(served at ``/api/designs``). analysis_service no longer owns this capability,
so its six redesign tables are dropped here.

Idempotent ``DROP TABLE IF EXISTS ... CASCADE`` — a no-op on a DB that never
had these tables (fresh CI), corrective on the live analysis_service DB where
they were created by ``create_all`` / earlier migrations. The migrating role
owns the tables (created by analysis_service), so the IF EXISTS guard resolves
cleanly (see memory ``alembic-idempotent-sql-needs-table-ownership``).

Forward-only: redesign jobs are ephemeral (regenerated on demand), so no data
is migrated to html_render_service — the new service starts with a fresh DB.

Revision ID: 0004_drop_redesign_tables
Revises: 0003_critique_jobs_auth_info
Create Date: 2026-06-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_drop_redesign_tables"
down_revision: str | None = "0003_critique_jobs_auth_info"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Children/leaf tables first, though every drop is independent + CASCADE-guarded.
_REDESIGN_TABLES = (
    "webhook_delivery_log",
    "batch_jobs",
    "redesign_variants",
    "custom_styles",
    "redesign_usage",
    "redesign_jobs",
)


def upgrade() -> None:
    for table in _REDESIGN_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


def downgrade() -> None:
    # Forward-only: the redesign domain (models + code) was removed from
    # analysis_service, so the tables cannot be faithfully recreated here.
    # Restoring the capability means reverting the code + re-running its
    # original create migrations, not running this downgrade.
    raise NotImplementedError(
        "0004_drop_redesign_tables is forward-only; the redesign domain moved to "
        "html_render_service. Revert the code change to restore the tables."
    )
