"""Add critique_jobs.auth_info JSONB column.

Carries the AuthInfo payload (type=none|basic|bearer|cookies + the
corresponding credential fields) that the orchestrator forwards on
behalf of the end-user so analysis_service can fetch authenticated
target pages. Nullable + defaults to NULL — pre-existing rows treat
absent value the same as type=none, which is the prior behaviour.

NOTE on at-rest storage: the credentials live in the column as
plaintext today, scoped by tenant_id (same model as DOWNSTREAM_
LICENSE_TOKEN reuse). At-rest encryption is tracked as a follow-up;
do not relax tenant_id filtering on /api/critique/{job_id} GETs.

Revision ID: 0003_critique_jobs_auth_info
Revises: 0002_redesign_concept_assets
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_critique_jobs_auth_info"
down_revision: str | None = "0002_redesign_concept_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "critique_jobs",
        sa.Column("auth_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("critique_jobs", "auth_info")
