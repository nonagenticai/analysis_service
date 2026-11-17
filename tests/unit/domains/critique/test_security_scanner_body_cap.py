"""RED-proof for the critique scanner's response byte cap, and for the browser gate.

WHY THIS FILE EXISTS
--------------------
``http_guard`` decides *who* the scanner may dereference. Nothing decided *how
much* they could make it hold. Every origin this package talks to is chosen by
the tenant, and two sinks read the whole body afterwards
(``_is_false_positive_file`` inspects ``resp.text``; ``_check_directory_listing``
lowercases it looking for "index of"). ``client.get`` buffers the entire body
before it returns, so a hostile origin answering a 5 kB ``.env`` probe with an
endless stream OOMed the pod, and a length check written after the ``await``
could not prevent it — the bytes were already resident.

WHAT IS LOAD-BEARING HERE
-------------------------
**How much was consumed, not that an exception was raised.** A post-hoc check
raises too, after the damage. So every cap test below counts the chunks actually
pulled off the wire: the declared-oversize path must consume *zero*, and the
undeclared path must stop at the first chunk that crosses the cap rather than
draining the payload. A fixture built with ``httpx.Response(content=...)`` cannot
tell the fixed implementation from the broken one, because such a body already
exists in memory before the guard runs — hence :class:`CountingStream`.

**The exception's base class, in both directions.**
``ScannerResponseTooLargeError`` is an ``OSError`` because every sink in this
package degrades through an ``except OSError`` clause it already had; rebasing it
on ``Exception`` would silently convert those fail-soft degradations into crashed
scan jobs. And it is deliberately *not* a ``ScannerSsrfBlockedError``:
``scanner.scan`` catches that type ahead of its broad ``OSError`` clause in order
to emit ``sec-ssrf-blocked``, so making the two related would report an oversized
homepage as an attempted SSRF — and would let the SSRF regression suite pass for
the wrong reason.

**The browser gate is actually wired.** PR #154 added a second Chromium launch in
``AxeAdapter`` after PR #153 introduced the gate, so the pod had two independent
unbounded browser paths while one of them believed it was respecting a ceiling of
four. That is a wiring defect, not a logic defect, and only a wiring test catches
it: the adapter's slot acquisition is asserted to be *held across the launch*,
not merely imported.

HERMETICITY
-----------
No network, no DNS, no browser. HTTP is ``httpx.MockTransport``; Playwright is a
stub injected into ``sys.modules`` for the duration of one test.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import playwright.async_api as playwright_api
import pytest

from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.critique.services.scanners.security_scanner.http_guard import (
    MAX_RESPONSE_BYTES,
    ScannerResponseTooLargeError,
    ScannerSsrfBlockedError,
    guarded_get,
    guarded_head,
)
from src.domains.critique.services.scanners.tool_adapters import axe_adapter as axe_adapter_mod
from src.shared.security.browser_gate import browser_slot

_Handler = Callable[[httpx.Request], httpx.Response]

PUBLIC_URL = "https://public.example.com/"

# Chunk size for the streaming fixtures. ``MAX_RESPONSE_BYTES`` is an exact
# multiple of it, which makes the expected abort point an exact integer.
CHUNK = 64 * 1024

#: Chunk ordinal at which a ``CHUNK``-sized stream must trip the default cap.
#: 2 MiB / 64 KiB = 32 chunks fit; the 33rd is the one that crosses.
ABORT_CHUNK = MAX_RESPONSE_BYTES // CHUNK + 1


class CountingStream(httpx.AsyncByteStream):
    """A response body that yields chunks lazily and records how many were pulled.

    This is what makes the cap testable at all: a streaming reader stops early
    and leaves most chunks un-yielded, while a buffering reader consumes all of
    them. The same chunk object is re-yielded, so the fixture never allocates the
    notional payload size itself.

    Attributes:
        chunks_yielded: How many chunks the consumer actually pulled.
    """

    def __init__(self, chunk_size: int, chunk_count: int) -> None:
        """Prepare a lazy body of *chunk_count* chunks of *chunk_size* bytes.

        Args:
            chunk_size: Size of each yielded chunk.
            chunk_count: Number of chunks the body would yield if fully drained.
        """
        self._chunk = b"x" * chunk_size
        self._chunk_count = chunk_count
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield the body lazily, counting each chunk handed over."""
        for _ in range(self._chunk_count):
            self.chunks_yielded += 1
            yield self._chunk


