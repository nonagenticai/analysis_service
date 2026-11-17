"""Tenant identity at this service's boundary — no sentinels, ever.

``w2v_common`` 0.67.0 deleted every fallback tenant (``AuthSettings.default_tenant_id``,
``UNATTRIBUTED_TENANT_ID``, ``DEFAULT_TENANT_ID``) and moved the tenant to
``uuid.UUID``. The product ruling behind that change is absolute: **every row
carries a real registered tenant**. A missing tenant is now a refusal, not a
substitution.

This repo used to substitute two sentinels of its own — ``"anonymous"`` in the
analyze/context-profile handlers and ``"default"`` in the brand/ICP handlers.
Both are gone. They were worse than the nil UUID they replaced upstream:
``"anonymous"`` is not even a UUID, so it could only ever have reached
``bind_session_tenant`` as a value Postgres would reject or silently coerce.

Two guards live here, not one:

* absence / non-UUID — delegated to :func:`w2v_common.tenant.parse_tenant_id`.
* **the nil UUID by VALUE** — ``00000000-0000-0000-0000-000000000000`` is a
  perfectly valid UUID and is registered upstream as ``default-anon``, so a
  ``None``-only guard leaves the whole mis-attribution hole open. It is rejected
  on ``.int == 0``, which covers every spelling ``uuid.UUID`` accepts: dashed,
  undashed, ``{braced}`` and ``urn:uuid:``.

The repo continues to carry the tenant as ``str`` internally (it is a cache-key
and a log field in ~40 places); this module is the single seam where it becomes
a ``uuid.UUID`` for ``w2v_common``, and the single place absence fails.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException
from w2v_common.tenant import InvalidTenantIdError, MissingTenantError, parse_tenant_id

if TYPE_CHECKING:
    from fastapi import Request

__all__ = ["parse_real_tenant_id", "require_request_tenant", "tenant_uuid"]


def parse_real_tenant_id(value: object, *, source: str = "tenant_id") -> uuid.UUID:
    """Coerce ``value`` to a real tenant UUID, or raise.

    Raises:
        MissingTenantError: ``value`` is absent or empty.
        InvalidTenantIdError: ``value`` is not a UUID, or is the nil UUID.
    """
    tenant = parse_tenant_id(value, source=source)
    if tenant.int == 0:
        # Not a MissingTenantError: something DID arrive, it is just the
        # sentinel this whole change exists to remove. Do not echo the value —
        # by construction it is all zeros, but the message shape matches
        # parse_tenant_id's deliberate no-echo policy.
        raise InvalidTenantIdError(
            f"{source}: the nil UUID is the removed 'default-anon' sentinel, not a tenant"
        )
    return tenant


def tenant_uuid(value: object, *, source: str = "tenant_id") -> uuid.UUID:
    """Boundary converter: a repo-held ``str`` tenant into the ``uuid.UUID`` w2v_common wants.

    Deliberately identical to :func:`parse_real_tenant_id`; the separate name
    marks the call sites that are a *type* boundary rather than an *authz* one,
    so a future reader does not mistake one for the other.
    """
    return parse_real_tenant_id(value, source=source)


def require_request_tenant(
    request: Request, *, source: str = "request.state.tenant_id"
) -> uuid.UUID:
    """The tenant bound to *request* by ``AuthMiddleware``, or HTTP 401.

    401 rather than 400: ``request.state.tenant_id`` is set exclusively by
    ``w2v_common``'s ``AuthMiddleware`` from a verified license token or JWT, so
    its absence means the requestor was never identified — the same condition
    the middleware itself answers with 401. A 400 would tell the caller their
    *payload* was wrong, which it was not.
    """
    try:
        return parse_real_tenant_id(getattr(request.state, "tenant_id", None), source=source)
    except MissingTenantError as exc:
        raise HTTPException(status_code=401, detail="No tenant bound to this request") from exc
    except InvalidTenantIdError as exc:
        raise HTTPException(status_code=401, detail="Invalid tenant id") from exc
