"""Which upstream statuses ``/api/brand/icp-from-url`` re-emits vs. launders into 502.

Background — the production defect these tests lock down::

    ERROR w2v_common.clients.base: WM worker 'icp-analysis' returned 402:
      {"error":{"code":"ERROR","message":{"code":"byo_llm_not_configured",
       "message":"Configure your LLM provider in Settings before running agents."},
       "status":402}}
    INFO: "POST /api/brand/icp-from-url HTTP/1.1" 502 Bad Gateway

worker_management named the exact tenant-fixable condition and the route answered
"Bad Gateway". Half of this file proves that specific case is fixed; the other
half proves the fix did NOT become "pass every upstream status through", which
would be a worse bug (an upstream 404 masquerading as ours).

Route style follows ``test_icp_routes.py``: the coroutine is awaited directly
with a ``MagicMock`` request; no TestClient, no HTTP traffic. The one exception is
``TestErrorEnvelope``, which needs a real app because the thing under test is how
the response is SERIALIZED.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from w2v_common.asgi.error_envelope import register_error_envelope_handlers
from w2v_common.clients.webscraper import ScrapeResponse

from src.domains.icp.routes import extract_icp_from_url
from src.domains.icp.schemas import ICPFromUrlRequest
from src.shared.upstream_errors import (
    UpstreamStatusError,
    register_upstream_error_handler,
    upstream_http_exception,
)
from tests.llm_test_doubles import FakeWMClient

# The real worker_management body, double-wrapped exactly as production logged it:
# the OUTER code is the useless generic "ERROR" and the actionable pair is nested
# one level deeper inside "message".
_BYO_LLM_BODY: dict[str, Any] = {
    "error": {
        "code": "ERROR",
        "message": {
            "code": "byo_llm_not_configured",
            "message": "Configure your LLM provider in Settings before running agents.",
        },
        "status": 402,
    }
}


def _wm_status_error(
    status: int, body: Any = None, *, text: str | None = None
) -> httpx.HTTPStatusError:
    """Build the ``httpx.HTTPStatusError`` ``w2v_common`` raises on a non-2xx.

    Mirrors ``w2v_common.clients.base.raise_for_status_with_body``, which attaches
    the live response — that attachment is the only reason the route can recover
    the upstream's code at all.
    """
    request = httpx.Request("POST", "http://wm.test.invalid:8887/workers/icp-analysis/message")
    if text is not None:
        response = httpx.Response(status_code=status, text=text, request=request)
    else:
        response = httpx.Response(status_code=status, json=body or {}, request=request)
    return httpx.HTTPStatusError(
        f"WM worker 'icp-analysis' returned {status}", request=request, response=response
    )


def _make_request(*, wm_client: object) -> MagicMock:
    """Mock Request exposing app.state.wm_client; cache disabled so the LLM path runs."""
    request = MagicMock()
    request.state.tenant_id = "df5ce213-933c-5747-9b72-840f50559ec6"
    request.state.license_token = "lic-token-abc"
    request.app.state.wm_client = wm_client
    request.app.state.icp_cache = None
    return request


def _patch_scraper():
    """Patch WebscraperClient so the scrape succeeds and the WM call is what fails."""
    fake_ws = MagicMock()
    fake_ws.scrape_url = AsyncMock(
        return_value=ScrapeResponse(
            url="https://merqi.nl", title="Merqi", text="Merqi homepage copy", error=None, links=[]
        )
    )
    fake_ws.scrape_bulk = AsyncMock(return_value=[])
    fake_ws.close = AsyncMock()
    return patch("src.domains.icp.routes.WebscraperClient", return_value=fake_ws)


async def _call_route_with_wm_error(error: BaseException) -> HTTPException:
    """Drive the route to its WM failure branch and return the raised HTTPException."""
    request = _make_request(wm_client=FakeWMClient(raise_after="always", error=error))
    body = ICPFromUrlRequest(url="https://merqi.nl")
    with _patch_scraper(), pytest.raises(HTTPException) as exc:
        await extract_icp_from_url(body, request)
    return exc.value


# ---------------------------------------------------------------------------
# The reported defect: a caller-fixable 402 must reach the caller
# ---------------------------------------------------------------------------


class TestClientFixableStatusPassesThrough:
    async def test_upstream_402_byo_llm_surfaces_as_402(self):
        """REVERTED ⇒ the exact production defect returns.

        The tenant sees a bare ``502 Bad Gateway`` for a problem they could have
        fixed in Settings in ten seconds; testers file infrastructure bugs against
        a healthy cluster. This assertion IS the bug report.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert exc.status_code == 402

    async def test_upstream_402_keeps_the_actionable_message(self):
        """REVERTED ⇒ status is right but the payload is useless.

        A 402 whose body says "Failed to analyze URL" tells the user nothing to do.
        The whole point of passing the status through is carrying the upstream's
        sentence about Settings with it, unwrapped from WM's double envelope.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert "Configure your LLM provider in Settings" in str(exc.detail)

    async def test_upstream_402_keeps_the_machine_readable_code(self):
        """REVERTED ⇒ the frontend can only string-match English prose.

        ``byo_llm_not_configured`` is the branch key that lets the SPA render a
        real "Open Settings" call-to-action. It is carried on the exception (not
        in ``detail``) because the shared envelope reprs a non-str ``detail``.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert isinstance(exc, UpstreamStatusError)
        assert exc.upstream_code == "byo_llm_not_configured"
        assert exc.upstream == "worker_management"

    async def test_detail_stays_a_string(self):
        """REVERTED (detail made a dict) ⇒ the fallback path emits a Python repr.

        If the narrow handler is ever not registered, the shared ``w2v_common``
        handler does ``str(exc.detail)``. With a string detail that degrades to
        "correct status + correct sentence, no code". With a dict it degrades to
        ``"{'code': 'byo_llm_not_configured', ...}"`` shown to a human.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert isinstance(exc.detail, str)

    async def test_402_with_unparseable_body_still_passes_through(self):
        """REVERTED ⇒ a parse failure silently downgrades a 402 back to a 502.

        Degrading the CODE is fine; degrading the STATUS re-introduces the defect
        for any upstream that answers 402 with non-JSON. The status carries the
        meaning even when the body does not.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(402, text="<html>nope</html>"))
        assert exc.status_code == 402
        assert isinstance(exc, UpstreamStatusError)
        assert exc.upstream_code == "upstream_error"


