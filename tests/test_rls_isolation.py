"""Proves the Phase D RLS policies ISOLATE tenants (not just that they exist).

Connects AS a throwaway non-owner ``NOBYPASSRLS`` role — the posture the runtime
``w2v_analysis_app`` role has under enforce — and asserts the shipped policies
(applied by ``register_rls_ddl`` on ``create_all``) actually scope reads/writes on
a representative tenant table. Needs a superuser test DB (``CREATE ROLE`` +
``SET ROLE``); skips loudly in CI where the DB user lacks ``CREATEROLE``.
"""

from __future__ import annotations

import warnings
from typing import NoReturn

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError


class RLSDbProofNotRun(UserWarning):
    """Emitted when the DB-level RLS proof could not run (missing ``CREATEROLE``).

    These two tests are the ONLY place the shipped policies are exercised against
    a real non-owner role. Without ``CREATEROLE`` they skip — and a silent skip
    means a policy regression ships green. The warning makes the loss visible in
    the pytest warnings summary next to the ``rls_db_proof`` marker; run the suite
    against a superuser DB (``pytest -m rls_db_proof -W error::UserWarning``) to
    turn "the proof did not run" into a hard failure.
    """


def _skip_loudly_without_createrole(exc: ProgrammingError) -> NoReturn:
    reason = (
        "DB-LEVEL RLS PROOF DID NOT RUN: this DB user cannot CREATE ROLE "
        f"(needs superuser/CREATEROLE), so tenant isolation was NOT verified: {exc.orig}"
    )
    warnings.warn(reason, RLSDbProofNotRun, stacklevel=2)
    pytest.skip(reason)


TENANT_A = "515f05fb-da56-5834-8196-fdffaa214220"
TENANT_B = "c2c749f3-d91c-5c12-b4d6-4bd5ac885389"
_ROLE = "w2v_analysis_app_rls_test"
_INS = "INSERT INTO review_jobs (job_id, tenant_id, status) VALUES (:job_id, :tenant, 'completed')"


