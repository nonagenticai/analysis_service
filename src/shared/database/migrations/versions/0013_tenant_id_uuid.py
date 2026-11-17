"""Flip all 20 ``tenant_id`` columns from ``character varying(255)`` to native ``uuid``.

Fleet-wide ruling: ``tenant_id`` is a real ``uuid``, never text, **never a
sentinel**. Every value must reference a real row in ``licenses.tenants``.

THE DIRTY VALUES — EXACTLY TWO, both mapped to REGISTERED TENANTS
=================================================================
This is the dirtiest database in the fleet: 302 rows across 10 of the 20 tables
carried a non-uuid ``tenant_id``. Measured live on the prod primary 2026-08-27.
There are exactly two distinct offending strings and both map to a tenant that
really exists — see :data:`LEGACY_TENANT_MAP`:

``n8n-dev`` (260 rows)
    A REGISTERED TENANT NAME, not a sentinel — verified live in
    ``licenses.tenants``. Its uuid is ``019de842-c92a-7e37-8fdd-eb0d8c5396f9``.
    Someone wrote the name where the id belonged; the rename is the whole fix.

``anonymous`` (42 rows: 21 in ``analysis_jobs``, 21 in ``analysis_usage``)
    April-2026 dev/smoke traffic (example.com x6, dev.specralabs.com x12,
    loginfix…svc.cluster.local x2, room15.amsterdam x1). It is NOT a tenant and
    never was. **USER RULING, binding:** these rows are re-homed onto the
    registered ``fr-test`` tenant ``0195f48b-d000-7000-8000-000000000099``,
    uniformly across BOTH tables. Uniformly, because ``analysis_usage`` has no
    ``domain`` column — there is nothing to bucket on there, so any per-domain
    split would silently disagree between the two tables about who owns the same
    day's traffic. A sentinel/nil uuid was considered and **rejected**: a
    sentinel is not a tenant, RLS would bind it like any other tenant, and
    whoever held it would read every row parked under it.

Both mappings are spelled out in an explicit module-level dict rather than
derived, so the exact provenance of every rewritten row is auditable from this
file alone, forever.

COLLISIONS: CHECKED CONCRETELY, NONE
====================================
A name→uuid backfill collapses distinct strings onto one value, which can violate
a unique constraint *during* the UPDATE. Each constraint touching ``tenant_id``
was checked against the live rows rather than assumed:

* ``review_usage.uq_usage_tenant_date (tenant_id, date)`` — its 4 dirty rows are
  all ``n8n-dev`` on 4 DISTINCT dates (2026-04-12/13/18/21) → 4 distinct
  ``(uuid, date)`` pairs. No other row already holds the n8n-dev uuid on those
  dates.
* ``brands.uq_brand_tenant_domain (tenant_id, domain)`` — 2 ``n8n-dev`` rows on
  distinct domains (``dev.specralabs.com``, ``room15.amsterdam``); no existing
  row holds the n8n-dev uuid on either domain.
* ``analysis_usage`` and ``critique_usage`` carry only a plain
  ``ix_*_tenant_id``; there is no unique constraint to collide on. This matters
  for ``analysis_usage`` in particular: 22 rows collapse onto the ``fr-test``
  uuid there and would have collided had it carried the ``review_usage`` shape.

THE RESOLVER — MEASURED, NOT ASSUMED
====================================
``claim_due_critique_schedules(p_now timestamptz)`` declares
``RETURNS TABLE(schedule_id varchar, tenant_id varchar, tenant_source varchar,
url text, categories jsonb)``, and ``ALTER COLUMN ... TYPE`` does not revalidate a
``LANGUAGE sql`` body. The expectation going in was that every call would then die
with ``structure of query does not match function result type``, silently killing
the 60-second critique scheduler — the exact outage 0011 and 0012 were written to
end.

**That was checked on postgres 18 and it is NOT what happens.** ``uuid`` has an
assignment cast to ``varchar``, so the old signature keeps working and returns the
uuid rendered as text. Recorded here rather than quietly dropped, because the
claim is load-bearing and a future reader must not re-derive the wrong reason.

The resolver is still DROPped and re-created with ``tenant_id uuid``, for two
reasons that DO hold:

* ``scheduler.CLAIM_DUE_SCHEDULES_DDL`` — the same DDL, installed by the
  ``create_all`` test path — now declares ``uuid``. If this migration left the
  varchar signature standing, a migration-built database and a ``create_all``
  database would disagree, and the test path's ``CREATE OR REPLACE`` would fail
  outright with ``cannot change return type of existing function``.
* The Python surface. A ``varchar`` return hands psycopg a ``str``; a ``uuid``
  return hands it a ``uuid.UUID``. ``_CLAIM_SQL`` therefore selects
  ``tenant_id::text`` so ``_fire_schedule`` keeps receiving the ``str`` that
  ``create_job`` and ``set_config`` require. Declaring the truth in the signature
  and casting once at the query is the version that cannot drift.

``CREATE OR REPLACE`` cannot change a return type, so the change must be a DROP +
CREATE. Dropping also drops the ACL, hence the ``REVOKE`` + guarded ``GRANT``
afterwards, same as 0012.

The other three SECURITY DEFINER resolvers were inspected against their live
definitions and need NO change: ``prune_review_jobs(timestamptz) → bigint``,
``prune_critique_data(timestamptz, timestamptz) → jsonb`` and
``reap_stale_critique_jobs(interval) → integer`` neither return nor take a
``tenant_id``, and none of their bodies name the column.

THE LATENT BUG THIS CLOSES
==========================
0012 gave ``critique_schedules`` its ``tenant_source`` provenance column while
its ``tenant_id`` stayed varchar. That table has 0 rows in prod, so the widened
resolver has NEVER run against real data — it passed every check by being empty.
The migration test seeds a schedule and calls the resolver after the flip.

ORDER IS LOAD-BEARING
=====================
1. **DROP the resolver.** Not required by the ALTER (Postgres does not track
   dependencies inside a SQL function's string body, and the old varchar
   signature does not in fact break — see above), but done first so the whole
   change lands as one transaction with no window in which a scheduler tick sees
   a signature that disagrees with the column.
2. **DROP every RLS policy.** Postgres refuses ``ALTER COLUMN ... TYPE`` outright
   on a column named by a policy expression ("cannot alter type of a column used
   in a policy definition"). 23 live policies in TWO shapes (see below).
3. **Backfill while the column is still text.** After the cast the string
   ``n8n-dev`` can no longer be matched or written; ``'n8n-dev'::uuid`` raises.
4. **Guard, fail loud.** A ``DO`` block counts every remaining non-NULL, non-uuid
   value and ``RAISE EXCEPTION``s with the count and a sample. Deliberately NOT a
   ``WHERE tenant_id ~ ...`` filter on the ALTER: a filter would quietly leave
   unconvertible rows behind or drop them, and silent data loss on a tenant key
   is far worse than a failed migration. If this fires, a new dirty value
   appeared after the 2026-08-27 measurement and a human must map it to a real
   tenant uuid and extend :data:`LEGACY_TENANT_MAP`.
5. **ALTER.** Nullability is preserved; indexes and unique constraints rebuild.
6. **Re-create the policies with the GUC cast ``::uuid``.** The GUC is always
   text, so against a ``uuid`` column the old text-to-text predicate is rejected
   at ``CREATE POLICY`` time with ``operator does not exist: uuid = text``
   (verified on postgres 18) — loud, and the mirror image of what a ``::uuid``
   cast does against a still-varchar column, which is the fresh-bootstrap failure
   ``0005_rls`` had to be pinned for. What is NOT loud is an unbound GUC: it folds
   to NULL and the predicate is simply false, so the pod reads ZERO rows with no
   error at all. Both the matching and the non-matching direction, plus the
   unbound case, are exercised against a real database before this ships.
7. **Re-create the resolver** with ``tenant_id uuid``, then REVOKE + guarded GRANT.

TWO POLICY SHAPES — PRESERVED EXACTLY
=====================================
19 tables have a NOT NULL ``tenant_id`` → the single strict ``tenant_isolation``
policy. ``audit_logs`` is the exception: its ``tenant_id`` is NULLABLE
(``Mapped[str | None]``), so it carries the 4-policy "global-or-own" set
(``tenant_isolation_read`` / ``_ins`` / ``_upd`` / ``_del``), under which a
``tenant_id IS NULL`` row stays readable by EVERY tenant. That is deliberate and
is NOT changed here — this revision changes a column TYPE, not a security
posture. Both shapes are rendered from the single source
``w2v_common.database.rls`` (whose ``_predicate`` picks the cast off
``isinstance(col.type, String)``, and whose policy-set choice keys off
nullability), so the migration and the ``create_all`` test path cannot drift.

The table list and the nullability of each column are pinned as module-level
LITERALS rather than read from live ``Base.metadata`` — a migration is immutable
history, and rendering from a moving model is precisely what broke 0007 (see the
frozen-literal note there). ``downgrade_statements`` drops all five possible
policy names per table unconditionally, so the drop is exhaustive across both
shapes regardless of which one a given database happens to hold.

DOWNGRADE
=========
Returns the columns to ``character varying(255)``, restores the text-comparison
policies in each table's own shape, and restores the resolver's ``varchar``
signature as a FROZEN LITERAL (a downgrade can never render from the live
scheduler module — that module now declares ``uuid``, and a ``LANGUAGE sql`` body
is validated at CREATE time against the actual column type).

The name→uuid backfill is deliberately NOT reversed: those uuids are the correct,
canonical identities and ``n8n-dev``/``anonymous`` were the defect. Going back to
varchar does not make the old strings right again; re-writing them would just
re-dirty the column.

Revision ID: 0013_tenant_id_uuid
Revises: 0012_schedule_tenant_source
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from w2v_common.database.rls import downgrade_statements, upgrade_statements

from src.shared.database.resolver_grants import guarded_grant_statement

revision: str = "0013_tenant_id_uuid"
down_revision: str | None = "0012_schedule_tenant_source"
branch_labels = None
depends_on = None

#: The 19 tables whose ``tenant_id`` is NOT NULL → strict ``tenant_isolation``.
#: A frozen literal: this is the schema as of this revision, not a live read.
TENANT_TABLES_STRICT: tuple[str, ...] = (
    "analysis_jobs",
    "analysis_usage",
    "analyze_review_jobs",
    "brand_extraction_logs",
    "brand_overrides",
    "brand_versions",
    "brands",
    "critique_batches",
    "critique_findings",
    "critique_jobs",
    "critique_schedules",
    "critique_usage",
    "finding_suppressions",
    "icp_extractions",
    "logo_assets",
    "review_batches",
    "review_jobs",
    "review_usage",
    "user_context_profiles",
)

#: The one table with a NULLABLE ``tenant_id`` → the 4-policy global-or-own set.
#: Its nullability is load-bearing (it selects the policy shape) and is preserved
#: by ``ALTER COLUMN ... TYPE``; the test asserts it is still nullable afterwards.
TENANT_TABLES_NULLABLE: tuple[str, ...] = ("audit_logs",)

#: All 20, in a stable order, for the backfill / guard / ALTER loops.
TENANT_TABLES: tuple[str, ...] = TENANT_TABLES_STRICT + TENANT_TABLES_NULLABLE

#: Every non-uuid ``tenant_id`` value in prod → the REGISTERED tenant it belongs
#: to. Explicit and exhaustive by design: no sentinel, no nil uuid, no derived
#: rule. Both targets are real rows in ``licenses.tenants`` (verified live
#: 2026-08-27). See the module docstring for the provenance of each.
LEGACY_TENANT_MAP: dict[str, str] = {
    # A registered tenant NAME written where its id belonged. 260 rows.
    "n8n-dev": "019de842-c92a-7e37-8fdd-eb0d8c5396f9",
    # April-2026 dev/smoke traffic, re-homed onto the registered `fr-test`
    # tenant by explicit user ruling. 42 rows (analysis_jobs + analysis_usage).
    "anonymous": "0195f48b-d000-7000-8000-000000000099",
}

_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Guard template. Interpolated ONLY with a name from TENANT_TABLES (module-level
# literals) and the uuid regex — never with anything caller- or row-derived.
_GUARD_TEMPLATE = """
DO $$
DECLARE
  bad_count bigint;
  sample text;
