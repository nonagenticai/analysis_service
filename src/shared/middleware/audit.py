"""Audit logging middleware for state-changing requests and auth failures."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("analysis_service.audit")

_MUTATION_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})
_AUTH_FAILURE_CODES = frozenset({401, 403})


def _resource_from_path(path: str) -> str | None:
    """Extract a logical resource name from a URL path."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "api":
        resource = parts[1]
        if resource.endswith("s"):
            resource = resource[:-1]
        return resource
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    """Log state-changing requests and auth failures for security auditing."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log mutations and auth failures with timing and tenant info.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or handler.

        Returns:
            The response from downstream handlers.
        """
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000

        should_log = (
            request.method in _MUTATION_METHODS or response.status_code in _AUTH_FAILURE_CODES
        )

        if not should_log:
            return response

        tenant_id = getattr(request.state, "tenant_id", "anonymous")
        resource = _resource_from_path(request.url.path)

        log_data = {
            "tenant_id": tenant_id,
            "method": request.method,
            "path": request.url.path,
            "resource": resource,
            "status_code": response.status_code,
            "ip": request.client.host if request.client else "unknown",
            "duration_ms": round(duration_ms, 1),
        }

        if response.status_code in _AUTH_FAILURE_CODES:
            logger.warning("AUTH_FAILURE: %s", log_data)
        else:
            logger.info("AUDIT: %s", log_data)

        return response
