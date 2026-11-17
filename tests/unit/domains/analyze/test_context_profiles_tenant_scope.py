"""Tenant-scoping tests for the context-profile GET and DELETE handlers.

The DELETE endpoint used to resolve no ``tenant_id`` at all and called an
unscoped ``DELETE ... WHERE id = :id``. RLS backstopped it, so this was never a
live leak — these tests pin the defence-in-depth layer so it stays that way if
RLS is ever off (local dev, superuser role, unbound GUC).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.domains.analyze.context_profiles import delete_profile, get_profile
from src.domains.analyze.services.repositories.context_profile_repository import (
    UserContextProfileRepository,
)

TENANT_A = "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
TENANT_B = "4151df14-4770-5a91-ab9f-cbf2d14e68be"


def _request(session_factory, tenant_id: str) -> MagicMock:
    """Build a Request double carrying a tenant and a real session factory."""
    request = MagicMock()
    request.app.state.db_session_factory = session_factory
    request.state.tenant_id = tenant_id
    return request


@pytest.mark.asyncio
async def test_tenant_b_cannot_delete_tenant_a_profile(db_session_factory, db_session):
    """DELETE as tenant B 404s and leaves tenant A's profile in place."""
    profile = await UserContextProfileRepository.create(
        db_session, tenant_id=TENANT_A, name="A-owned", context_data={"a": 1}
    )
    await db_session.commit()
    pid = profile.id

    with pytest.raises(HTTPException) as exc_info:
        await delete_profile(pid, _request(db_session_factory, TENANT_B))
    assert exc_info.value.status_code == 404

    survivor = await UserContextProfileRepository.get(db_session, pid, tenant_id=TENANT_A)
    assert survivor is not None, "tenant B's DELETE removed tenant A's profile"
    assert survivor.tenant_id == TENANT_A

    # And the owner can still read it.
    owned = await get_profile(pid, _request(db_session_factory, TENANT_A))
    assert owned.tenant_id == TENANT_A


@pytest.mark.asyncio
async def test_owner_can_delete_own_profile(db_session_factory, db_session):
    """The owning tenant's DELETE still works (the fix is not over-tight)."""
    profile = await UserContextProfileRepository.create(
        db_session, tenant_id=TENANT_A, name="A-owned", context_data={"a": 1}
    )
    await db_session.commit()
    pid = profile.id

    await delete_profile(pid, _request(db_session_factory, TENANT_A))

    await db_session.rollback()  # start a fresh transaction to see the commit
    assert await UserContextProfileRepository.get(db_session, pid, tenant_id=TENANT_A) is None


@pytest.mark.asyncio
async def test_tenant_b_cannot_read_tenant_a_profile(db_session_factory, db_session):
    """GET /{profile_id} as tenant B 404s on tenant A's profile.

    Before the fix this route called ``UserContextProfileRepository.get(session,
    profile_id)`` — the repository's ``tenant_id`` defaulted to ``None`` and the
    predicate was applied only ``if tenant_id is not None``, so the SQL was a
    bare lookup by primary key. RLS was the ONLY thing closing it, and RLS is a
    ConfigMap key (``TENANT_RLS_MODE``). These sessions connect as the table
    owner, so RLS is inert here by design and this test sees the app-level
    filter alone: without it, tenant B gets a 200 and tenant A's context_data.
    """
    profile = await UserContextProfileRepository.create(
        db_session, tenant_id=TENANT_A, name="A-owned", context_data={"secret": "A"}
    )
    await db_session.commit()
    pid = profile.id

    with pytest.raises(HTTPException) as exc_info:
        await get_profile(pid, _request(db_session_factory, TENANT_B))
    assert exc_info.value.status_code == 404, (
        "cross-tenant GET must be indistinguishable from a missing profile; a "
        "403 would confirm the id exists to a tenant that must not learn it"
    )

    # And the owner still reads it — the filter is not simply over-tight.
    owned = await get_profile(pid, _request(db_session_factory, TENANT_A))
    assert owned.tenant_id == TENANT_A
    assert owned.context_data == {"secret": "A"}


@pytest.mark.asyncio
async def test_missing_profile_and_foreign_profile_are_the_same_404(db_session_factory, db_session):
    """The stranger's 404 detail matches a genuinely-absent profile's 404."""
    profile = await UserContextProfileRepository.create(
        db_session, tenant_id=TENANT_A, name="A-owned", context_data={"a": 1}
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as foreign:
        await get_profile(profile.id, _request(db_session_factory, TENANT_B))
    with pytest.raises(HTTPException) as absent:
        await get_profile(999_999_999, _request(db_session_factory, TENANT_B))

    assert (foreign.value.status_code, foreign.value.detail) == (
        absent.value.status_code,
        absent.value.detail,
    ), "the cross-tenant 404 differs from the genuine not-found 404 — an oracle"


@pytest.mark.asyncio
async def test_delete_without_db_returns_503(db_session_factory):
    """No session factory on app.state -> 503, not a crash."""
    request = MagicMock()
    request.app.state.db_session_factory = None
    request.state.tenant_id = TENANT_A

    with pytest.raises(HTTPException) as exc_info:
        await delete_profile(1, request)
    assert exc_info.value.status_code == 503
