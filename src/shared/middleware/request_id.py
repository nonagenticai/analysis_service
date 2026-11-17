"""Request ID middleware for request tracing.

Binds the inbound X-Request-Id (or a freshly generated uuid7) to both
``request.state.request_id`` and the ``w2v_common.observability.request_context``
ContextVar. The ContextVar binding is what lets the JSON log formatter and the
``trace_event_hook`` on every ``BaseServiceClient`` propagate the same id to
peer services on outbound httpx calls.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from w2v_common.observability.request_context import set_request_id


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Inject X-Request-Id header for request tracing.

    If the incoming request already has an X-Request-Id header, it is preserved.
    Otherwise, a new UUID is generated. The id is also bound to the
    ``w2v_common`` request_id ContextVar so outbound httpx S2S calls forward it.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Extract or generate a request ID and attach to request state.

        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or handler.

        Returns:
            Response with X-Request-Id header set.
        """
        request_id = request.headers.get("x-request-id") or str(uuid.uuid7())
        request.state.request_id = request_id
        set_request_id(request_id)

        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response


# Alias for tests that use the all-caps "ID" spelling.
RequestIDMiddleware = RequestIdMiddleware
