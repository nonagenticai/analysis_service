"""Phase D RLS — enable row-level security on the tenant-scoped tables.

Applies the SAME policies the ``create_all`` ``register_rls_ddl`` event applies in
tests, rendered from the single source ``w2v_common.database.rls`` (all tenant_id
tables: varchar → text predicate; the one nullable tenant_id on critiques →
global-or-own). ENABLE-not-FORCE → inert until the runtime connects as the
restricted non-owner role ``w2v_analysis_app`` with ``TENANT_RLS_MODE=enforce``.

Also creates ``prune_review_jobs(timestamptz)`` — a SECURITY DEFINER resolver owned
by the migration user (bypasses ENABLE) so the cross-tenant background cleanup loop
(``review/services/cleanup.py``) still deletes old jobs across ALL tenants under
enforce (a non-bypass role with no GUC would otherwise fail-closed → delete 0).

**This migration renders from live ``Base.metadata``, so it is pinned.** A later
revision that adds ``tenant_id`` to an EXISTING table enrols that table in RLS —
and would retroactively make THIS revision emit ``CREATE POLICY ... USING
(tenant_id = ...)`` against a column that does not exist yet, breaking every
fresh ``alembic upgrade head`` (prod already ran 0005, so only fresh DBs — i.e.
CI — see the break). ``_TENANT_COLUMNS_ADDED_AFTER`` records those late columns
so the DDL rendered here stays the schema **as of 0005**; the later revision
applies the policy itself. This is the column-level analog of the "do not import
the model at module scope" note in ``0006_icp_extractions``.

Revision ID: 0005_rls
Revises: 0004_drop_redesign_tables
"""

from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from w2v_common.database.rls import TENANT_COLUMN, downgrade_statements, upgrade_statements

# Import every domain model so Base.metadata is fully populated before we render
# the per-table policy DDL (alembic env.py imports only Base).
from src.domains.analyze import models as _analyze_models  # noqa: F401
from src.domains.brand import models as _brand_models  # noqa: F401
from src.domains.critique import models as _critique_models  # noqa: F401
from src.domains.review import models as _review_models  # noqa: F401
from src.shared.database.base import Base

revision: str = "0005_rls"
down_revision: str | None = "0004_drop_redesign_tables"
branch_labels = None
depends_on = None

# Tables whose ``tenant_id`` column was added by a revision AFTER this one — they
# were NOT tenant tables at 0005 and must not be rendered here. Append (never
# remove) an entry whenever a later migration adds ``tenant_id`` to a table that
# already existed at 0005.
_TENANT_COLUMNS_ADDED_AFTER: frozenset[str] = frozenset({"critique_findings"})

# ⭐ FROZEN TYPE. At 0005 every ``tenant_id`` was ``character varying(255)``, and
# ``w2v_common.database.rls._predicate`` picks its cast off the column TYPE —
# ``isinstance(col.type, String)`` compares text-to-text, anything else casts the
# GUC ``::uuid``. This used to read ``col.type`` straight off the live model,
# which meant the predicate this immutable revision emits was decided by whatever
# ``models.py`` says TODAY. ``0013_tenant_id_uuid`` is where that broke: the
# moment the models declared ``PgUUID``, a FRESH ``alembic upgrade head`` died
# right here with ``operator does not exist: character varying = uuid`` — the
# columns are still varchar for another eight revisions. An already-migrated
# database (prod) was completely unaffected, so the defect was invisible to every
# check except a from-scratch bootstrap. Reproduced and mutation-checked on
# postgres 18 before this line was pinned.
#
# This is the same class of bug as ``_TENANT_COLUMNS_ADDED_AFTER`` above (live
# metadata leaking into frozen history), one level down: table set, then column
# type. Nullability is still read live and is safe by the same reasoning 0009
# used — a revision that flips nullability carries its OWN scoped metadata
# (0008/0009) rather than editing this one — but if a future revision ever makes
# an existing 0005-era ``tenant_id`` nullable (or not), pin it here too.
_TENANT_TYPE_AS_OF_0005 = String(255)


def _metadata_as_of_0005() -> MetaData:
    """``Base.metadata`` reduced to the tenant tables that existed at this revision.

    Only the table name plus the ``tenant_id`` column's type and nullability feed
    the renderer (see ``w2v_common.database.rls``), so a minimal mirror is exact —
    the same ``_scoped_metadata`` shape 0006/0007 use. The TYPE is pinned to the
    ``varchar(255)`` of this era rather than copied from the live model; see the
    ``_TENANT_TYPE_AS_OF_0005`` note above for the fresh-bootstrap failure that
    caused.
    """
    meta = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name in _TENANT_COLUMNS_ADDED_AFTER or TENANT_COLUMN not in table.columns:
            continue
        col = table.columns[TENANT_COLUMN]
        Table(
            table.name, meta, Column(TENANT_COLUMN, _TENANT_TYPE_AS_OF_0005, nullable=col.nullable)
        )
    return meta


_PRUNE_REVIEW_FN = """
CREATE OR REPLACE FUNCTION prune_review_jobs(p_cutoff timestamptz) RETURNS bigint
  LANGUAGE sql SECURITY DEFINER SET search_path FROM CURRENT AS $fn$
    WITH d AS (
      DELETE FROM review_jobs
      WHERE status IN ('completed', 'failed') AND updated_at < p_cutoff
      RETURNING 1
    ) SELECT count(*) FROM d;
  $fn$
"""

# Cross-tenant critique retention (review/services/cleanup analog for the critique
# domain). SECURITY DEFINER (owner bypasses ENABLE) so the background retention loop
# prunes across ALL tenants under enforce.
_PRUNE_CRITIQUE_FN = """
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


def upgrade() -> None:
    for stmt in upgrade_statements(_metadata_as_of_0005()):
        op.execute(stmt)
    op.execute(_PRUNE_REVIEW_FN)
    op.execute("REVOKE EXECUTE ON FUNCTION prune_review_jobs(timestamptz) FROM PUBLIC")
    op.execute(_PRUNE_CRITIQUE_FN)
    op.execute(
        "REVOKE EXECUTE ON FUNCTION prune_critique_data(timestamptz, timestamptz) FROM PUBLIC"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS prune_critique_data(timestamptz, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS prune_review_jobs(timestamptz)")
    for stmt in downgrade_statements(_metadata_as_of_0005()):
        op.execute(stmt)