@pytest.mark.rls_db_proof
@pytest.mark.asyncio
async def test_rls_isolates_tenants_under_restricted_role(db_engine) -> None:
    async with db_engine.begin() as conn:
        # Skip-gate FIRST (before any RLS-affected seed): a non-superuser CI/hook role
        # can't CREATE ROLE, and (not being the table owner) its seed insert would hit
        # the policy with no GUC → IntegrityError. Gate, then seed as the owner/superuser.
        try:
            await conn.execute(text(f'CREATE ROLE "{_ROLE}" NOSUPERUSER NOBYPASSRLS NOINHERIT'))
        except ProgrammingError as exc:  # CI user lacks CREATEROLE
            _skip_loudly_without_createrole(exc)
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON review_jobs TO "{_ROLE}"')
        )

        await conn.execute(text(_INS), {"job_id": "job-a", "tenant": TENANT_A})
        await conn.execute(text(_INS), {"job_id": "job-b", "tenant": TENANT_B})

        try:
            await conn.execute(text(f'SET LOCAL ROLE "{_ROLE}"'))

            # no GUC → fail-closed
            n = (await conn.execute(text("SELECT count(*) FROM review_jobs"))).scalar()
            assert n == 0, f"leaked {n} rows with no GUC"

            # GUC = tenant A → only A's row, never B's
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT_A}
            )
            # ``tenant_id::text``: migration 0013 made the column a native uuid, so
            # raw SQL (unlike the ORM, which is pinned to PgUUID(as_uuid=False))
            # hands psycopg back a ``uuid.UUID``. Casting here is the same thing
            # ``scheduler._CLAIM_SQL`` does at the one production raw-SQL site that
            # selects this column, and it keeps the assertion comparing like with
            # like. Do NOT "fix" this by comparing against ``uuid.UUID(TENANT_A)``:
            # the point of the cast is that the repo's tenant surface is ``str``.
            rows = (
                (await conn.execute(text("SELECT tenant_id::text FROM review_jobs")))
                .scalars()
                .all()
            )
            assert rows == [TENANT_A], f"not isolated under GUC=A: {rows}"

            # cross-tenant WITH CHECK: insert stamped B while GUC=A is rejected
            await conn.execute(text("SAVEPOINT s"))
            with pytest.raises(ProgrammingError):
                await conn.execute(text(_INS), {"job_id": "job-x", "tenant": TENANT_B})
            await conn.execute(text("ROLLBACK TO SAVEPOINT s"))
        finally:
            await conn.execute(text("RESET ROLE"))
            await conn.execute(text(f'DROP OWNED BY "{_ROLE}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))


_FINDING_INS = (
    "INSERT INTO critique_findings "
    "(job_id, tenant_id, finding_id, category, severity, title, description) "
    "VALUES (:job_id, :tenant, :finding_id, 'seo', 'high', 't', 'd')"
)


@pytest.mark.rls_db_proof
@pytest.mark.asyncio
async def test_rls_isolates_critique_findings(db_engine) -> None:
    """``critique_findings`` is enrolled — it was the one tenant table outside RLS.

    Enrolment is implicit: it follows from the table having a ``tenant_id``,
    which makes ``tenant_table_names`` select it. This builds the schema from
    ``Base.metadata`` via ``create_all``, where the column is NOT NULL, so what it
    proves is the STRICT policy — i.e. the TARGET (post-contract) state, not what
    migration 0008 alone leaves in prod (nullable column → global-or-own policies;
    see 0008's docstring and ``tests/unit/shared/test_migration_0008_rls_ddl.py``,
    which pins the SQL 0008 itself renders).
    """
    async with db_engine.begin() as conn:
        try:
            await conn.execute(text(f'CREATE ROLE "{_ROLE}" NOSUPERUSER NOBYPASSRLS NOINHERIT'))
        except ProgrammingError as exc:  # CI user lacks CREATEROLE
            _skip_loudly_without_createrole(exc)
        await conn.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON critique_findings TO "{_ROLE}"')
        )

        await conn.execute(
            text(_FINDING_INS), {"job_id": "job-a", "tenant": TENANT_A, "finding_id": "f-a"}
        )
        await conn.execute(
            text(_FINDING_INS), {"job_id": "job-b", "tenant": TENANT_B, "finding_id": "f-b"}
        )

        try:
            await conn.execute(text(f'SET LOCAL ROLE "{_ROLE}"'))

            # no GUC → fail-closed
            n = (await conn.execute(text("SELECT count(*) FROM critique_findings"))).scalar()
            assert n == 0, f"leaked {n} findings with no GUC"

            # GUC = tenant A → only A's finding, never B's
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :t, true)"), {"t": TENANT_A}
            )
            rows = (
                (await conn.execute(text("SELECT finding_id FROM critique_findings")))
                .scalars()
                .all()
            )
            assert rows == ["f-a"], f"critique_findings not isolated under GUC=A: {rows}"

            # cross-tenant WITH CHECK: a finding stamped B while GUC=A is rejected
            await conn.execute(text("SAVEPOINT s"))
            with pytest.raises(ProgrammingError):
                await conn.execute(
                    text(_FINDING_INS),
                    {"job_id": "job-b", "tenant": TENANT_B, "finding_id": "f-x"},
                )
            await conn.execute(text("ROLLBACK TO SAVEPOINT s"))
        finally:
            await conn.execute(text("RESET ROLE"))
            await conn.execute(text(f'DROP OWNED BY "{_ROLE}"'))
            await conn.execute(text(f'DROP ROLE IF EXISTS "{_ROLE}"'))


@pytest.mark.asyncio
async def test_tenant_guc_rebinds_after_commit(db_session_factory, monkeypatch) -> None:
    """The after_begin listener keeps ``app.current_tenant`` set across COMMITs.

    Regression for the critique ``job_completed`` audit INSERT: ``process_job``
    commits many times and ``bind_session_tenant`` is transaction-scoped, so the
    final audit write landed in an unbound transaction → RLS ``InsufficientPrivilege``.
    The listener re-binds from the identity ContextVar on every transaction.
    """
    from w2v_common.auth import clear_identity_context, set_identity_context

    from src.shared.database.rls_events import (
        install_rls_tenant_rebind,
        uninstall_rls_tenant_rebind,
    )

    monkeypatch.setenv("TENANT_RLS_MODE", "enforce")
    install_rls_tenant_rebind()
    set_identity_context(TENANT_A, None, None)
    try:
        async with db_session_factory() as session:
            # First transaction: a write that commits (ends the bound transaction).
            await session.execute(text("SELECT 1"))
            await session.commit()
            # Second, fresh transaction: the GUC must still be the tenant — proving
            # the re-bind fired on after_begin, not just on the initial bind.
            guc = (
                await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
            ).scalar()
            assert guc == TENANT_A, f"GUC not re-bound after commit: {guc!r}"

        # Control: with no tenant in context the listener is a no-op (background loops).
        clear_identity_context()
        async with db_session_factory() as session:
            guc = (
                await session.execute(text("SELECT current_setting('app.current_tenant', true)"))
            ).scalar()
            assert not guc, f"GUC leaked with empty identity context: {guc!r}"
    finally:
        # Process-global listener on the Session class — remove it so it does not
        # stay installed for every later test on this xdist worker.
        clear_identity_context()
        uninstall_rls_tenant_rebind()
