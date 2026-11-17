"""Map an upstream client error onto the status this service should answer with.

The failure mode this prevents
------------------------------
Every synchronous route in this service wrapped its downstream call in a
``except Exception: raise HTTPException(502, ...)``. That is right for a
downstream that is genuinely broken, and WRONG for the one case where the
downstream told us exactly what the *tenant* has to do. Observed in production
on ``POST /api/brand/icp-from-url``::

    WM worker 'icp-analysis' returned 402: {"error": {"code": "ERROR",
      "message": {"code": "byo_llm_not_configured",
                  "message": "Configure your LLM provider in Settings
                              before running agents."}, "status": 402}}
    "POST /api/brand/icp-from-url HTTP/1.1" 502 Bad Gateway

The tenant had not configured an LLM provider. worker_management said so, in a
machine-readable ``{code, message}``, and we threw it away and answered
``502 Bad Gateway`` — which means "the upstream is broken, try later". A tester
reading that files an infrastructure bug; the user never learns there is a
Settings page to click. Self-service was impossible for a self-service problem.

What the upstream actually raises
---------------------------------
``w2v_common.clients.base.raise_for_status_with_body`` logs the body and then
raises a plain :class:`httpx.HTTPStatusError` with the **live, unbuffered-safe**
``response`` attached — no custom wrapper, and ``WMClient.message`` does not
retry or reclassify a 402 (``_RETRYABLE_STATUS_CODES`` is ``{429, 500, 502,
503, 504}``). So the full upstream JSON is recoverable here via
``exc.response.json()``; the truncation/redaction in that library applies only
to the log string. Nothing about this fix belongs in ``w2v_common``.

Why ONLY 402 is passed through
------------------------------
Passing *every* upstream status through would be a different bug: an upstream
``404`` (we named a worker that does not exist) would surface as OUR ``404``,
telling the caller their resource is missing when in fact our wiring is wrong.
The test for passthrough is not "is it 4xx" but "is the CALLER the party who
can fix it, and does the status still mean the same thing when WE say it".

Only ``402 Payment Required`` passes that test today:

* ``401`` / ``403`` — the S2S license/identity WE send was rejected. Our
  credential problem, invisible and unfixable to the caller. Stays 502.
* ``404`` — our worker name / endpoint. Ours, and re-emitting it would claim
  the caller's own resource is missing. Stays 502.
* ``400`` / ``422`` — we built a body the upstream rejected. Ours. Stays 502.
* ``429`` — ambiguous between a tenant quota and a shared gateway rate limit,
  and re-emitting it without an upstream ``Retry-After`` invites the caller to
  hammer us. Stays 502 until there is a case that needs it.
* ``5xx`` / timeouts / connection errors — genuinely the upstream. Stays 502,
  which is what keeps real outages paging.

Adding a status here is a deliberate act: justify it against those two
questions in this docstring before you widen the set.

Envelope — why a dedicated exception type rather than ``HTTPException``
-----------------------------------------------------------------------
No new error envelope is invented; this uses the fleet one. But a plain
``HTTPException(detail={...})`` **cannot** carry structure to the client here.
This service does not use FastAPI's default ``{"detail": ...}`` — the shared
factory installs ``w2v_common.asgi.error_envelope``, whose handler does::

    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)

so a dict ``detail`` arrives at the frontend as a **Python repr inside a string
field** (``"{'code': 'byo_llm_not_configured', ...}"``), and the handler never
populates the envelope's own ``details`` key. The frontend would have to parse a
repr to branch on the code — i.e. the actionable data would still be lost, just
one layer later. (The same latent bug already exists at
``brand/brands_routes.py`` ``detail={"validation_errors": [...]}``.)

:class:`UpstreamStatusError` therefore subclasses ``HTTPException`` and carries
the code/upstream as real attributes, and :func:`register_upstream_error_handler`
registers a handler for **that subclass only**. Starlette resolves handlers by
walking ``type(exc).__mro__``, so the subclass handler wins without touching the
generic ``HTTPException`` path — no app-wide envelope change.

The degradation is deliberate and safe: ``detail`` is set to the human-readable
upstream *message*, so if the handler is ever not registered (a unit test that
calls a route directly, a future app that forgets to wire it) the shared handler
still emits the correct **status** and the correct **actionable text**. Only the
machine-readable ``code`` is lost. It fails to "less structured", never to 502.

Only the extracted ``code`` and ``message`` scalars are echoed — never the raw
upstream body. Upstream services occasionally echo the bearer/license token they
rejected (that is why ``w2v_common.clients.base`` redacts before logging), and
this payload crosses a trust boundary to the caller, so the safe move is to
forward two known fields rather than an arbitrary blob.
"""

