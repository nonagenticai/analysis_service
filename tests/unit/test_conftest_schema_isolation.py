"""Proves ``tests/conftest.py`` provisions each worker schema race-free under xdist.

Before the per-process guard, the ``connect`` event listener re-ran
``CREATE SCHEMA`` + ``GRANT USAGE`` on *every* NullPool checkout. Two connections
opened concurrently against the same worker schema issued racing catalog writes
on the same ``pg_namespace`` tuple, which Postgres rejects with ``tuple
concurrently updated`` (empirically reproducible with ~20 genuinely-concurrent
raw connections). The fix runs the catalog DDL at most once per worker process —
recording the schema in ``conftest._PROVISIONED_SCHEMAS`` — while ``SET
search_path`` still runs on every connect.

SQLAlchemy's async engine serialises connect-event execution, so the *timing*
race cannot be reproduced through it; the end-to-end race regression is covered
by the async job-poll integration test running under ``--reruns 0``. This unit
test instead pins the fix's mechanism deterministically:

* a connect must record the schema in the guard set → FAILS if the CREATE/GRANT
  guard is removed (the un-gated listener never populates the set, so the
  catalog DDL re-runs on every connect and can race);
* concurrent connects must all resolve to the worker schema → FAILS if the
  per-connect ``SET search_path`` is ever gated behind the guard.

Only meaningful under xdist: the listener registers only when
``PYTEST_XDIST_WORKER`` is set (serial runs use the archetype's per-JOB
search_path), so this skips cleanly otherwise.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

from tests.conftest import _PROVISIONED_SCHEMAS

pytestmark = pytest.mark.skipif(
    not os.environ.get("PYTEST_XDIST_WORKER"),
    reason="per-worker schema listener only registers under pytest-xdist",
)


async def test_worker_schema_provisioned_once_and_search_path_every_connect(db_engine) -> None:
    """CREATE/GRANT run once (recorded in the guard set); search_path runs every connect."""
    schema = f"test_{os.environ['PYTEST_XDIST_WORKER']}"

    async def _open_and_check() -> str:
        async with db_engine.connect() as conn:
            return (await conn.execute(text("SELECT current_schema()"))).scalar()

    # Force the guard to re-provision on the next connect so the assertion is
    # independent of whichever earlier test in this worker first touched it.
    # (Within a worker, tests run serially, so mutating this module-level set is
    # safe; the engine re-adds the schema on the connect below.)
    _PROVISIONED_SCHEMAS.discard(schema)

    # First connect must run — and RECORD — the one-time CREATE SCHEMA + GRANT.
    assert await _open_and_check() == schema
    assert schema in _PROVISIONED_SCHEMAS, (
        "the connect listener did not record the worker schema: the per-process "
        "CREATE SCHEMA/GRANT guard is missing, so the catalog DDL re-runs on every "
        "connect and can race under concurrency ('tuple concurrently updated')"
    )

    # SET search_path is ungated: several concurrent connects must all resolve to
    # the worker schema (this is the scenario that reproduced the race pre-fix).
    schemas = await asyncio.gather(*(_open_and_check() for _ in range(8)))
    assert all(s == schema for s in schemas), (
        f"expected every concurrent connection in {schema!r}, got {schemas!r}"
    )
