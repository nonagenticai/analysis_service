"""Contract half of the ``critique_findings.tenant_id`` enrolment: NOT NULL + strict RLS.

``0008_critique_tenant_rls`` was deliberately EXPAND-ONLY — it added the column
NULLABLE so that OLD replicas, whose ``_save_findings`` does not stamp a tenant,
kept working while the run-migrations initContainer ran ahead of them. The price
of shipping it nullable is spelled out in 0008's docstring: a nullable
``tenant_id`` makes ``w2v_common.database.rls`` render the LOOSE "global-or-own"
policy set (``tenant_isolation_read/_ins/_upd/_del``), whose SELECT predicate is
``tenant_id IS NULL OR <own>`` — i.e. any NULL-tenant finding is readable by
EVERY tenant. Writes were always GUC-scoped; reads were not.

This revision closes that. Preconditions were verified live in prod before it was
written (0008 applied; 17745 rows / 4 distinct tenants / **0 NULLs**; 0 orphans
deleted by 0008; the loose 4-policy set standing), and the new code that stamps
``tenant_id`` on every insert is fully deployed — which is the gate 0008 named.

Step order, and why it is this order
------------------------------------
1. **Re-backfill from the parent job.** Belt and braces. Prod shows zero NULLs
   *now*, but 0008's backfill ran at initContainer time and old replicas kept
   serving for the rest of that rollout; any finding they finalised in that
   window landed NULL. Cheap, idempotent, and it must precede the delete so a
   recoverable row is recovered rather than destroyed.
2. **Count → log at WARNING → delete the remainder.** Whatever is still NULL
   after (1) has no parent ``critique_jobs`` row, so no tenant is derivable and
   the row is unreachable from every (tenant-scoped) read path. It cannot be
   kept: leaving it would fail step (3), and parking it under a sentinel tenant
   would invent ownership. The count is logged BEFORE the delete, on the
   ``alembic``-child logger, so a destructive step is auditable from the
   initContainer log — same pattern (and same reasoning) as 0008.
3. ``ALTER COLUMN tenant_id SET NOT NULL`` — the actual contract. Safe now:
   every replica stamps the column.
4. **``downgrade_statements`` THEN ``upgrade_statements``**, over a scoped
   MetaData with ``nullable=False``. The drop is not optional and a bare
   re-render is NOT equivalent: ``upgrade_statements`` on a non-nullable column
   emits only ``DROP POLICY IF EXISTS tenant_isolation`` before creating the
   strict policy — it never names the four loose policies, so
   ``tenant_isolation_read`` (``tenant_id IS NULL OR own``) would survive
   alongside it. Postgres ORs permissive policies together, so the loose SELECT
   policy would keep granting exactly the cross-tenant read this revision
   exists to remove, and the migration would look successful.
   ``downgrade_statements`` drops all five policy names, so drop-then-create
   leaves precisely one policy. Both run inside this migration's single
   transaction, so no window exists where the table is unprotected.

Ownership: every statement is unguarded (no ``IF EXISTS`` of our own — the ones
in the rendered DROP POLICY lines come from the shared renderer). Per memory
``alembic-idempotent-sql-needs-table-ownership`` a guarded ``ALTER`` silently
no-ops for a non-owning role, which on a security-boundary migration would
"succeed" while leaving the column nullable and the loose policy standing.
Failing loudly is the correct outcome.

Like 0008 this describes the table locally instead of importing the ORM model,
so the rendered DDL is pinned to the one table this revision changes.
``0005_rls``'s ``_TENANT_COLUMNS_ADDED_AFTER`` already excludes
``critique_findings`` and needs no change: the column is still added by 0008,
not by 0005.

Revision ID: 0009_critique_tenant_not_null
Revises: 0008_critique_tenant_rls

NOTE ON THE ID LENGTH: ``alembic_version.version_num`` is ``varchar(32)``. A
longer id applies the DDL and then fails the stamp, leaving a half-applied
migration (0008's first draft was 33 chars and CI caught it with
``StringDataRightTruncation``). This id is 28; the length is pinned by an
assertion in ``tests/unit/shared/test_migration_0009_not_null.py``.
"""

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from w2v_common.database.rls import downgrade_statements, upgrade_statements

revision: str = "0009_critique_tenant_not_null"
down_revision: str | None = "0008_critique_tenant_rls"
branch_labels = None
depends_on = None

# Child of the ``alembic`` logger (alembic.ini: level=INFO, console handler), so
# these lines land in the run-migrations initContainer log.
_log = logging.getLogger("alembic.critique_findings_tenant_not_null")

_TABLE = "critique_findings"

_BACKFILL = """
UPDATE critique_findings AS f
   SET tenant_id = j.tenant_id
  FROM critique_jobs AS j
 WHERE j.job_id = f.job_id
   AND f.tenant_id IS NULL
"""

_COUNT_NULLS = "SELECT count(*) FROM critique_findings WHERE tenant_id IS NULL"

_DROP_NULLS = "DELETE FROM critique_findings WHERE tenant_id IS NULL"


def _scoped_metadata() -> MetaData:
    """A fresh MetaData holding ONLY ``critique_findings`` (for per-table RLS DDL).

    ``nullable=False`` is the whole point of this revision: nullability is what
    selects the policy shape in ``w2v_common.database.rls``, so this MUST match
    the column ``upgrade()`` leaves behind (NOT NULL) → the strict single
    ``tenant_isolation`` policy, with no ``tenant_id IS NULL OR ...`` read
    escape. ``tests/unit/shared/test_migration_0009_not_null.py`` asserts on the
    SQL this renders; 0008's own ``_scoped_metadata()`` stays ``nullable=True``
    because it describes the schema 0008 left, which is history and immutable.
    """
    meta = MetaData()
    Table(_TABLE, meta, Column("tenant_id", String(255), nullable=False))
    return meta


def upgrade() -> None:
    # 1) Recover anything an old replica wrote NULL during 0008's rollout window.
    op.execute(_BACKFILL)

    # 2) Whatever is left is un-tenantable (no parent job). Count and log before
    #    destroying it, so the initContainer log shows what was dropped.
    nulls = op.get_bind().execute(sa.text(_COUNT_NULLS)).scalar_one()
    _log.warning(
        "critique_findings: deleting %s row(s) still NULL after re-backfill "
        "(no parent critique_jobs row → un-tenantable, unreachable from every "
        "tenant-scoped read path) before SET NOT NULL",
        nulls,
    )
    op.execute(_DROP_NULLS)

    # 3) Contract the column.
    op.alter_column(_TABLE, "tenant_id", existing_type=sa.String(length=255), nullable=False)

    # 4) Drop the loose global-or-own set FIRST, then render the strict policy.
    #    A bare re-render would leave tenant_isolation_read standing (see the
    #    module docstring) and permissive policies OR together.
    for stmt in downgrade_statements(_scoped_metadata()):
        op.execute(stmt)
    for stmt in upgrade_statements(_scoped_metadata()):
        op.execute(stmt)


def downgrade() -> None:
    # Back to exactly what 0008 left: nullable column + the global-or-own set.
    for stmt in downgrade_statements(_scoped_metadata()):
        op.execute(stmt)
    op.alter_column(_TABLE, "tenant_id", existing_type=sa.String(length=255), nullable=True)
    for stmt in upgrade_statements(_nullable_metadata()):
        op.execute(stmt)


def _nullable_metadata() -> MetaData:
    """0008's view of the table — used only by ``downgrade()`` to restore its policies."""
    meta = MetaData()
    Table(_TABLE, meta, Column("tenant_id", String(255), nullable=True))
    return meta
