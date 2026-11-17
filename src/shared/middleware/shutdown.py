"""Graceful-shutdown middleware.

Flips ``app.state.shutting_down = True`` is owned by the lifespan handler in
``asgi.py``. This middleware short-circuits *new* mutating requests once the
flag is set so they fail fast with a retriable 503 + ``Retry-After`` header
during the drain window, instead of getting cut at SIGKILL when k8s decides
the pod has used up ``terminationGracePeriodSeconds``.

GETs and HEADs are allowed through during drain — the frontend retries them
idempotently and the operational cost of a half-served read is bounded. Only
POST / PATCH / PUT / DELETE return 503 during drain.

The lifespan handler installs the flag, optionally waits a bounded window
for in-flight dispatch tasks to drain, then proceeds with the existing
``dispatcher.shutdown()`` + DB/Redis close path.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_DRAINABLE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ShutdownMiddleware(BaseHTTPMiddleware):
    """Refuse new mutating requests once the lifespan flips ``shutting_down``."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Return 503 + Retry-After during drain for write methods; pass otherwise."""
        if request.method in _DRAINABLE_METHODS and getattr(
            request.app.state, "shutting_down", False
        ):
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Service is draining for shutdown; retry shortly.",
                    "code": "shutting_down",
                },
                headers={"Retry-After": "5"},
            )
        return await call_next(request)