def _client(handler: _Handler) -> httpx.AsyncClient:
    """Build a client that can reach nothing but *handler*.

    Args:
        handler: Mock-transport responder.

    Returns:
        A real client — the capped path uses ``client.stream``, and a mock
        cannot model an async context manager yielding a response.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    """Route every ``httpx.AsyncClient`` the scanner builds through *handler*.

    Args:
        monkeypatch: Undoes the rebind on teardown.
        handler: Mock-transport responder to install.
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


def _streams(stream: CountingStream, headers: dict[str, str] | None = None) -> _Handler:
    """Serve *stream* as the body of every response.

    Args:
        stream: The lazy body to hand out.
        headers: Optional response headers. Omitting ``content-length`` is what
            forces the cap to be proven by the streaming accumulator rather than
            by ``_get_capped``'s cheap header pre-check.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers or {}, stream=stream)

    return _handle


def _oversized_handler() -> _Handler:
    """Build a handler whose every response overruns the default cap.

    Returns:
        A handler serving an undeclared body twice the size of the cap.
    """

    def _handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=CountingStream(chunk_size=CHUNK, chunk_count=ABORT_CHUNK * 2),
        )

    return _handle


# --------------------------------------------------------------------------- #
# The cap itself
# --------------------------------------------------------------------------- #
class TestGuardedGetByteCap:
    """``guarded_get`` bounds the body by default, on both paths."""

    async def test_declared_oversize_is_refused_without_reading_a_byte(self) -> None:
        """A ``Content-Length`` over the cap means the body is never touched.

        The cheap pre-check is the difference between refusing an announced
        multi-gigabyte response and streaming 2 MiB of it first.
        """
        stream = CountingStream(chunk_size=CHUNK, chunk_count=64)
        handler = _streams(stream, {"content-length": str(MAX_RESPONSE_BYTES + 1)})

        async with _client(handler) as client:
            with pytest.raises(ScannerResponseTooLargeError, match="declares"):
                await guarded_get(client, PUBLIC_URL)

        assert stream.chunks_yielded == 0

    async def test_undeclared_body_aborts_at_the_crossing_chunk(self) -> None:
        """An origin that declares nothing is stopped by the accumulator.

        The assertion is the chunk count, not the exception. A buffering
        implementation raises too — after draining all 66 chunks.
        """
        stream = CountingStream(chunk_size=CHUNK, chunk_count=ABORT_CHUNK * 2)

        async with _client(_streams(stream)) as client:
            with pytest.raises(ScannerResponseTooLargeError, match="exceeds maximum"):
                await guarded_get(client, PUBLIC_URL)

        assert stream.chunks_yielded == ABORT_CHUNK
        assert stream.chunks_yielded < ABORT_CHUNK * 2

    async def test_body_exactly_at_the_cap_succeeds(self) -> None:
        """Boundary, lower half: ``total > max_bytes`` and not ``>=``.

        An off-by-one here would silently refuse legitimate pages, and every
        sink would degrade to "no finding" without saying why.
        """
        body = b"y" * MAX_RESPONSE_BYTES

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=body)

        async with _client(_handle) as client:
            response = await guarded_get(client, PUBLIC_URL)

        assert len(response.content) == MAX_RESPONSE_BYTES
        # The rebuilt response must still look like the original to callers.
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html"

    async def test_one_byte_over_the_cap_raises(self) -> None:
        """Boundary, upper half. Partner of the test above; neither is meaningful alone."""
        body = b"y" * (MAX_RESPONSE_BYTES + 1)

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with _client(_handle) as client:
            with pytest.raises(ScannerResponseTooLargeError):
                await guarded_get(client, PUBLIC_URL)

    async def test_explicit_none_restores_the_unbounded_buffered_read(self) -> None:
        """``max_bytes=None`` is still a plain ``client.get``, not an infinite cap.

        The escape hatch is pinned so that "the cap is on by default" cannot
        quietly become "the cap is unconditional" — the redirect-chain sink and
        any future caller with a reason keep a documented way out.
        """
        body = b"z" * (MAX_RESPONSE_BYTES + 1024)

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with _client(_handle) as client:
            response = await guarded_get(client, PUBLIC_URL, max_bytes=None)

        assert len(response.content) == len(body)

    async def test_redirect_is_returned_unread_so_the_hop_walk_is_unchanged(self) -> None:
        """A ``30x`` is judged on status and ``Location`` alone.

        Reading a redirect body would be pointless work and would give a hostile
        origin a second lever: a chain of 30x responses each carrying megabytes.
        Asserting ``chunks_yielded == 0`` proves the body was skipped rather than
        read and discarded.
        """
        stream = CountingStream(chunk_size=CHUNK, chunk_count=64)

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"location": "https://elsewhere.example.com/"}, stream=stream
            )

        async with _client(_handle) as client:
            response = await guarded_get(client, PUBLIC_URL, max_redirects=0)

        assert response.status_code == 302
        assert response.headers["location"] == "https://elsewhere.example.com/"
        assert stream.chunks_yielded == 0

    async def test_redirection_still_works_with_the_cap_on(self) -> None:
        """Control: the cap did not break redirection itself."""

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "https://final.example.com/page"})
            return httpx.Response(200, content=b"final")

        async with _client(_handle) as client:
            response = await guarded_get(client, PUBLIC_URL)

        assert response.content == b"final"


class TestTheCapIsPerHopNotPerCall:
    """The cap binds every hop of a redirect chain, not just the first.

    WHY THIS CLASS EXISTS
    ---------------------
    The hop loop used to be inline in ``guarded_get``; it is now
    ``_walk_guarded``, a shared walk driven by a ``send`` callable so that
    ``GET`` and ``HEAD`` cannot drift into two subtly-different copies. That
    refactor moved the cap dispatch from the body of the loop into the ``GET``
    binding's ``send`` closure. Both shapes apply the cap on every hop — but the
    suite could not tell them apart: with the cap deliberately degraded to
    first-hop-only, the entire pre-existing cap suite still passed.

    That is a hole rather than a hypothetical. A first-hop-only cap is exactly
    what a plausible "optimisation" of this code would produce, and it hands a
    hostile origin the whole attack back: answer hop 0 with a 200-byte ``302``
    and stream the unbounded body from hop 1, which is the cheaper thing to do
    anyway. The property is asserted here so it survives the next edit.
    """

    async def test_body_cap_still_applies_after_a_redirect(self) -> None:
        """Hop 1, reached via ``302``, is capped exactly as hop 0 is.

        The assertion is the *chunk count*, not merely the exception: a cap that
        only ran on the first hop would let the redirected body be drained in
        full, and a post-hoc size check would raise after the pod had already
        paid for every byte.
        """
        stream = CountingStream(chunk_size=CHUNK, chunk_count=ABORT_CHUNK * 2)

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "https://second.example.com/big"})
            return httpx.Response(200, stream=stream)

        async with _client(_handle) as client:
            with pytest.raises(ScannerResponseTooLargeError, match="exceeds maximum"):
                await guarded_get(client, PUBLIC_URL)

        assert stream.chunks_yielded == ABORT_CHUNK
        assert stream.chunks_yielded < ABORT_CHUNK * 2

    async def test_declared_oversize_is_refused_on_a_redirected_hop_too(self) -> None:
        """The cheap ``Content-Length`` pre-check also survives the hop."""
        stream = CountingStream(chunk_size=CHUNK, chunk_count=64)

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "https://second.example.com/big"})
            return httpx.Response(
                200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, stream=stream
            )

        async with _client(_handle) as client:
            with pytest.raises(ScannerResponseTooLargeError, match="declares"):
                await guarded_get(client, PUBLIC_URL)

        assert stream.chunks_yielded == 0

    async def test_a_public_redirect_to_a_small_body_is_untouched(self) -> None:
        """False-positive control: the per-hop cap does not refuse ordinary chains.

        Without this, every assertion above would be satisfied by a
        ``guarded_get`` that simply raised on any redirect at all.
        """

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "https://second.example.com/ok"})
            return httpx.Response(200, content=b"small")

        async with _client(_handle) as client:
            response = await guarded_get(client, PUBLIC_URL)

        assert response.content == b"small"


class TestGuardedHeadSharesTheSameWalk:
    """``guarded_head`` is a binding over ``_walk_guarded``, not a second copy.

    A ``HEAD`` probe is a real connection to whatever the ``Location`` chain
    names, and its status code is itself the signal an attacker reads back out
    of the findings. It therefore needs the identical per-hop screen a body
    fetch gets — and must get it from the *same* loop, because a divergent
    private copy is precisely how one of the two paths ends up unguarded.
    """

    async def test_head_screens_a_redirect_into_the_tailnet(self) -> None:
        """A public host cannot launder a ``HEAD`` probe into the estate.

        ``100.64.0.5`` is prod Postgres, and ``ipaddress.is_private`` is
        ``False`` for the whole RFC 6598 range — which is how it stayed
        reachable. The block must land on the *hop*, after a clean first
        response.
        """

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": "http://100.64.0.5/"})
            raise AssertionError(f"guarded_head followed the chain to {request.url}")

        async with _client(_handle) as client:
            with pytest.raises(ScannerSsrfBlockedError):
                await guarded_head(client, PUBLIC_URL)

    async def test_head_screens_the_entry_url_before_connecting(self) -> None:
        """Hop 0 is screened too, and no request is issued for a blocked target."""

        def _handle(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"guarded_head connected to blocked {request.url}")

        async with _client(_handle) as client:
            with pytest.raises(ScannerSsrfBlockedError):
                await guarded_head(client, "http://169.254.169.254/latest/meta-data/")

    async def test_head_follows_a_public_chain_normally(self) -> None:
        """False-positive control: the screen refuses hops, not redirection."""

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(301, headers={"location": "https://final.example.com/x"})
            return httpx.Response(204)

        async with _client(_handle) as client:
            response = await guarded_head(client, PUBLIC_URL)

        assert response.status_code == 204


class TestTooLargeErrorTaxonomy:
    """The base class is a contract; the *non*-relation to SSRF is another one."""

    def test_too_large_is_an_oserror(self) -> None:
        """Every sink degrades through an ``except OSError`` clause it already had.

        Rebasing this on ``Exception`` would convert four fail-soft degradations
        into hard failures and no other test would notice.
        """
        assert issubclass(ScannerResponseTooLargeError, OSError)

    def test_a_bare_except_oserror_catches_it(self) -> None:
        """The same property as the call sites actually spell it."""
        caught = False
        try:
            raise ScannerResponseTooLargeError("too big")
        except OSError:
            caught = True
        assert caught

    def test_too_large_is_not_an_ssrf_block(self) -> None:
        """An oversized page must never be reported as ``sec-ssrf-blocked``.

        ``scanner.scan`` catches ``ScannerSsrfBlockedError`` *above* its broad
        ``OSError`` clause. If the two types were related, that clause would
        swallow this one and relabel a resource verdict as a security verdict.
        """
        assert not issubclass(ScannerResponseTooLargeError, ScannerSsrfBlockedError)

    def test_an_ssrf_block_is_not_a_too_large(self) -> None:
        """The other direction, so "siblings" cannot degrade into "one subclasses
        the other" by a future edit that happens to point the right way."""
        assert not issubclass(ScannerSsrfBlockedError, ScannerResponseTooLargeError)

    def test_an_except_for_one_does_not_catch_the_other(self) -> None:
        """The taxonomy as an ``except`` clause sees it, not as ``issubclass`` does."""
        with pytest.raises(ScannerResponseTooLargeError):
            try:
                raise ScannerResponseTooLargeError("too big")
            except ScannerSsrfBlockedError:  # pragma: no cover - must not catch
                pytest.fail("an oversized body was caught as an SSRF block")


