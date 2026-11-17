"""ICP extractions audit table + RLS.

Creates ``icp_extractions`` (one row per successful ICP extraction) and applies
the standard tenant-isolation RLS policy to it, rendered from the single source
``w2v_common.database.rls`` — the SAME policy the ``create_all``
``register_rls_ddl`` event applies in tests. ENABLE-not-FORCE → inert until the
runtime connects as the restricted non-owner role with ``TENANT_RLS_MODE=enforce``.

NOTE: this migration deliberately does NOT import the ORM model at module scope.
Alembic imports every version file to build the revision graph, and importing the
model would register ``icp_extractions`` on the shared ``Base.metadata`` before any
migration runs — which would make the earlier ``0005_rls`` migration (it renders RLS
from live ``Base.metadata``) try to ``ALTER TABLE icp_extractions`` before this
migration creates it. Instead the table is described locally, both for
``create_table`` and for a minimal RLS-rendering ``MetaData`` (only the ``tenant_id``
column — non-nullable → strict predicate — matters to the renderer).

Revision ID: 0006_icp_extractions
Revises: 0005_rls
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from w2v_common.database.rls import downgrade_statements, upgrade_statements

revision: str = "0006_icp_extractions"
down_revision: str | None = "0005_rls"
branch_labels = None
depends_on = None


def _scoped_metadata() -> MetaData:
    """A fresh MetaData holding ONLY ``icp_extractions`` (for per-table RLS DDL).

    Non-nullable ``tenant_id`` → the renderer emits the strict tenant-isolation
    policy (not the nullable global-or-own variant).
    """
    meta = MetaData()
    Table("icp_extractions", meta, Column("tenant_id", String(255), nullable=False))
    return meta


def upgrade() -> None:
    op.create_table(
        "icp_extractions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_icp_extractions_tenant_id", "icp_extractions", ["tenant_id"])
    for stmt in upgrade_statements(_scoped_metadata()):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in downgrade_statements(_scoped_metadata()):
        op.execute(stmt)
    op.drop_index("ix_icp_extractions_tenant_id", table_name="icp_extractions")
    op.drop_table("icp_extractions")
