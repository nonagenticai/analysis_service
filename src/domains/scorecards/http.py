"""Shared HTTP-layer helpers for the synchronous scorecard routes.

The ``cro`` / ``seo_audit`` / ``marketing`` / ``artifact_score`` routes all read
the tenant the same way (``AuthMiddleware`` sets ``request.state.tenant_id``) and
declare the same ``422`` / ``502`` OpenAPI responses. Both were copied verbatim
into every route module; they live here once instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from src.shared.tenant import require_request_tenant


def tenant_id_from_request(request: Request) -> str:
    """Get the tenant ID that ``AuthMiddleware`` put on ``request.state``, or HTTP 401.

    These routes are all license-authed, so a missing/empty tenant is never a
    valid state — it means the auth boundary did not run. Fabricating a
    ``"default"`` tenant (the prior fallback) masked that and logged a bogus
    tenant as if legitimate; reject with 401 instead ("empty tenant is always a
    bug" — the three-piece auth invariant).

    Presence is no longer sufficient. Migration 0013 made every ``tenant_id``
    column a native ``uuid``, so a PRESENT but non-uuid value (the legacy slugs
    ``n8n-dev`` / ``anonymous``, which real callers were still sending as recently
    as May) stops degrading to "0 rows / 404" and instead makes Postgres raise
    ``InvalidTextRepresentation: invalid input syntax for type uuid`` — a 500 on
    malformed input. ``require_request_tenant`` rejects both conditions with the
    same 401 this function already used, so the contract of these routes is
    unchanged and only the 500 disappears. It returns ``str``, never
    ``uuid.UUID``: ``set_config('app.current_tenant', $2, true)`` is
    ``(text, text, bool)`` and a uuid there raises ``UndefinedFunction``.
    """
    return str(require_request_tenant(request))


def scorecard_responses(
    detail_502: str = "Failed to fetch or scan the page",
) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses=`` block shared by the scorecard routes.

    ``detail_502`` overrides only the 502 description (artifact_score scores an
    "artifact" rather than a "page", and its delta route scores "either artifact").
    """
    return {
        422: {"description": "Validation error"},
        502: {"description": detail_502},
    }