# ---------------------------------------------------------------------------
# The containment half: everything else must still be a 502
# ---------------------------------------------------------------------------


class TestUpstreamFaultsStayBadGateway:
    async def test_upstream_500_is_still_502(self):
        """REVERTED ⇒ real upstream outages stop paging.

        A worker_management 500 is OUR dependency being broken. Re-emitting it as
        a 5xx-that-looks-like-a-client-error (or as the upstream's own 500) would
        make an outage read as an application bug and mis-route the alert.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(500, {"error": "boom"}))
        assert exc.status_code == 502
        assert not isinstance(exc, UpstreamStatusError)

    async def test_upstream_503_is_still_502(self):
        """REVERTED ⇒ a WM rollout/restart surfaces as a caller-fixable error.

        503 is the classic "upstream is restarting" signal; the caller can do
        nothing about it and must see the retry-later semantics of 502.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(503, {"error": "unavailable"}))
        assert exc.status_code == 502

    async def test_connection_error_is_still_502(self):
        """REVERTED ⇒ a transport failure would crash the route with a raw httpx error.

        A ``ConnectError`` carries no ``.response``; the mapper must fall through
        to the original 502 rather than assuming a status is available.
        """
        exc = await _call_route_with_wm_error(httpx.ConnectError("connection refused"))
        assert exc.status_code == 502
        assert "Failed to analyze URL" in str(exc.detail)

    async def test_read_timeout_is_still_502(self):
        """REVERTED ⇒ a slow LLM would surface as something other than a gateway fault.

        Timeouts are the single most common WM failure (long generations) and must
        keep the retry-later semantics they have always had.
        """
        exc = await _call_route_with_wm_error(httpx.ReadTimeout("timed out"))
        assert exc.status_code == 502

    async def test_shape_error_is_still_502(self):
        """REVERTED ⇒ a malformed LLM payload could leak out as a client error.

        ``analyze_icp_with_llm`` raises ``ValueError``/``JSONDecodeError`` when WM
        returns an unusable shape. That is our-side/model-side, never the caller's.
        """
        exc = await _call_route_with_wm_error(ValueError("ICP-analysis JSON is not an object"))
        assert exc.status_code == 502

    # Each of these is a status a "just pass 4xx through" refactor would wrongly
    # forward. The reasoning per status lives in the src.shared.upstream_errors
    # module docstring; this pins the decision so widening the set is a conscious
    # act that turns a test red rather than a silent behaviour change.
    @pytest.mark.parametrize(
        ("status", "why"),
        [
            (400, "we built the request body; the caller cannot fix our body"),
            (401, "OUR S2S license was rejected — invisible to the caller"),
            (403, "OUR identity lacks the grant — invisible to the caller"),
            (404, "OUR worker name is wrong; re-emitting claims the CALLER's resource is missing"),
            (422, "we built the request body"),
            (429, "ambiguous tenant-quota vs shared gateway limit, and we have no Retry-After"),
        ],
    )
    async def test_non_passthrough_status_is_still_502(self, status: int, why: str):
        """REVERTED ⇒ upstream statuses start masquerading as this service's own.

        The worst case is 404: the caller reads "not found" about the URL they
        submitted when in fact our worker name is wrong. 401/403 are equally
        misleading — they blame the caller for a credential only we control.
        """
        exc = await _call_route_with_wm_error(_wm_status_error(status, {"error": "nope"}))
        assert exc.status_code == 502, why
        assert not isinstance(exc, UpstreamStatusError), why


