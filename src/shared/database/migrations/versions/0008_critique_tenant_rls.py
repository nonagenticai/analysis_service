"""Enrol ``critique_findings`` in tenant RLS by giving it its own ``tenant_id``.

``critique_findings`` was the one tenant-owned table in this service with no
``tenant_id`` column, so ``w2v_common.database.rls.tenant_table_names`` never
selected it and it sat entirely outside RLS while the cluster runs
``TENANT_RLS_MODE=enforce``. Isolation was application-level only: every read
had to remember to join the parent ``critique_jobs`` for tenant scope, and a
single forgotten join leaked findings across tenants.

**Adding the column is what enrols the table** — no bespoke policy SQL here. The
DDL is rendered from the single source ``w2v_common.database.rls``, the same
statements the ``create_all`` ``register_rls_ddl`` event applies in tests.
ENABLE-not-FORCE → inert until the runtime connects as the restricted non-owner
role ``w2v_analysis_app``.

EXPAND ONLY — this revision deliberately does NOT ``SET NOT NULL``
=================================================================
This runs in the ``run-migrations`` initContainer, i.e. BEFORE any new pod is
Ready and while the OLD replicas keep serving. Old ``_save_findings`` does not
know about the column, so a ``SET NOT NULL`` here makes every old-pod finding
INSERT raise ``IntegrityError`` at flush inside ``_phase_finalize_completed``.
That is NOT absorbed by the narrow ``except OSError, ValueError, RuntimeError,
TimeoutError`` around ``db.add()`` — the whole finalize transaction rolls back
and the job wedges in ``running``. So the schema change is split expand/contract:

* **this revision (expand):** add the column NULLABLE, backfill from
  ``critique_jobs``, delete true orphans, index it, render the RLS DDL. Both old
  and new code run fine against this schema.
* **a follow-up revision (contract), only after the new code is fully deployed:**
  see ``CONTRACT FOLLOW-UP`` below.

**Honest consequence of shipping the column nullable.** ``_scoped_metadata()``
describes ``tenant_id`` as nullable, so the renderer emits the looser
"global-or-own" policy set (``tenant_isolation_read/_ins/_upd/_del``) instead of
the strict ``tenant_isolation`` policy: a row with ``tenant_id IS NULL`` is
readable by EVERY tenant. Writes are still GUC-scoped. The backfill leaves zero
NULL rows, and new code always stamps the parent job's tenant — but an OLD
replica that finalizes a critique during the rolling window writes NULL, and
those findings are globally readable until the contract migration lands. This PR
therefore delivers **enrolment plus a named follow-up**, not strict isolation.

CONTRACT FOLLOW-UP — **LANDED as ``0009_critique_tenant_not_null``**
---------------------------------------------------------------------------
The list below is the spec that revision implements; it is kept for the
rationale. Note step 5 was resolved differently: 0008's ``_scoped_metadata()``
and its test stay ``nullable=True`` forever, because they describe the schema
*this* revision leaves, which is immutable history. 0009 carries its own
``nullable=False`` metadata and its own DDL test.

Once every replica runs code that stamps ``tenant_id`` (verify: no pod of the
previous image is Running), a ``0009`` must, in this order:

1. ``UPDATE critique_findings f SET tenant_id = j.tenant_id FROM critique_jobs j
   WHERE j.job_id = f.job_id AND f.tenant_id IS NULL`` — re-backfill the rows the
   rolling window wrote NULL;
2. ``DELETE FROM critique_findings WHERE tenant_id IS NULL`` — count and log
   first, same as here;
3. ``ALTER TABLE critique_findings ALTER COLUMN tenant_id SET NOT NULL``;
4. drop the loose policies and re-render the STRICT set — i.e. run
   ``downgrade_statements`` then ``upgrade_statements`` over a scoped MetaData
   whose ``tenant_id`` is ``nullable=False`` (the loose policies are named
   differently from the strict one, so a bare re-render would leave the
   NULL-readable ``tenant_isolation_read`` policy in place beside it);
5. flip ``_scoped_metadata()``'s ``nullable=True`` expectation in
   ``tests/unit/shared/test_migration_0008_rls_ddl.py`` to the 0009 equivalent.

Ownership: every statement here is unguarded (no ``IF EXISTS``). That is
deliberate — per memory ``alembic-idempotent-sql-needs-table-ownership`` an
``ALTER ... IF EXISTS`` silently no-ops when the migrating role does not own the
table, which would leave the column (and therefore RLS) quietly unapplied. The
migrating role does own ``critique_findings`` (created by 0001 / ``create_all``,
same as the tables 0004 drops); if that ever stops holding, this fails loudly,
which is the correct outcome for a security-boundary migration.

Unlike 0006/0007 this migration DOES touch a table already on ``Base.metadata``,
so it describes the table locally for the RLS renderer and 0005 is pinned to the
schema as of 0005 (see ``_TENANT_COLUMNS_ADDED_AFTER`` there) — otherwise 0005
would try to create a policy on a column this revision has not added yet.

Revision ID: 0008_critique_tenant_rls
Revises: 0007_multireplica_safety

NOTE ON THE ID LENGTH: ``alembic_version.version_num`` is ``varchar(32)``, so a
revision id longer than that makes the stamp fail *after* the DDL has already
run — the migration applies and then cannot record itself. The first draft used
``0008_critique_findings_tenant_rls`` (33 chars) and CI caught it with
``StringDataRightTruncation``. Keep ids short, like the rest of the chain
(``0007_multireplica_safety`` is 24).
"""

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from w2v_common.database.rls import downgrade_statements, upgrade_statements

