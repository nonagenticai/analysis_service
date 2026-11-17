"""Pin the contract migration ``0009``: strict RLS, and the drop-before-render order.

``tests/test_rls_isolation.py`` proves policies at the DB level but builds the
schema from live ``Base.metadata`` via ``register_rls_ddl``/``create_all`` and
skips entirely without ``CREATEROLE`` — it never exercises this revision's
rendered DDL. Nothing else asserts on it.

These tests fail on these mutations:
  * ``_scoped_metadata()`` ``nullable=False`` → ``True`` — the whole revision
    silently degrades to re-rendering the loose global-or-own set, leaving the
    ``tenant_id IS NULL OR ...`` cross-tenant read escape in place;
  * deleting the ``downgrade_statements`` loop from ``upgrade()`` (or moving it
    after ``upgrade_statements``) — a bare re-render only drops
    ``tenant_isolation``, so ``tenant_isolation_read`` survives and Postgres ORs
    permissive policies, keeping the exact leak this revision removes;
  * dropping ``SET NOT NULL`` / ``nullable=False`` from ``upgrade()`` — the
    column stays nullable while the strict policy claims it is not;
  * deleting the count/log before the destructive delete, or deleting rows
    before the re-backfill (a recoverable row would be destroyed);
  * renaming the revision to something longer than ``varchar(32)``;
  * pointing the DDL at a different table.
"""

from __future__ import annotations

import importlib
import inspect

from w2v_common.database.rls import upgrade_statements

_MOD = importlib.import_module(
    "src.shared.database.migrations.versions.0009_critique_tenant_not_null"
)

# ``alembic_version.version_num`` is varchar(32); an over-long id applies the DDL
# and THEN fails the stamp, leaving a half-applied migration. Pinned here so a
# rename is caught by a test rather than by StringDataRightTruncation in CI.
assert len("0009_critique_tenant_not_null") <= 32

_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')"


def _statements() -> list[str]:
    return upgrade_statements(_MOD._scoped_metadata())


def test_revision_id_matches_module_and_fits_version_num() -> None:
    assert _MOD.revision == "0009_critique_tenant_not_null"
    assert len(_MOD.revision) <= 32
    assert _MOD.down_revision == "0008_critique_tenant_rls"


def test_migration_renders_the_strict_policy_not_the_global_or_own_set() -> None:
    """NOT NULL column ⇒ the single strict ``tenant_isolation`` policy.

    Kills the ``nullable=False`` → ``True`` mutation in ``_scoped_metadata()``:
    that flip is invisible to every other test, and it turns the contract
    migration into a no-op that leaves ``tenant_id IS NULL`` globally readable.
    """
    stmts = _statements()
    assert stmts, "0009 renders no RLS DDL at all"
    assert all("critique_findings" in s for s in stmts), stmts
    assert "ALTER TABLE critique_findings ENABLE ROW LEVEL SECURITY" in stmts

    joined = "\n".join(stmts)
    assert (
        f"CREATE POLICY tenant_isolation ON critique_findings USING ({_PREDICATE}) "
        f"WITH CHECK ({_PREDICATE})" in stmts
    ), joined

    # None of the loose set may be (re)created, and no NULL read escape anywhere.
    for name in (
        "tenant_isolation_read",
        "tenant_isolation_ins",
        "tenant_isolation_upd",
        "tenant_isolation_del",
    ):
        assert f"CREATE POLICY {name} " not in joined, joined
    assert "IS NULL OR" not in joined, joined


def test_upgrade_drops_the_loose_policies_before_rendering_the_strict_one() -> None:
    """Order matters: ``downgrade_statements`` must run BEFORE ``upgrade_statements``.

    ``upgrade_statements`` on a non-nullable column emits only ``DROP POLICY IF
    EXISTS tenant_isolation`` — it never names the four global-or-own policies
    0008 created. Without the explicit drop pass, ``tenant_isolation_read``
    (``tenant_id IS NULL OR own``) survives beside the strict policy and, since
    permissive policies are OR-ed, the cross-tenant read stays open.
    """
    src = inspect.getsource(_MOD.upgrade)
    down_at = src.index("downgrade_statements")
    up_at = src.index("upgrade_statements")
    assert down_at < up_at, src

    # And the drop pass really names all five policies.
    from w2v_common.database.rls import downgrade_statements

    dropped = "\n".join(downgrade_statements(_MOD._scoped_metadata()))
    for name in (
        "tenant_isolation",
        "tenant_isolation_read",
        "tenant_isolation_ins",
        "tenant_isolation_upd",
        "tenant_isolation_del",
    ):
        assert f"DROP POLICY IF EXISTS {name} ON critique_findings" in dropped, dropped


def test_upgrade_contracts_the_column() -> None:
    src = inspect.getsource(_MOD.upgrade)
    assert "nullable=False" in src, src
    assert "alter_column" in src, src


def test_upgrade_backfills_then_logs_then_deletes() -> None:
    """The destructive step is auditable, and recoverable rows are recovered first.

    Kills: deleting the WARNING log (a silent delete in the initContainer), and
    reordering the delete ahead of the re-backfill (rows whose parent job still
    exists would be destroyed instead of tenanted).
    """
    src = inspect.getsource(_MOD.upgrade)
    backfill_at = src.index("_BACKFILL")
    count_at = src.index("_COUNT_NULLS")
    log_at = src.index("_log.warning")
    delete_at = src.index("_DROP_NULLS")
    assert backfill_at < count_at < log_at < delete_at, src

    assert "SET tenant_id = j.tenant_id" in _MOD._BACKFILL
    assert "f.tenant_id IS NULL" in _MOD._BACKFILL
    assert _MOD._DROP_NULLS == "DELETE FROM critique_findings WHERE tenant_id IS NULL"


def test_ddl_is_unguarded_so_a_non_owning_role_fails_loudly() -> None:
    """No ``IF EXISTS``/``IF NOT EXISTS`` of our own on the ALTERs.

    Per ``alembic-idempotent-sql-needs-table-ownership``: a guarded ALTER
    silently no-ops for a role that does not own the table, which here would
    "succeed" while leaving the column nullable and the leak open.
    """
    src = inspect.getsource(_MOD)
    for stmt in (_MOD._BACKFILL, _MOD._COUNT_NULLS, _MOD._DROP_NULLS):
        assert "IF EXISTS" not in stmt.upper()
    assert "add_column" not in src, src  # 0008 owns the column's creation