# ---------------------------------------------------------------------------
# The mapper in isolation — envelope unwrapping
# ---------------------------------------------------------------------------


class TestUpstreamHttpException:
    def test_unwraps_wm_double_envelope(self):
        """REVERTED ⇒ the caller gets the generic outer ``"ERROR"`` code.

        WM nests the useful pair inside ``error.message``. Reading only the outer
        level yields ``code="ERROR"``, which is exactly as useless as ``HTTP_402``.
        """
        exc = upstream_http_exception(
            _wm_status_error(402, _BYO_LLM_BODY),
            upstream="worker_management",
            fallback_detail="Failed to analyze URL: boom",
        )
        assert isinstance(exc, UpstreamStatusError)
        assert exc.upstream_code == "byo_llm_not_configured"

    def test_handles_single_wrapped_body(self):
        """REVERTED ⇒ an upstream that wraps only once loses its message.

        Not every peer double-wraps; descending unconditionally would walk past a
        string ``message`` and drop it.
        """
        exc = upstream_http_exception(
            _wm_status_error(402, {"error": {"code": "no_credit", "message": "Top up."}}),
            upstream="worker_management",
            fallback_detail="fallback",
        )
        assert isinstance(exc, UpstreamStatusError)
        assert exc.upstream_code == "no_credit"
        assert exc.detail == "Top up."

    def test_non_http_error_keeps_the_original_fallback_detail(self):
        """REVERTED ⇒ the 502 path stops being byte-identical to the old behaviour.

        Existing consumers (and the existing 502 tests) read that detail string;
        the fix must not perturb the path it was not supposed to touch.
        """
        exc = upstream_http_exception(
            RuntimeError("wm down"),
            upstream="worker_management",
            fallback_detail="Failed to analyze URL: wm down",
        )
        assert exc.status_code == 502
        assert exc.detail == "Failed to analyze URL: wm down"


# ---------------------------------------------------------------------------
# Serialization — the part unit-level route tests cannot see
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    """Proves the structure survives the WIRE, not just the exception object.

    Route tests assert on ``exc.value``, so they would stay green even if the
    envelope reduced the payload to a Python repr in a string field — which is
    precisely what the shared handler does to a dict ``detail``. Only a real
    request can catch that.
    """

    @staticmethod
    def _app() -> FastAPI:
        # Same order as src/asgi.py: shared envelope handlers first, then the
        # narrow subclass handler. That order is the thing under test.
        app = FastAPI()
        register_error_envelope_handlers(app, debug=False)
        register_upstream_error_handler(app)

        @app.get("/boom")
        async def boom() -> dict[str, str]:
            raise upstream_http_exception(
                _wm_status_error(402, _BYO_LLM_BODY),
                upstream="worker_management",
                fallback_detail="Failed to analyze URL",
            )

        @app.get("/plain")
        async def plain() -> dict[str, str]:
            raise HTTPException(status_code=502, detail="Failed to analyze URL")

        return app

    def test_envelope_carries_the_upstream_code_not_http_402(self):
        """REVERTED ⇒ the SPA loses its branch key even though the status is right.

        The shared handler hardcodes ``code="HTTP_<status>"``, which tells the
        frontend nothing the status line did not. Registering the subclass handler
        is what puts ``byo_llm_not_configured`` in ``error.code``. If Starlette's
        MRO-based handler lookup ever stopped preferring the subclass handler,
        this is the test that catches it.
        """
        with TestClient(self._app()) as client:
            response = client.get("/boom")
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "byo_llm_not_configured"

    def test_envelope_message_is_the_actionable_sentence_not_a_repr(self):
        """REVERTED (dict detail) ⇒ ``message`` becomes ``"{'code': ..., ...}"``.

        A Python repr rendered into a user-facing toast is the failure this whole
        design avoids; assert the human sentence is intact and un-repr'd.
        """
        with TestClient(self._app()) as client:
            body = client.get("/boom").json()
        assert body["error"]["message"].startswith("Configure your LLM provider")
        assert "{" not in body["error"]["message"]
        assert body["error"]["details"]["upstream"] == "worker_management"

    def test_plain_http_exceptions_are_untouched(self):
        """REVERTED ⇒ the narrow handler widened into an app-wide envelope change.

        Registering a handler for ``UpstreamStatusError`` must not change how any
        other ``HTTPException`` serializes; every other route in the service still
        depends on the shared ``HTTP_<status>`` envelope.
        """
        with TestClient(self._app()) as client:
            response = client.get("/plain")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "HTTP_502"
