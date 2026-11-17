"""Phase D RLS — re-bind the tenant GUC at the start of every transaction.

The scan scheduler (``critique/services/scheduler.py``) claims a job cross-tenant
off the database, then opens a ``get_db_session()`` and calls
``set_identity_context(tenant_id)`` for the run. (A Redis job worker used to do
the same; it was deleted as unreachable — see ``_RETIRED_ENV_PREFIXES`` in
``src/config/__init__.py``. The listener is still required for the scheduler.)
``bind_session_tenant`` only sets
``app.current_tenant`` transaction-scoped (``set_config(..., is_local=true)``,
PgBouncer-safe), but ``CritiqueService.process_job`` commits MANY times across
``lifecycle``/``phases``/``finalize`` — each commit ends the transaction the GUC
was bound in, so the final ``job_completed`` audit INSERT runs in a fresh, unbound
transaction and RLS fail-closes (``InsufficientPrivilege`` on the ``audit_logs``
WITH CHECK).

This installs an ``after_begin`` listener that re-applies ``app.current_tenant``
from the identity ContextVar (which the worker/scheduler set) at the start of
EVERY transaction. Inert unless ``TENANT_RLS_MODE=enforce`` and a tenant is in
context, so the pre-claim poll/background-retention sessions (no tenant in
context — the retention loop uses the SECURITY DEFINER ``prune_critique_data``
resolver that bypasses RLS as owner) stay no-ops. Scope is this process only.
"""

from __future__ import annotations

from sqlalchemy import Connection, event
from sqlalchemy.orm import Session, SessionTransaction
from w2v_common.auth.identity_context import get_tenant_id_from_context
from w2v_common.database.rls import tenant_rls_mode

_installed = False


def _rebind_tenant(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    if tenant_rls_mode() != "enforce":
        return
    tenant = get_tenant_id_from_context()
    if tenant is None:
        return
    # w2v_common 0.67.0 made this ContextVar a ``uuid.UUID`` (default None, was
    # ""). psycopg3 adapts a bare uuid.UUID to the postgres ``uuid`` type, and
    # ``set_config(text, text, bool)`` has no ``uuid`` overload — the re-bind
    # would die with "function set_config(unknown, uuid, boolean) does not
    # exist" on EVERY transaction under enforce. It must be sent as text.
    # A `if not tenant` guard would also be wrong now: uuid.UUID defines no
    # __bool__, so even the nil UUID is truthy.
    connection.exec_driver_sql("SELECT set_config('app.current_tenant', %s, true)", (str(tenant),))


def install_rls_tenant_rebind() -> None:
    """Register the after_begin GUC re-bind listener once per process."""
    global _installed
    if _installed:
        return
    event.listen(Session, "after_begin", _rebind_tenant)
    _installed = True


def uninstall_rls_tenant_rebind() -> None:
    """Remove the after_begin listener (paired with install).

    The listener is process-global on the ``Session`` class, so a test that
    installs it must remove it in teardown — otherwise it stays registered for
    every later test on the same xdist worker, an order-sensitive cross-test
    coupling (it re-binds the tenant GUC on each transaction under enforce mode).
    """
    global _installed
    if not _installed:
        return
    event.remove(Session, "after_begin", _rebind_tenant)
    _installed = False