revision: str = "0008_critique_tenant_rls"
down_revision: str | None = "0007_multireplica_safety"
branch_labels = None
depends_on = None

# Child of the ``alembic`` logger (alembic.ini: level=INFO, console handler), so
# these lines land in the run-migrations initContainer log.
_log = logging.getLogger("alembic.critique_findings_tenant_rls")

_TABLE = "critique_findings"
_INDEX = "ix_critique_findings_tenant_id"

_BACKFILL = """
UPDATE critique_findings AS f
   SET tenant_id = j.tenant_id
  FROM critique_jobs AS j
 WHERE j.job_id = f.job_id
   AND f.tenant_id IS NULL
"""

_COUNT_ORPHANS = "SELECT count(*) FROM critique_findings WHERE tenant_id IS NULL"

# Orphans = findings whose parent ``critique_jobs`` row is GONE, so no tenant is
# derivable. The dominant source is NOT retention: it is
# ``critique_service/lifecycle.py::delete_job``, which did ``await db.delete(job)``
# with no FK, no ON DELETE CASCADE and no findings delete — so every job any
# tenant ever deleted, for the life of the table, left its findings behind
# permanently. (``prune_critique_data`` in 0005, which prunes jobs and findings on
# independent cutoffs, is the smaller boundary case.) ``delete_job`` is fixed in
# this same PR, so the orphan set stops regrowing after this runs.
#
# An orphan is unreachable through every read path (all of which start from a
# tenant-scoped job) and un-tenantable, so it is dropped rather than parked under
# a sentinel tenant — leaving it NULL would keep it readable by every tenant under
# the global-or-own policy. The row count is unknown up front and potentially
# large, so it is counted and logged before the (single, unbatched) delete: a
# surprising number is visible in the initContainer log next to the delete.
_DROP_ORPHANS = "DELETE FROM critique_findings WHERE tenant_id IS NULL"


def _scoped_metadata() -> MetaData:
    """A fresh MetaData holding ONLY ``critique_findings`` (for per-table RLS DDL).

    Describing the table locally (rather than importing the ORM model) keeps this
    revision's rendered DDL pinned to the one table it changes, exactly like
    ``0006``/``0007`` — and pins the NULLABILITY, which is what selects the policy
    shape. ``nullable=True`` here MUST match the column this revision actually
    leaves behind (see the EXPAND ONLY note): nullable → the looser
    "global-or-own" policy set. The contract revision flips both together.
    ``tests/unit/shared/test_migration_0008_rls_ddl.py`` asserts on the SQL this
    renders, so flipping one without the other fails.
    """
    meta = MetaData()
    Table(_TABLE, meta, Column("tenant_id", String(255), nullable=True))
    return meta


def upgrade() -> None:
    # 1) nullable — and it STAYS nullable this deploy (old pods still write rows
    #    without the column while this initContainer runs).
    op.add_column(_TABLE, sa.Column("tenant_id", sa.String(length=255), nullable=True))

    # 2) backfill from the parent job, then drop what has no parent left — counted
    #    and logged first so the delete is auditable from the initContainer log.
    op.execute(_BACKFILL)
    orphans = op.get_bind().execute(sa.text(_COUNT_ORPHANS)).scalar_one()
    _log.warning(
        "critique_findings: deleting %s orphan row(s) with no parent critique_jobs row "
        "(un-tenantable, unreachable from every tenant-scoped read path)",
        orphans,
    )
    op.execute(_DROP_ORPHANS)

    op.create_index(_INDEX, _TABLE, ["tenant_id"])

    # 3) enrol the table. Nullable column → global-or-own policy set; the contract
    #    revision replaces it with the strict one.
    for stmt in upgrade_statements(_scoped_metadata()):
        op.execute(stmt)


def downgrade() -> None:
    for stmt in downgrade_statements(_scoped_metadata()):
        op.execute(stmt)
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "tenant_id")