# --------------------------------------------------------------------------- #
# Every sink degrades rather than raising
# --------------------------------------------------------------------------- #
class TestSinksDegradeOnAnOversizedBody:
    """Capping a body can make a check a no-op; it can never crash a scan job."""

    async def test_probe_exposed_file_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The ``.env`` probe expects a few kB; megabytes are simply not a finding."""
        _install_transport(monkeypatch, _oversized_handler())

        result = await SecurityScanner._probe_exposed_file(
            "https://public.example.com",
            ".env",
            "sec-exposed-env",
            "critical",
            asyncio.Semaphore(5),
        )

        assert result is None

    async def test_check_cors_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CORS reads headers only, but the cap fires before they are handed over."""
        _install_transport(monkeypatch, _oversized_handler())

        findings = await SecurityScanner()._check_cors(PUBLIC_URL)

        assert findings == []

    async def test_check_directory_listing_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This sink lowercases the whole body, so it is the one an unbounded
        response hurts most — and it degrades per candidate and overall."""
        _install_transport(monkeypatch, _oversized_handler())

        findings = await SecurityScanner()._check_directory_listing(PUBLIC_URL)

        assert findings == []

    async def test_scan_reports_a_scan_error_not_an_ssrf_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The taxonomy, end to end.

        An origin that answers the top-level request with megabytes is worth
        surfacing — as ``sec-scan-error``. Reporting it as ``sec-ssrf-blocked``
        would tell an operator the scanner refused to talk to their own site.
        """
        _install_transport(monkeypatch, _oversized_handler())

        findings = await SecurityScanner().scan(PUBLIC_URL)

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-scan-error" in check_ids
        assert "sec-ssrf-blocked" not in check_ids

    def test_the_cap_is_default_on_not_opt_in(self) -> None:
        """Every sink above degrades because it inherits the default, not because
        it asked for a cap. Flipping this default to ``None`` would restore the
        unbounded read at all four call sites at once, and each of them would
        keep passing its own behavioural test on a small fixture body — so the
        default itself is asserted directly.
        """
        default = inspect.signature(guarded_get).parameters["max_bytes"].default

        assert default == MAX_RESPONSE_BYTES


