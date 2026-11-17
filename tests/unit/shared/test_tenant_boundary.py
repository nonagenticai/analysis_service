"""The w2v_common 0.67.0 tenant boundary: absence refuses, the nil UUID refuses.

Three properties are pinned here because each one shipped GREEN somewhere in the
fleet while being wrong:

1. ``MissingTenantError`` and ``InvalidTenantIdError`` subclass ``ValueError``.
   A ``try: uuid.UUID(v) except ValueError:`` around a tenant parse therefore
   SWALLOWS the refusal and hands back a fallback. Pinned so a future reader
   cannot re-derive "it's just a ValueError" and wrap it.
2. The nil UUID is rejected BY VALUE, in every spelling ``uuid.UUID`` accepts.
   It is a perfectly valid UUID and was the removed ``default-anon`` sentinel,
   so a ``None``-only guard leaves the mis-attribution hole wide open.
3. ``require_request_tenant`` answers absence with 401, not a substitution.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from w2v_common.tenant import InvalidTenantIdError, MissingTenantError, TenantError

from src.shared.tenant import parse_real_tenant_id, require_request_tenant, tenant_uuid

_REAL = "3f2a1c4e-8b7d-4c19-9f3a-6d5e2b1a0c77"

#: Every spelling ``uuid.UUID`` accepts for 00000000-0000-0000-0000-000000000000.
_NIL_SPELLINGS = [
    "00000000-0000-0000-0000-000000000000",
    "00000000000000000000000000000000",
    "{00000000-0000-0000-0000-000000000000}",
    "urn:uuid:00000000-0000-0000-0000-000000000000",
    "{00000000000000000000000000000000}",
]


class TestTenantErrorsAreValueErrors:
    def test_missing_and_invalid_both_subclass_value_error(self) -> None:
        """If this ever stops being true the guard below is dead weight; if it
        stays true, no tenant parse may sit inside `except ValueError`."""
        assert issubclass(MissingTenantError, ValueError)
        assert issubclass(InvalidTenantIdError, ValueError)
        assert issubclass(TenantError, ValueError)

    def test_a_value_error_handler_would_swallow_the_refusal(self) -> None:
        """Demonstrate the exact bug shape, so the reason for the rule is in the
        suite and not only in a commit message."""
        swallowed = False
        try:
            parse_real_tenant_id(None)
        except ValueError:
            swallowed = True
        assert swallowed, (
            "parse_real_tenant_id must raise something a bare `except ValueError` "
            "catches — that is precisely why no caller may use one"
        )


class TestNilUuidRejectedByValue:
    @pytest.mark.parametrize("spelling", _NIL_SPELLINGS)
    def test_every_nil_spelling_is_refused(self, spelling: str) -> None:
        assert uuid.UUID(spelling).int == 0, "fixture must actually be the nil UUID"
        with pytest.raises(InvalidTenantIdError):
            parse_real_tenant_id(spelling)

    @pytest.mark.parametrize("spelling", _NIL_SPELLINGS)
    def test_tenant_uuid_refuses_the_same_spellings(self, spelling: str) -> None:
        with pytest.raises(InvalidTenantIdError):
            tenant_uuid(spelling)

    def test_nil_as_a_uuid_object_is_refused_too(self) -> None:
        """Not only strings — an already-parsed nil UUID must not slip past."""
        with pytest.raises(InvalidTenantIdError):
            parse_real_tenant_id(uuid.UUID(int=0))

    def test_a_real_tenant_still_parses(self) -> None:
        assert parse_real_tenant_id(_REAL) == uuid.UUID(_REAL)
        assert tenant_uuid(uuid.UUID(_REAL)) == uuid.UUID(_REAL)


class TestAbsenceRefuses:
    @pytest.mark.parametrize("absent", [None, "", "   "])
    def test_absent_raises_missing_not_a_substitution(self, absent: object) -> None:
        with pytest.raises(MissingTenantError):
            parse_real_tenant_id(absent)

    @pytest.mark.parametrize("junk", ["anonymous", "default", "not-a-uuid"])
    def test_the_removed_string_sentinels_are_refused(self, junk: str) -> None:
        """`"anonymous"` and `"default"` were this repo's own fallbacks."""
        with pytest.raises(InvalidTenantIdError):
            parse_real_tenant_id(junk)


class TestRequireRequestTenant:
    def test_returns_the_bound_tenant(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=_REAL))
        assert require_request_tenant(cast(Request, request)) == uuid.UUID(_REAL)

    def test_absent_state_is_401_not_a_fallback(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(HTTPException) as exc:
            require_request_tenant(cast(Request, request))
        assert exc.value.status_code == 401

    def test_nil_uuid_on_the_request_is_401(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(tenant_id="00000000-0000-0000-0000-000000000000")
        )
        with pytest.raises(HTTPException) as exc:
            require_request_tenant(cast(Request, request))
        assert exc.value.status_code == 401
