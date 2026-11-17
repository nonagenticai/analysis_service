"""Replace redesign concept-image path arrays with structured asset payloads.

Revision ID: 0002_redesign_concept_assets
Revises: a3f7b1c9d4e2
Create Date: 2026-03-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_redesign_concept_assets"
down_revision: str | None = "a3f7b1c9d4e2"  # pragma: allowlist secret
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("redesign_jobs", "concept_image_paths")
    op.drop_column("redesign_variants", "concept_image_paths")
    op.add_column(
        "redesign_jobs",
        sa.Column("concept_image_assets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "redesign_variants",
        sa.Column("concept_image_assets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("redesign_variants", "concept_image_assets")
    op.drop_column("redesign_jobs", "concept_image_assets")
    op.add_column(
        "redesign_variants",
        sa.Column("concept_image_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "redesign_jobs",
        sa.Column("concept_image_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