# --------------------------------------------------------------------------- #
# The browser gate is wired into the *second* Chromium launcher
# --------------------------------------------------------------------------- #
class _SlotSpy:
    """A stand-in for ``browser_slot`` that records when it is held.

    Attributes:
        entries: How many times the gate was entered.
        exits: How many times it was left.
        held_at_launch: Whether a slot was held when Chromium was launched.
            ``None`` if no launch happened at all.
        held_at_close: Whether a slot was still held when the browser closed.
    """

    def __init__(self) -> None:
        """Start with a clean log."""
        self.entries = 0
        self.exits = 0
        self.held = False
        self.held_at_launch: bool | None = None
        self.held_at_close: bool | None = None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a notional slot for the enclosed block.

        Yields:
            ``None``; the slot is released on exit, including on exception.
        """
        self.entries += 1
        self.held = True
        try:
            yield
        finally:
            self.held = False
            self.exits += 1


class _FakePage:
    """The two page calls ``AxeAdapter.scan`` makes, and nothing else."""

    def set_default_timeout(self, timeout_ms: float) -> None:
        """Accept the timeout the adapter sets.

        Args:
            timeout_ms: Ignored.
        """

    async def goto(
        self,
        url: str,
        wait_until: str = "load",
        timeout: float = 0,  # noqa: ASYNC109 — why: mirrors playwright Page.goto's signature
    ) -> None:
        """Pretend to navigate.

        Args:
            url: Ignored.
            wait_until: Ignored.
            timeout: Ignored.
        """

    async def evaluate(self, script: str) -> dict[str, list[dict[str, str]]]:
        """Return an axe result with no violations.

        Args:
            script: Ignored.

        Returns:
            An empty axe report, so ``scan`` completes its happy path.
        """
        return {"violations": []}


class _FakeContext:
    """A browser context that can make exactly one page."""

    async def new_page(self) -> _FakePage:
        """Return a stub page."""
        return _FakePage()


class _FakeBrowser:
    """A browser whose only observable behaviour is when it opens and closes."""

    def __init__(self, spy: _SlotSpy) -> None:
        """Bind the spy this browser reports its lifetime to.

        Args:
            spy: The slot spy to record against.
        """
        self._spy = spy

    async def new_context(self, **kwargs: object) -> _FakeContext:
        """Return a stub context.

        Takes ``**kwargs`` like the real ``Browser.new_context`` (the adapter
        passes ``bypass_csp=True``); a narrower double fails on argument lists,
        not behaviour.
        """
        return _FakeContext()

    async def close(self) -> None:
        """Record whether a slot was still held at close."""
        self._spy.held_at_close = self._spy.held


class _FakeChromium:
    """The ``pw.chromium`` namespace."""

    def __init__(self, spy: _SlotSpy) -> None:
        """Bind the spy this launcher reports against.

        Args:
            spy: The slot spy to record against.
        """
        self._spy = spy

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        """Record whether a slot was held at the moment of launch.

        This single line is the regression assertion: the ungated adapter
        launches Chromium with ``held`` still ``False``.

        Args:
            headless: Ignored.

        Returns:
            A stub browser.
        """
        self._spy.held_at_launch = self._spy.held
        return _FakeBrowser(self._spy)


class _FakePlaywright:
    """What ``async_playwright()`` yields."""

    def __init__(self, spy: _SlotSpy) -> None:
        """Bind the spy the launcher reports against.

        Args:
            spy: The slot spy to record against.
        """
        self.chromium = _FakeChromium(spy)


class _FakePlaywrightCM:
    """``async_playwright()`` — an async context manager, not a coroutine."""

    def __init__(self, spy: _SlotSpy) -> None:
        """Bind the spy.

        Args:
            spy: The slot spy to record against.
        """
        self._spy = spy

    async def __aenter__(self) -> _FakePlaywright:
        """Enter the playwright context.

        Returns:
            The stub playwright namespace.
        """
        return _FakePlaywright(self._spy)

    async def __aexit__(self, *_exc: object) -> bool:
        """Leave the playwright context.

        Returns:
            ``False``; exceptions are never suppressed here.
        """
        return False


async def _noop_guard(_context: object) -> None:
    """Stand in for ``install_ssrf_guard``; its own coverage lives elsewhere.

    Args:
        _context: Ignored.
    """


@pytest.fixture
def slot_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SlotSpy]:
    """Stub out Playwright, settings and the browser gate for ``AxeAdapter.scan``.

    Args:
        monkeypatch: Undoes every rebind on teardown.

    Yields:
        The spy recording the adapter's use of the gate.
    """
    spy = _SlotSpy()

    # ``scan`` does ``from playwright.async_api import async_playwright`` at call
    # time, so rebinding the attribute on the real module is enough — no
    # ``sys.modules`` surgery, and the import itself stays honest: if Playwright
    # ever stopped being a dependency this fixture would fail rather than
    # silently stub a module that no longer exists.
    monkeypatch.setattr(playwright_api, "async_playwright", lambda: _FakePlaywrightCM(spy))
    monkeypatch.setattr(axe_adapter_mod, "browser_slot", spy.slot)
    monkeypatch.setattr(
        axe_adapter_mod,
        "get_settings",
        lambda: SimpleNamespace(external_tools=SimpleNamespace(axe_timeout=30, axe_enabled=True)),
    )
    monkeypatch.setattr(axe_adapter_mod, "install_ssrf_guard", _noop_guard)
    yield spy


class TestAxeAdapterTakesABrowserSlot:
    """The regression PR #154 introduced: a second, ungated Chromium launch.

    The gate was added at brand's ``FetchStage`` and is pod-wide because the
    memory it rations is pod-wide. ``AxeAdapter`` then began launching its own
    Chromium without taking a slot, so a critique accessibility sweep and a brand
    extraction could each believe they were respecting a ceiling of four while
    the pod carried eight browsers. Presence of the import proves nothing —
    only holding the slot across the launch does.
    """

    async def test_the_slot_is_held_across_the_launch(self, slot_spy: _SlotSpy) -> None:
        """Entered once, held at launch, held at close, released afterwards."""
        findings = await axe_adapter_mod.AxeAdapter().scan("https://example.com")

        assert findings == []
        assert slot_spy.entries == 1
        assert slot_spy.held_at_launch is True, "Chromium launched without a browser slot"
        assert slot_spy.held_at_close is True, "slot released while a browser was resident"
        assert slot_spy.exits == 1
        assert slot_spy.held is False

    def test_the_adapter_uses_the_shared_pod_wide_gate(self) -> None:
        """Not a critique-local ceiling: the same object brand's ``FetchStage`` takes.

        Two ceilings of four are a ceiling of eight, which is the OOM the gate
        exists to prevent — so this asserts the identity of the callable, which a
        copied-and-renamed local gate would fail.
        """
        assert axe_adapter_mod.browser_slot is browser_slot
