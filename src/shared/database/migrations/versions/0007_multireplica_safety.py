"""Multi-replica safety: atomic critique-schedule claim + durable analyze review jobs.

Two independent multi-replica hazards, fixed together:

1. ``claim_due_critique_schedules(timestamptz)`` — a SECURITY DEFINER resolver
   (owner-owned → bypasses RLS ENABLE, never FORCE) that atomically claims due
   ``critique_schedules`` rows via ``UPDATE ... WHERE id IN (SELECT ... FOR
   UPDATE SKIP LOCKED)`` and bumps ``next_run_at`` in one statement, so the
   per-replica scheduler loop fires each due schedule exactly once instead of N
   times. The DDL is imported from the scheduler module so the SQL interval
   periods stay generated from the single-source ``CRON_PRESETS``.

2. ``analyze_review_jobs`` — the durable backing store for the analyze-embedded
   ``/api/analyze/review`` sub-jobs (previously in-memory only → cross-pod 404).
   Standard tenant-isolation RLS is applied from ``w2v_common.database.rls`` —
   the SAME policy the ``create_all`` ``register_rls_ddl`` event applies in
   tests. ENABLE-not-FORCE → inert until the runtime connects as the restricted
   non-owner role with ``TENANT_RLS_MODE=enforce``.

Like ``0006_icp_extractions`` this migration does NOT import the ORM model at
module scope — describing the table locally keeps ``analyze_review_jobs`` off
``Base.metadata`` during ``0005_rls`` (which renders RLS from live metadata and
would otherwise ALTER a not-yet-created table).

Revision ID: 0007_multireplica_safety
Revises: 0006_icp_extractions
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import JSONB
from w2v_common.database.rls import downgrade_statements, upgrade_statements

revision: str = "0007_multireplica_safety"
down_revision: str | None = "0006_icp_extractions"
branch_labels = None
depends_on = None


# ⭐ FROZEN LITERAL. This used to be
# ``from src.domains.critique.services.scheduler import CLAIM_DUE_SCHEDULES_DDL``
# and the module docstring above still explains why (single-source CRON_PRESETS).
# That import made a MIGRATION -- immutable history -- render itself from code
# that keeps moving, and 0012 is where it broke: 0012 adds
# ``critique_schedules.tenant_source`` and widens the resolver's ``RETURNS TABLE``
# to carry it, so on a database built from scratch this migration would have
# executed the NEW body, whose ``RETURNING s.tenant_source`` names a column that
# does not exist for another five revisions. ``LANGUAGE sql`` bodies are parsed
# and validated at CREATE time, so that is a hard failure, not a latent one.
#
# The text below is exactly what this revision rendered when it was written
# (CRON_PRESETS as of 0007). It must NEVER be regenerated: what 0007 creates is
# superseded by 0012 anyway.
_CLAIM_DUE_SCHEDULES_DDL_AT_0007 = """
CREATE OR REPLACE FUNCTION claim_due_critique_schedules(p_now timestamptz)
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


def _scoped_metadata() -> MetaData:
    """A fresh MetaData holding ONLY ``analyze_review_jobs`` (for per-table RLS DDL).

    Non-nullable ``tenant_id`` → the renderer emits the strict tenant-isolation
    policy (not the nullable global-or-own variant).
    """
    meta = MetaData()
    Table("analyze_review_jobs", meta, Column("tenant_id", String(255), nullable=False))
    return meta


def upgrade() -> None:
    # 1) Atomic single-replica claim resolver for critique schedules.
    op.execute(_CLAIM_DUE_SCHEDULES_DDL_AT_0007)
    op.execute("REVOKE EXECUTE ON FUNCTION claim_due_critique_schedules(timestamptz) FROM PUBLIC")

    # 2) Durable store for analyze-embedded review jobs.
    op.create_table(
        "analyze_review_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("review_job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("review_types", JSONB(), nullable=True),
        sa.Column("reviews", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processing_time_seconds", sa.Float(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_analyze_review_jobs_review_job_id",
        "analyze_review_jobs",
        ["review_job_id"],
        unique=True,
    )
    op.create_index("ix_analyze_review_jobs_tenant_id", "analyze_review_jobs", ["tenant_id"])
    for stmt in upgrade_statements(_scoped_metadata()):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in downgrade_statements(_scoped_metadata()):
        op.execute(stmt)
    op.drop_index("ix_analyze_review_jobs_tenant_id", table_name="analyze_review_jobs")
    op.drop_index("ix_analyze_review_jobs_review_job_id", table_name="analyze_review_jobs")
    op.drop_table("analyze_review_jobs")
    op.execute("DROP FUNCTION IF EXISTS claim_due_critique_schedules(timestamptz)")
