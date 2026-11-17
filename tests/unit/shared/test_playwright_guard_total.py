"""The BrowserContext SSRF route handler must be TOTAL.

Playwright does not answer a route whose handler raised: it neither continues
nor aborts, so Chromium holds the request until the navigation times out
(~30 s, per affected request). That is fail-CLOSED, so it is not an SSRF bypass
-- which is exactly why it survives a security review of the guard's *decisions*.
It is still the wrong failure mode: it converts any bug on this path into a
stall instead of an immediate, logged refusal.

The same requirement applies to the ``route_web_socket`` handler the guard also
registers, and is sharper there: an unanswered WebSocket route is silent rather
than merely slow -- the page holds a socket that never opens and never closes,
with nothing logged. ``TestWebSocketHandlerIsTotal`` below covers it, and
carries the minimum decision cases needed as its false-positive control (a
handler that closed every socket unconditionally would satisfy every other
assertion in that class while breaking live-chat, analytics and HMR sockets).

The decision logic itself (which origins are judged public, how redirect hops
are re-judged, that ``ws``/``wss`` share the http verdict cache, that a real
Chromium is actually intercepted) is covered by
``tests/integration/test_playwright_ssrf_guard.py``. This file covers only
totality, which needs no browser -- and unlike that file it is NOT gated on a
Chromium build, so it runs in the bare-runner CI lint/unit job.

``src/shared/security/playwright_guard.py`` is byte-identical to
``video_production_service``'s copy apart from one docstring line. The
equivalent cases live there in
``tests/unit/test_playwright_guard_and_webhook_ssrf.py``, in
``TestHandlerIsTotal`` and ``TestWebSocketHandlerIsTotal``; keep the two in step.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any

import pytest

from src.shared.security.playwright_guard import install_ssrf_guard

pytestmark = pytest.mark.unit

# TEST-NET-3. Not real, not in any block-list, so it is judged public.
PUBLIC_IP = "203.0.113.10"
PUBLIC_URL = "https://cdn.example/app.css"


@pytest.fixture
def stub_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``cdn.example`` to a public IP; everything else fails closed.

    A real lookup under this repo's ``-n 4`` xdist is flaky and ``--reruns 2``
    would paper over it. IP literals are echoed back because that is what the
    real :func:`socket.getaddrinfo` does.
    """

    def _fake(host: str, *_a: Any, **_kw: Any) -> list[tuple[Any, ...]]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 0))]
        if host == "cdn.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 0))]
        raise socket.gaierror(f"stub resolver has no answer for {host}")

    monkeypatch.setattr("w2v_common.net.safe_fetch.socket.getaddrinfo", _fake)


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.headers: dict[str, str] = {}


class _FakeRoute:
    def __init__(self) -> None:
        self.continued = False
        self.aborted_with: str | None = None
        self.fulfilled: _FakeResponse | None = None

    async def continue_(self) -> None:
        self.continued = True

    async def abort(self, reason: str) -> None:
        self.aborted_with = reason

    async def fetch(self, *, url: str, max_redirects: int) -> _FakeResponse:
        assert max_redirects == 0, "the guard must never delegate redirect-following"
        return _FakeResponse()

    async def fulfill(self, *, response: _FakeResponse) -> None:
        self.fulfilled = response


class _RaisingFetchRoute(_FakeRoute):
    """``route.fetch`` blows up with something the guard does not anticipate."""

    async def fetch(self, *, url: str, max_redirects: int) -> _FakeResponse:
        raise RuntimeError("playwright transport exploded")


class _TargetClosedFetchRoute(_FakeRoute):
    """``route.fetch`` fails because the page/context/browser is already gone.

    Playwright's real message, verbatim from a live pod on 2026-08-10:
    ``Route.fetch: Target page, context or browser has been closed``.
    """

    async def fetch(self, *, url: str, max_redirects: int) -> _FakeResponse:
        raise RuntimeError("Route.fetch: Target page, context or browser has been closed")