from __future__ import annotations

import json
import logging
import uuid

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from w2v_common.asgi.error_envelope import make_error_payload

logger = logging.getLogger(__name__)

#: Anything ``response.json()`` can hand back.
#:
#: Spelled out rather than ``object`` because this module's whole job is to walk
#: INTO the decoded body: under ``object`` an ``isinstance(x, dict)`` narrows to
#: ``dict[Unknown, Unknown]``, whose key type is ``Never``, so every ``.get("error")``
#: below is a type error. Spelled out rather than ``Any`` because the repo bans
#: bare ``Any`` (ruff ANN401) — and because the point here is precisely that the
#: shape is UNTRUSTED: every field is probed with ``isinstance`` before use, since
#: this runs on an error path where a malformed body must degrade, never raise.
type JsonValue = dict[str, "JsonValue"] | list["JsonValue"] | str | int | float | bool | None

# Upstream statuses this service re-emits verbatim. See the module docstring for
# the two questions a candidate status must answer before being added here.
PASSTHROUGH_STATUSES: frozenset[int] = frozenset({402})

# The caller-facing message is bounded: it lands in a JSON response and an
# upstream is free to return a wall of text.
_MAX_MESSAGE_CHARS = 500

# Fallback code when the upstream sent a passthrough status with no parseable
# ``code``. Still actionable — the status carries the meaning — but the frontend
# can tell "upstream said 402 without detail" from a real "byo_llm_not_configured".
_UNKNOWN_CODE = "upstream_error"


class UpstreamStatusError(HTTPException):
    """An upstream status this service deliberately re-emits to its own caller.

    Subclasses ``HTTPException`` so every existing ``except HTTPException`` and
    every route test that asserts on ``exc.value.status_code`` keeps working, and
    so an unregistered handler degrades gracefully (see the module docstring).
    """

    def __init__(self, *, status_code: int, code: str, message: str, upstream: str) -> None:
        # ``detail`` is the plain message, NOT a dict: the shared envelope handler
        # stringifies a non-str detail into a repr. Keeping it a string is what
        # makes the no-handler fallback readable instead of garbage.
        super().__init__(status_code=status_code, detail=message)
        self.upstream_code = code
        self.upstream = upstream


def _unwrap_error_object(body: JsonValue) -> JsonValue:
    """Peel the outer envelope(s) off an upstream error body.

    worker_management wraps twice: ``{"error": {"code": "ERROR", "message":
    {"code": "byo_llm_not_configured", "message": "..."}, "status": 402}}``.
    The OUTER ``code`` is the useless generic ``"ERROR"`` and the actionable pair
    is nested one level deeper inside ``message``. Peeling ``error`` and then a
    dict-valued ``message`` finds the innermost object that actually carries a
    string message; a single-wrapped body (``{"error": {"code": .., "message":
    "text"}}``) or a bare ``{"code", "message"}`` stops at the right level too.
    """
    current = body
    if isinstance(current, dict) and isinstance(current.get("error"), dict | str):
        current = current["error"]
    # Only descend when ``message`` is itself an object — a string ``message`` is
    # the payload we want, not another envelope.
    if isinstance(current, dict) and isinstance(current.get("message"), dict):
        current = current["message"]
    return current


def _extract_code_and_message(response: httpx.Response) -> tuple[str, str]:
    """Pull the actionable ``(code, message)`` out of an upstream error response.

    Falls back to the raw (truncated) body text when the upstream did not send
    JSON, and to ``("upstream_error", "")`` when it sent nothing usable —
    degrading is mandatory here: a parse failure must never turn a passthrough
    into a 500 and lose the status we were trying to preserve.
    """
    try:
        body = response.json()
    except json.JSONDecodeError, ValueError, UnicodeDecodeError:
        return _UNKNOWN_CODE, response.text[:_MAX_MESSAGE_CHARS]

    inner = _unwrap_error_object(body)
    if not isinstance(inner, dict):
        text = inner if isinstance(inner, str) else ""
        return _UNKNOWN_CODE, text[:_MAX_MESSAGE_CHARS]

    raw_code = inner.get("code")
    raw_message = inner.get("message") or inner.get("detail")
    code = raw_code if isinstance(raw_code, str) and raw_code else _UNKNOWN_CODE
    message = raw_message if isinstance(raw_message, str) else ""
    return code, message[:_MAX_MESSAGE_CHARS]


