"""The shared scorecard HTTP helpers behave as the four route copies did.

``tenant_id_from_request`` and ``scorecard_responses`` (in
``src.domains.scorecards.http``) replace the byte-identical ``_get_tenant_id``
and ``responses={422,502}`` blocks that every scorecard route hand-copied.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request

from src.domains.scorecards.http import scorecard_responses, tenant_id_from_request


def _fake_request(state: SimpleNamespace) -> Request:
    """A duck-typed stand-in exposing only the ``request.state`` the helper reads."""
    return cast(Request, SimpleNamespace(state=state))


def test_tenant_id_reads_request_state() -> None:
    request = _fake_request(SimpleNamespace(tenant_id="38394337-9aff-503a-b737-e711338283d4"))
    assert tenant_id_from_request(request) == "38394337-9aff-503a-b737-e711338283d4"


def test_tenant_id_rejects_missing() -> None:
    # A missing tenant means AuthMiddleware did not run — reject, don't fabricate.
    request = _fake_request(SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        tenant_id_from_request(request)
    assert exc.value.status_code == 401


def test_tenant_id_rejects_empty() -> None:
    # An empty-string tenant is present-but-invalid — same 401, never logged as a
    # legitimate tenant.
    request = _fake_request(SimpleNamespace(tenant_id=""))
    with pytest.raises(HTTPException) as exc:
        tenant_id_from_request(request)
    assert exc.value.status_code == 401


def test_responses_default_page_wording() -> None:
    responses = scorecard_responses()
    assert responses[422]["description"] == "Validation error"
    assert responses[502]["description"] == "Failed to fetch or scan the page"


def test_responses_custom_502_wording() -> None:
    responses = scorecard_responses("Failed to fetch or scan the artifact")
    assert responses[502]["description"] == "Failed to fetch or scan the artifact"