class _UnabortableRoute(_RaisingFetchRoute):
    """...and the rescue ``abort`` fails too, as it does on an already-resolved route."""

    async def abort(self, reason: str) -> None:
        raise RuntimeError("route is already handled")


class _CancellingFetchRoute(_FakeRoute):
    """``route.fetch`` is cancelled -- teardown, not a failure."""

    async def fetch(self, *, url: str, max_redirects: int) -> _FakeResponse:
        raise asyncio.CancelledError


class _FakeRequest:
    def __init__(self, url: str, resource_type: str = "stylesheet") -> None:
        self.url = url
        self.resource_type = resource_type


class _FakeWebSocketRoute:
    """Stands in for ``playwright.async_api.WebSocketRoute``.

    ``connect_to_server`` is deliberately synchronous, matching the real API:
    the guard's refusal is "never call it", so a double that made it awaitable
    would let a missing ``await`` read as a refusal.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.closed_with: tuple[int | None, str | None] | None = None
        self.connected = False

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed_with = (code, reason)

    def connect_to_server(self) -> object:
        self.connected = True
        return object()


class _UnclosableWebSocketRoute(_FakeWebSocketRoute):
    """...and the rescue ``close`` fails too, as it does on a torn-down socket."""

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        raise RuntimeError("socket already gone")


class _ConnectRaisingWebSocketRoute(_FakeWebSocketRoute):
    """The target is judged public, then opting in to the server blows up."""

    def connect_to_server(self) -> object:
        raise RuntimeError("playwright transport exploded")


class _FakeContext:
    def __init__(self) -> None:
        self.handler: Any = None
        self.pattern: Any = None
        self.ws_handler: Any = None
        self.ws_pattern: Any = None
        self.init_scripts: list[str] = []
        #: Registration order, so a test can assert the init script is installed
        #: before the route handlers without monkeypatching the double's methods.
        self.calls: list[str] = []
        #: ``(permissions, origin)`` per ``grant_permissions`` call.
        self.grants: list[tuple[tuple[str, ...], Any]] = []

    async def route(self, pattern: Any, handler: Any) -> None:
        self.calls.append("route")
        self.pattern = pattern
        self.handler = handler

    # Same reasoning as ``route_web_socket`` below: the real ``BrowserContext``
    # has this, so the double must too. The guard installs the SharedWorker
    # mitigation through it, and a double without it fails as "the guard is
    # broken" rather than "the double is narrower than production".
    async def add_init_script(self, script: Any) -> None:
        self.calls.append("add_init_script")
        self.init_scripts.append(str(script))

    # The real ``BrowserContext`` has this, so the double must too. A double
    # NARROWER than the object it stands in for cannot fail the way production
    # fails: without this method every test here would have died with
    # `AttributeError` the moment the guard started registering a WebSocket
    # route, which reports as "the guard is broken" rather than "the double is".
    async def route_web_socket(self, pattern: Any, handler: Any) -> None:
        self.calls.append("route_web_socket")
        self.ws_pattern = pattern
        self.ws_handler = handler

    # The real ``BrowserContext`` has this too. The guard grants Chromium's
    # ``local-network-access`` permission to allow-listed origins through it; the
    # double records every grant so a widened scope is visible without a browser.
    async def grant_permissions(self, permissions: Any, origin: Any = None) -> None:
        self.calls.append("grant_permissions")
        self.grants.append((tuple(permissions), origin))


async def _handler_for() -> _FakeContext:
    context = _FakeContext()
    await install_ssrf_guard(context)  # ty: ignore[invalid-argument-type]  # why: _FakeContext duck-types BrowserContext
    return context


class TestHandlerIsTotal:
    """``pytest`` already fails a test on an escaping exception, so the real
    assertion in each case is "the call returned at all". The ``aborted_with``
    assertion is what separates "returned because the handler is total" from a
    hypothetical handler that swallowed the error and left the route unanswered
    anyway -- which would stall Chromium just the same.
    """

    @pytest.mark.asyncio
    async def test_raising_fetch_aborts_instead_of_stalling(self, stub_dns: None) -> None:
        """The origin is judged public, then the transport fails underneath us."""
        context = await _handler_for()
        route = _RaisingFetchRoute()

        await context.handler(route, _FakeRequest(PUBLIC_URL))

        assert route.aborted_with == "blockedbyclient"
        assert route.fulfilled is None
        assert route.continued is False

    @pytest.mark.asyncio
    async def test_unexpected_validator_error_aborts(
        self, stub_dns: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_origin_is_public`` handles SSRFError/ValueError/OSError -- and only those."""

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("resolver library regression")

        monkeypatch.setattr("src.shared.security.playwright_guard.validate_public_url", _boom)
        context = await _handler_for()
        route = _FakeRoute()

        await context.handler(route, _FakeRequest(PUBLIC_URL))

        assert route.aborted_with == "blockedbyclient"
        assert route.fulfilled is None

    @pytest.mark.asyncio
    async def test_target_closed_is_teardown_not_an_error(
        self, stub_dns: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A closed target is teardown noise, logged at DEBUG -- but still aborted.

        Measured live 2026-08-10: this was the ONLY error line in the whole w2v
        namespace across an hour of traffic, logged as
        ``SSRF guard: unexpected error handling <url> -- refusing``. It reads as
        the guard rejecting a legitimate asset; in fact the page that asked for
        it no longer existed. A false ERROR is worse than no ERROR, because it is
        the one signal an operator actually looks at.
        """
        context = await _handler_for()
        route = _TargetClosedFetchRoute()

        with caplog.at_level(logging.DEBUG, logger="src.shared.security.playwright_guard"):
            await context.handler(route, _FakeRequest(PUBLIC_URL))

        # Still TOTAL: the route is answered, so Chromium never stalls.
        assert route.aborted_with == "blockedbyclient"
        assert route.fulfilled is None
        levels = {r.levelno for r in caplog.records}
        assert logging.ERROR not in levels, "teardown must not be reported as an error"
        assert logging.DEBUG in levels

    @pytest.mark.asyncio
    async def test_a_real_fetch_failure_is_still_an_error(
        self, stub_dns: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The negative control for the demotion above.

        Without this, quietening the closed-target case could be widened until it
        swallowed genuine failures -- which is precisely the class of change this
        module's docstring exists to prevent.
        """
        context = await _handler_for()
        route = _RaisingFetchRoute()

        with caplog.at_level(logging.DEBUG, logger="src.shared.security.playwright_guard"):
            await context.handler(route, _FakeRequest(PUBLIC_URL))

        assert route.aborted_with == "blockedbyclient"
        assert logging.ERROR in {r.levelno for r in caplog.records}

    @pytest.mark.asyncio
    async def test_failing_rescue_abort_still_does_not_escape(self, stub_dns: None) -> None:
        """A route already resolved raises on a second ``abort``; that must not escape either."""
        context = await _handler_for()
        route = _UnabortableRoute()

        await context.handler(route, _FakeRequest(PUBLIC_URL))

        assert route.fulfilled is None

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self, stub_dns: None) -> None:
        """Cancellation is control flow. Absorbing it would break browser teardown.

        This is the false-positive control: it passes against the *unfixed*
        handler too. Its job is to fail if the catch-all is ever widened to
        ``BaseException``, not to prove the fix.
        """
        context = await _handler_for()
        route = _CancellingFetchRoute()

        with pytest.raises(asyncio.CancelledError):
            await context.handler(route, _FakeRequest(PUBLIC_URL))

        assert route.aborted_with is None