BEGIN
  SELECT count(*), min(tenant_id) INTO bad_count, sample
    FROM {table}
   WHERE tenant_id IS NOT NULL AND tenant_id !~ '{uuid_re}';
  IF bad_count > 0 THEN
    RAISE EXCEPTION
      '{table}.tenant_id: % row(s) are not a parseable uuid (e.g. %). Refusing to '
      'ALTER to uuid. Map each value to a REAL tenant uuid from licenses.tenants '
      'and add it to LEGACY_TENANT_MAP in 0013, then re-run. Do NOT skip them with '
      'a WHERE and do NOT invent a sentinel.',
      bad_count, sample;
  END IF;
END $$
"""

_DROP_CLAIM = "DROP FUNCTION IF EXISTS claim_due_critique_schedules(timestamptz)"
_REVOKE_CLAIM = "REVOKE EXECUTE ON FUNCTION claim_due_critique_schedules(timestamptz) FROM PUBLIC"

#: Resolver with ``tenant_id uuid``. Frozen literal — never regenerate from
#: ``scheduler.CLAIM_DUE_SCHEDULES_DDL`` (see the note in 0007 for why).
#: Byte-identical to 0012's body apart from the ``tenant_id`` return type.
_CLAIM_TENANT_UUID = """
CREATE FUNCTION claim_due_critique_schedules(p_now timestamptz)
  RETURNS TABLE(schedule_id varchar, tenant_id uuid, tenant_source varchar, url text, categories jsonb)
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

