"""Pin the RLS DDL migration ``0008`` actually renders.

Nothing else asserts on it. ``tests/test_rls_isolation.py`` proves the policy at
the DB level, but it builds the schema from live ``Base.metadata`` via
``register_rls_ddl``/``create_all`` — never from
``upgrade_statements(_scoped_metadata())`` — and it skips entirely without
``CREATEROLE``. So the migration's own ``_scoped_metadata()`` (whose hardcoded
nullability SELECTS the policy shape: NOT NULL → strict ``tenant_isolation``;
NULLABLE → the looser global-or-own set where ``tenant_id IS NULL`` is readable
by every tenant) could be flipped either way with every test still green.

0008 is history and its assertions do NOT move now that the contract revision
has landed: ``_scoped_metadata()`` here stays ``nullable=True`` because it
describes the schema *0008* leaves. The strict end state is asserted by
``tests/unit/shared/test_migration_0009_not_null.py``.

These tests fail on these mutations:
  * ``_scoped_metadata()`` ``nullable=True`` → ``False`` (the expand/contract
    invariant: 0008 must ship the column nullable, matching the DB it leaves);
  * dropping ``ENABLE ROW LEVEL SECURITY`` (the table falls out of RLS);
  * pointing the DDL at a different table;
  * re-adding ``op.alter_column(... nullable=False)`` to ``upgrade()``.
"""

from __future__ import annotations

import importlib
import inspect

from w2v_common.database.rls import upgrade_statements

_MOD = importlib.import_module("src.shared.database.migrations.versions.0008_critique_tenant_rls")

# The module name must stay <= 32 chars as a revision id: ``alembic_version.
# version_num`` is varchar(32), and an over-long id fails the stamp AFTER the
# DDL has run. Pinned here so a rename back is caught by a test, not by a
# half-applied migration in CI.
assert len("0008_critique_tenant_rls") <= 32

_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant', true), '')"


def _statements() -> list[str]:
    return upgrade_statements(_MOD._scoped_metadata())


def test_migration_renders_rls_for_critique_findings_only() -> None:
    stmts = _statements()
    assert stmts, "0008 renders no RLS DDL at all — the table would not be enrolled"
    assert all("critique_findings" in s for s in stmts), stmts
    assert "ALTER TABLE critique_findings ENABLE ROW LEVEL SECURITY" in stmts


def test_migration_renders_the_nullable_global_or_own_policy_set() -> None:
    """0008 is EXPAND-only: nullable column ⇒ the global-or-own policies.

    This is the assertion that dies if someone "tightens" ``_scoped_metadata()``
    to ``nullable=False`` without also adding the ``SET NOT NULL`` — which is the
    exact combination that breaks old pods mid-rollout (see the migration
    docstring). The contract revision flips BOTH and updates this test.
    """
    stmts = _statements()
    joined = "\n".join(stmts)

    assert (
        f"CREATE POLICY tenant_isolation_read ON critique_findings FOR SELECT "
        f"USING (tenant_id IS NULL OR {_PREDICATE})" in stmts
    ), joined
    for verb, name in (("INSERT", "ins"), ("UPDATE", "upd"), ("DELETE", "del")):
        assert f"CREATE POLICY tenant_isolation_{name} ON critique_findings FOR {verb}" in joined

    # The STRICT single policy must NOT appear while the column is nullable: it
    # would be rendered only by a nullable=False mutation.
    assert (
        f"CREATE POLICY tenant_isolation ON critique_findings USING ({_PREDICATE})" not in joined
    ), "strict policy rendered — _scoped_metadata() was flipped to nullable=False"


def test_upgrade_does_not_set_not_null() -> None:
    """The expand/contract invariant, asserted on the migration source itself.

    ``upgrade()`` must not contract the column in the same deploy that adds it:
    the run-migrations initContainer completes before any new pod is Ready, so a
    NOT NULL here makes every old-replica finding INSERT fail and wedges the job
    in ``running``.
    """
    src = inspect.getsource(_MOD.upgrade)
    assert "nullable=False" not in src, src
    assert "SET NOT NULL" not in src.upper(), src
    # ...and the column it adds is explicitly nullable.
    assert "nullable=True" in src, src