def upstream_http_exception(
    exc: Exception,
    *,
    upstream: str,
    fallback_detail: str,
) -> HTTPException:
    """Build the ``HTTPException`` a route should raise for a downstream failure.

    Returns (rather than raises) so the caller keeps its ``raise ... from exc``
    chain, which is what preserves the original traceback in logs.

    Args:
        exc: Whatever the client raised. Only ``httpx.HTTPStatusError`` carries
            an upstream status; everything else (transport errors, timeouts,
            shape/``ValueError`` failures) is by definition not a caller-fixable
            condition and maps to 502.
        upstream: Name of the downstream service, e.g. ``"worker_management"``.
            Echoed to the caller so a support ticket names the right hop.
        fallback_detail: The 502 detail string this route used before — kept
            verbatim so the non-passthrough path is byte-identical to the old
            behaviour and no existing consumer breaks.

    Returns:
        An :class:`UpstreamStatusError` carrying the upstream's ``{code,
        message}`` when the status is in :data:`PASSTHROUGH_STATUSES`, else the
        original 502.
    """
    response = getattr(exc, "response", None)
    if not isinstance(exc, httpx.HTTPStatusError) or response is None:
        return HTTPException(status_code=502, detail=fallback_detail)

    status = response.status_code
    if status not in PASSTHROUGH_STATUSES:
        return HTTPException(status_code=502, detail=fallback_detail)

    code, message = _extract_code_and_message(response)
    logger.warning(
        "Passing upstream %s %d through to the caller (code=%s) instead of "
        "laundering it into a 502 — this condition is the caller's to fix",
        upstream,
        status,
        code,
    )
    return UpstreamStatusError(status_code=status, code=code, message=message, upstream=upstream)


def passthrough_status_error(exc: Exception, *, upstream: str) -> UpstreamStatusError | None:
    """Return the mapped passthrough error for *exc*, or ``None`` if it is not one.

    For the routes that do NOT simply re-raise — a batch handler that must keep
    the other 49 URLs, a helper called twice inside one request — the question is
    not "what should I raise" but "is this the caller's problem or mine". This
    answers that by running the SAME :func:`upstream_http_exception` mapper and
    reporting whether it produced a passthrough, so :data:`PASSTHROUGH_STATUSES`
    stays the single place that decides. Deliberately not a second mapper: adding
    a status there changes every call site here at once.

    Args:
        exc: Whatever the downstream client raised.
        upstream: Name of the downstream service, echoed to the caller.

    Returns:
        The :class:`UpstreamStatusError` carrying the upstream ``{code, message}``
        when the status is caller-fixable, else ``None`` — meaning "this is ours,
        handle it however this route handles its own failures".
    """
    # ``fallback_detail`` is unused on the ``None`` branch: the caller is asking
    # a yes/no question, not for a 502 it may not want to raise.
    mapped = upstream_http_exception(exc, upstream=upstream, fallback_detail="")
    return mapped if isinstance(mapped, UpstreamStatusError) else None


def _request_id_from(request: Request) -> str:
    """Correlation id for the envelope, mirroring the shared handler's precedence.

    Reimplemented rather than imported because the library's version is private
    (``_request_id_from``); importing an underscore-prefixed symbol across a
    package boundary is the kind of coupling that breaks on a patch release.
    """
    rid = getattr(request.state, "request_id", None)
    if rid:
        return str(rid)
    return request.headers.get("x-request-id") or str(uuid.uuid7())


async def _upstream_status_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Emit the fleet error envelope with the upstream's code/message preserved.

    The whole point of this handler is the ``code`` field: the shared handler
    hardcodes ``HTTP_<status>``, which tells the frontend nothing it did not
    already know from the status line. Here it carries ``byo_llm_not_configured``,
    which is what a "Configure your LLM provider" call-to-action branches on.
    """
    if not isinstance(exc, UpstreamStatusError):  # pragma: no cover - defensive
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content=make_error_payload(
            code=exc.upstream_code,
            message=str(exc.detail),
            status=exc.status_code,
            request_id=_request_id_from(request),
            # ``http_code`` keeps the shape the generic handler would have
            # produced, so a client that already keys off ``HTTP_402`` still finds it.
            details={"upstream": exc.upstream, "http_code": f"HTTP_{exc.status_code}"},
        ),
        headers=dict(exc.headers or {}),
    )


def register_upstream_error_handler(app: FastAPI) -> None:
    """Register the :class:`UpstreamStatusError` handler on *app*.

    Registered for the SUBCLASS only. Starlette resolves handlers by walking
    ``type(exc).__mro__`` and taking the first match, so this wins for
    ``UpstreamStatusError`` while every other ``HTTPException`` continues to hit
    the shared ``w2v_common`` handler untouched. Must be called AFTER
    ``create_app`` — the factory's own registration would otherwise not exist yet.
    """
    app.add_exception_handler(UpstreamStatusError, _upstream_status_error_handler)