#: Resolver with ``tenant_id varchar`` — exactly 0012's ``_CLAIM_WITH_TENANT_SOURCE``.
#: Restored by ``downgrade``. Frozen literal, for the reason in the module docstring.
_CLAIM_TENANT_VARCHAR = """
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


def _scoped_metadata(*, as_uuid: bool) -> MetaData:
    """A local MetaData describing ONLY ``tenant_id`` on the 20 tenant tables.

    Only the table name plus the ``tenant_id`` column's TYPE and NULLABILITY feed
    ``w2v_common.database.rls`` — type picks the GUC cast, nullability picks the
    policy shape — so this minimal mirror renders exactly what the real schema
    would, while staying pinned to this revision (same ``_scoped_metadata`` shape
    0006/0007/0008/0009 use).

    Args:
        as_uuid: ``True`` renders the post-ALTER world (``uuid`` column → the GUC
            is cast ``::uuid``); ``False`` renders the pre-ALTER / post-downgrade
            world (``varchar`` → text-to-text comparison).
    """
    meta = MetaData()
    coltype = (lambda: PgUUID(as_uuid=False)) if as_uuid else (lambda: String(255))
    for name in TENANT_TABLES_STRICT:
        Table(name, meta, Column("tenant_id", coltype(), nullable=False))
    for name in TENANT_TABLES_NULLABLE:
        Table(name, meta, Column("tenant_id", coltype(), nullable=True))
    return meta


def _guard_sql(table: str) -> str:
    """FAIL LOUD if any non-NULL ``tenant_id`` in ``table`` is still not a uuid."""
    return _GUARD_TEMPLATE.format(table=table, uuid_re=_UUID_RE)


def upgrade() -> None:
    # 1. The resolver's varchar RETURNS TABLE would survive the ALTER and then
    #    fail on every call. Drop it before anything else touches the column.
    op.execute(_DROP_CLAIM)

    # 2. Policies name the column → they block the ALTER outright. Drops all five
    #    policy names per table, so both shapes are covered exhaustively.
    for stmt in downgrade_statements(_scoped_metadata(as_uuid=False)):
        op.execute(stmt)

    # 3. Name → uuid backfill, while the column is still text. Values are BOUND;
    #    only the table IDENTIFIER is interpolated (it cannot be a bind param) and
    #    it comes from TENANT_TABLES, a module-level literal tuple. bandit flags
    #    the f-string shape regardless, so `nosec` is needed alongside `noqa`
    #    (`noqa: S608` silences ruff ONLY — see the same note in the sibling
    #    video_production_service migration, where that gap kept CI red).
    for table in TENANT_TABLES:
        for legacy_name, tenant_uuid in LEGACY_TENANT_MAP.items():
            op.execute(  # nosec B608
                sa.text(
                    f"UPDATE {table} SET tenant_id = :new_owner "  # noqa: S608
                    "WHERE tenant_id = :legacy_name"
                ).bindparams(new_owner=tenant_uuid, legacy_name=legacy_name),
            )

    # 4. Fail loud on anything still unparseable — never silently skipped/dropped.
    for table in TENANT_TABLES:
        op.execute(_guard_sql(table))

    # 5. The type flip. Preserves nullability (audit_logs stays NULLABLE) and
    #    rebuilds ix_*_tenant_id, uq_usage_tenant_date, uq_brand_tenant_domain.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid")

    # 6. Re-create both policy shapes with the GUC cast ``::uuid``. Without the
    #    cast the pod reads 0 rows — silently, not as an error.
    for stmt in upgrade_statements(_scoped_metadata(as_uuid=True)):
        op.execute(stmt)

    # 7. Re-create the resolver with ``tenant_id uuid``. The DROP in step 1 took
    #    the ACL with it, so 0007's REVOKE and 0011's GRANT are both re-applied —
    #    without the GRANT the scheduler gets `permission denied for function` on
    #    every tick, which is the exact 9.4-day outage 0011 exists to prevent.
    op.execute(_CLAIM_TENANT_UUID)
    op.execute(_REVOKE_CLAIM)
    op.execute(guarded_grant_statement(("claim_due_critique_schedules(timestamptz)",)))


def downgrade() -> None:
    op.execute(_DROP_CLAIM)

    for stmt in downgrade_statements(_scoped_metadata(as_uuid=True)):
        op.execute(stmt)

    for table in TENANT_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN tenant_id TYPE character varying(255) "
            f"USING tenant_id::text"
        )

    # Text-comparison policies, in each table's own shape (strict for the 19,
    # global-or-own for audit_logs). NOTE: the name→uuid backfill is deliberately
    # NOT reversed — see the module docstring.
    for stmt in upgrade_statements(_scoped_metadata(as_uuid=False)):
        op.execute(stmt)

    op.execute(_CLAIM_TENANT_VARCHAR)
    op.execute(_REVOKE_CLAIM)
    op.execute(guarded_grant_statement(("claim_due_critique_schedules(timestamptz)",)))