class TestWebSocketHandlerIsTotal:
    """The same totality requirement, on the ``route_web_socket`` path.

    Sharper here than on the HTTP side. Playwright dials the real server only
    when the handler calls ``connect_to_server()``, so a raising WebSocket
    handler *is* fail-closed -- but silently: the page is left holding a socket
    that never opens and never closes, with nothing logged. Every case below
    therefore asserts the socket was explicitly closed, not merely that the
    handler returned.

    Which targets are judged public, and how ``ws``/``wss`` map onto the http
    verdict cache, is covered against a real Chromium in
    ``tests/integration/test_playwright_ssrf_guard.py``.
    """

    @pytest.mark.asyncio
    async def test_the_guard_registers_a_websocket_route_at_all(self, stub_dns: None) -> None:
        """Non-vacuity: without this registration the whole class is decorative.

        ``context.route`` is never consulted for a ``ws://`` handshake, so a
        guard that installs only the HTTP handler leaves a full-duplex channel
        to any internal address wide open.
        """
        context = await _handler_for()

        assert context.ws_handler is not None, (
            "install_ssrf_guard did not register a WebSocket route -- ws:// and "
            "wss:// targets are not judged at all"
        )

    @pytest.mark.asyncio
    async def test_internal_target_is_closed_not_connected(self, stub_dns: None) -> None:
        """The decision itself, at the unit layer: loopback must be refused."""
        context = await _handler_for()
        ws = _FakeWebSocketRoute("ws://127.0.0.1:9999/internal-endpoint")

        await context.ws_handler(ws)

        assert ws.connected is False, "the guard dialled an internal WebSocket target"
        assert ws.closed_with == (1008, "blocked by client")

    @pytest.mark.asyncio
    async def test_public_target_is_connected(self, stub_dns: None) -> None:
        """False-positive control: the handler must not simply close everything.

        Without this, every other case in this class would also pass against a
        handler that refused unconditionally -- which would break live-chat,
        analytics and HMR sockets on every page the service touches.
        """
        context = await _handler_for()
        ws = _FakeWebSocketRoute("wss://cdn.example/socket")

        await context.ws_handler(ws)

        assert ws.connected is True, "an allowed wss:// target was refused"
        assert ws.closed_with is None

    @pytest.mark.asyncio
    async def test_unexpected_validator_error_closes(
        self, stub_dns: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_origin_is_public`` handles SSRFError/ValueError/OSError -- and only those."""

        def _boom(*_a: Any, **_kw: Any) -> None:
            raise RuntimeError("resolver library regression")

        monkeypatch.setattr("src.shared.security.playwright_guard.validate_public_url", _boom)
        context = await _handler_for()
        ws = _FakeWebSocketRoute("wss://cdn.example/socket")

        await context.ws_handler(ws)

        assert ws.connected is False
        assert ws.closed_with == (1008, "blocked by client")

    @pytest.mark.asyncio
    async def test_raising_connect_closes_instead_of_stalling(self, stub_dns: None) -> None:
        """The target is judged public, then the transport fails underneath us."""
        context = await _handler_for()
        ws = _ConnectRaisingWebSocketRoute("wss://cdn.example/socket")

        await context.ws_handler(ws)

        assert ws.closed_with == (1008, "blocked by client")

    @pytest.mark.asyncio
    async def test_failing_rescue_close_still_does_not_escape(self, stub_dns: None) -> None:
        """A socket already torn down raises on ``close``; that must not escape either."""
        context = await _handler_for()
        ws = _UnclosableWebSocketRoute("ws://127.0.0.1:9999/internal-endpoint")

        await context.ws_handler(ws)

        assert ws.connected is False

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self, stub_dns: None) -> None:
        """Cancellation is control flow, exactly as on the HTTP path.

        False-positive control for the catch-all: it must fail if that clause is
        ever widened to ``BaseException``, not prove the fix.
        """

        def _cancel(*_a: Any, **_kw: Any) -> None:
            raise asyncio.CancelledError

        context = await _handler_for()
        ws = _FakeWebSocketRoute("wss://cdn.example/socket")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("src.shared.security.playwright_guard.validate_public_url", _cancel)
            with pytest.raises(asyncio.CancelledError):
                await context.ws_handler(ws)

        assert ws.closed_with is None


class TestSharedWorkerMitigationIsInstalled:
    """The mitigation must be installed by ``install_ssrf_guard`` itself.

    The real proof that it *works* is the Chromium integration suite. This is
    the cheap structural half: a refactor that moves the route registrations
    around must not drop the init script, and the double records it so the
    omission is visible without a browser.
    """

    @pytest.mark.asyncio
    async def test_install_ssrf_guard_adds_the_shared_worker_init_script(self) -> None:
        context = await _handler_for()

        assert context.init_scripts, (
            "install_ssrf_guard registered no init script, so the SharedWorker channel -- "
            "which cannot be routed and is NOT fail-closed (it completes a TCP handshake "
            "against the target) -- is open again"
        )
        assert any("SharedWorker" in script for script in context.init_scripts), (
            f"an init script was installed but it does not mention SharedWorker: "
            f"{context.init_scripts}"
        )

    @pytest.mark.asyncio
    async def test_the_init_script_is_installed_before_the_route_handlers(self) -> None:
        """Ordering is load-bearing, not cosmetic.

        If a route registration raised, an init script added afterwards would
        never run, leaving a context the caller believes is guarded but whose
        SharedWorker channel is open. Pinning the order keeps a later edit from
        reintroducing that window.
        """
        context = await _handler_for()

        assert context.calls, "install_ssrf_guard registered nothing at all"
        assert context.calls[0] == "add_init_script", (
            f"the SharedWorker init script must be installed before the route handlers, "
            f"or a raising route registration leaves the channel open: {context.calls}"
        )
        assert "route" in context.calls and "route_web_socket" in context.calls, context.calls


class _GrantRaisingContext(_FakeContext):
    async def grant_permissions(self, permissions: Any, origin: Any = None) -> None:
        self.calls.append("grant_permissions")
        raise RuntimeError("Unknown permission: local-network-access")


class TestLocalNetworkAccessGrantIsScoped:
    """The LNA grant restores the ``allow_hosts`` contract on Playwright >= 1.60.

    The behavioural proof is the Chromium integration test
    ``test_guarded_context_still_allows_an_allow_listed_websocket``. That test
    cannot tell a scoped grant from a context-wide one (the guard still refuses
    the internal socket either way), so the scope is pinned here: a grant with
    ``origin=None`` would strip Chromium's Local Network Access check from every
    public page in the context, removing the browser's own second layer.
    """

    @pytest.mark.asyncio
    async def test_no_allow_hosts_means_no_grant(self) -> None:
        context = await _handler_for()

        assert context.grants == [], (
            f"a guard with no allow_hosts granted local-network-access: {context.grants}"
        )

    @pytest.mark.asyncio
    async def test_grant_names_exactly_the_allow_listed_origins(self) -> None:
        context = _FakeContext()
        hosts = ["minio.internal:9000", "Artifacts.Example"]
        await install_ssrf_guard(context, allow_hosts=hosts)  # ty: ignore[invalid-argument-type]  # why: _FakeContext duck-types BrowserContext

        assert context.grants == [
            (("local-network-access",), "http://minio.internal:9000"),
            (("local-network-access",), "https://minio.internal:9000"),
            (("local-network-access",), "http://artifacts.example"),
            (("local-network-access",), "https://artifacts.example"),
        ], context.grants
        assert all(origin for _, origin in context.grants), (
            f"a grant without an origin applies to EVERY page in the context: {context.grants}"
        )

    @pytest.mark.asyncio
    async def test_an_entry_that_is_not_host_port_is_never_granted(self) -> None:
        context = _FakeContext()
        hosts = ["http://evil.example/x", "user@host", "  "]
        await install_ssrf_guard(context, allow_hosts=hosts)  # ty: ignore[invalid-argument-type]  # why: _FakeContext duck-types BrowserContext

        assert context.grants == [], context.grants

    @pytest.mark.asyncio
    async def test_a_failed_grant_leaves_the_route_handlers_in_force(self) -> None:
        """Fail-closed: a failed grant only keeps Chromium refusing those sockets."""
        context = _GrantRaisingContext()
        hosts = ["minio.internal:9000"]
        await install_ssrf_guard(context, allow_hosts=hosts)  # ty: ignore[invalid-argument-type]  # why: _FakeContext duck-types BrowserContext

        assert context.handler is not None and context.ws_handler is not None, context.calls
        assert context.calls.index("route_web_socket") < context.calls.index("grant_permissions")
