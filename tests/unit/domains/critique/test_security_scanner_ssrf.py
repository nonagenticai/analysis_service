"""RED-proof that the security scanner's SSRF guard holds *per connection*.

WHY THIS FILE EXISTS
--------------------
``SecurityScanner.scan`` screened the job URL exactly once, at entry. That
single verdict was then relied upon by five sinks which each opened their own
connection -- ``_probe_exposed_file``, ``_check_cors``,
``_check_directory_listing``, ``_check_redirect_chain`` and ``_check_tls`` --
and three of those followed redirects automatically. A one-shot check covers
neither case:

* Two of the sinks ``urljoin`` a *derived* target (``/.env``, ``/images/``) out
  of the scanned origin, so the string dereferenced is not the string screened.
* A perfectly public origin the entry check admits is free to answer
  ``302 Location: https://100.64.0.5/`` and steer the probe wherever it
  likes.

``100.64.0.0/10`` is RFC 6598 shared address space and is this fleet's entire
Tailscale tailnet -- prod Postgres (``100.64.0.5``), the Docker registry
(``100.64.0.6``), the k3s control plane (``100.64.0.7``).
``ipaddress.ip_address(...).is_private`` is **False** for every one of them,
which is exactly why the range stayed reachable through guards written as a
"private networks" list. The bracketing controls below (``100.63.255.255`` /
``100.128.0.0``) exist so a future edit cannot "fix" this by prefix-matching
``100.`` and quietly denying two legitimate public /10 neighbours.

WHAT EACH GROUP LOCKS DOWN
--------------------------
``TestEnsureSafeUrl``
    The bare predicate wrapper over the shared conformance vector, including
    the two bracketing allow controls.

``TestGuardedGetHopWalk``
    ``guarded_get`` screens the initial URL *and* re-screens every
    ``Location``. The load-bearing case is the second one: a public URL that
    302s into the tailnet must raise on hop 2, and the tailnet host must never
    appear in the transport's request log -- "never fetched", not "no crash".
    Also pins the three non-raising exits (no ``Location``, hop budget spent,
    ``max_redirects=0``) as *returns*, because a merely misconfigured site is
    not a blocked one and must not be reported as such.

``TestSinksNeverFetchBlockedTargets``
    Each HTTP sink, handed a blocked target, issues **zero** requests and still
    returns its fail-soft value (``None`` / ``[]``). Degrading is the contract:
    adding the guard must not be able to turn a scannable site into a crashed
    job. One control proves the sinks still do their job on a permitted origin.

``TestTlsCheckGuard``
    ``_check_tls`` is the one sink with no HTTP layer at all -- a raw
    ``socket.create_connection`` to an attacker-chosen ``(host, port)``, the
    most direct probe primitive in the scanner. It must emit a
    ``sec-ssrf-blocked`` info finding and never reach the socket.

``TestScanEmitsBlockedFinding``
    End to end: both the entry-blocked path and the redirect-into-the-tailnet
    path surface ``sec-ssrf-blocked``, and neither degrades into the generic
    ``sec-scan-error``. That ordering is a real constraint --
    ``ScannerSsrfBlockedError`` subclasses ``OSError``, so its ``except`` clause
    only works while it sits *above* the broad one.

``TestBlockedErrorIsOsError``
    The subclassing is deliberate and load-bearing, not incidental. Every sink
    degrades through an ``except OSError`` clause it already had; a refactor
    that rebased this exception on ``Exception`` would silently convert five
    fail-soft degradations into hard failures, with no other test in this repo
    noticing.

HERMETICITY
-----------
No real network, DNS or socket is touched. HTTP goes through
``httpx.MockTransport``; the transport doubles as the request log the
"never fetched" assertions read. ``_check_tls``'s connect is denied by
rebinding ``tls_checks.socket`` to a stub -- module-scoped on purpose, exactly
as ``tests/dns_stub.py`` argues: patching ``socket.create_connection``
process-globally would also hijack psycopg. ``dns_stub`` itself is not needed
here because the predicate under test (``_is_ssrf_safe``) is deliberately
structural and performs no resolution -- it has to stay callable per hop from
inside an event loop.

The conformance vectors are mirrored from
``tests/unit/shared/test_ssrf_tailnet_guard.py`` rather than imported: this
tree exposes no test-package import path, and a divergence between the two
copies is itself worth failing on.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import NoReturn

import httpx
import pytest

from src.domains.critique.services.scanners.security_scanner import (
    SecurityScanner,
)
from src.domains.critique.services.scanners.security_scanner import (
    tls_checks as tls_checks_mod,
)
from src.domains.critique.services.scanners.security_scanner.http_guard import (
    ScannerSsrfBlockedError,
    ensure_safe_url,
    guarded_get,
)

# Live fleet addresses, all inside 100.64.0.0/10 -- the range CPython reports as
# neither private nor reserved.
WORKER4_POSTGRES = "100.64.0.5"
WORKER3_REGISTRY = "100.64.0.6"
MASTER_K3S = "100.64.0.7"

#: Mirrors the shared conformance vector. Every entry must be refused.
BLOCKED_URLS = (
    f"https://{WORKER4_POSTGRES}/",
    f"https://{WORKER3_REGISTRY}/v2/_catalog",
    f"https://{MASTER_K3S}/",
    "https://100.64.0.1/",
    "https://[::ffff:100.64.0.1]/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/",
    "http://127.0.0.1/",
    "http://localhost:8080/admin",
    "https://registry.internal/v2/_catalog",
    "https://postgres.internal/",
)

#: The deny is the CIDR, not the ``100.`` prefix. ``100.63.255.255`` sits
#: immediately below ``100.64.0.0/10`` and ``100.128.0.0`` immediately above; if
#: either is refused the implementation has become a blanket deny.
ALLOWED_URLS = (
    "https://100.63.255.255/",
    "https://100.128.0.0/",
    "https://8.8.8.8/",
    "https://public.example.com/",
)

PUBLIC_URL = "https://public.example.com/"

Responder = Callable[[httpx.Request], httpx.Response]


class _Recorder:
    """A ``MockTransport`` handler that also logs what it was asked to fetch.

    The log is the evidence behind every "never fetched" assertion here: a
    guard that raises *after* opening the connection would still satisfy a
    ``pytest.raises`` check, and would still be a live SSRF probe.

    Attributes:
        requests: Every request the transport actually received, in order.
    """

    def __init__(self, responder: Responder) -> None:
        """Wrap *responder* with request recording.

        Args:
            responder: The handler producing the canned response.
        """
        self._responder = responder
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request*, then delegate to the wrapped responder.

        Args:
            request: The outgoing request the transport intercepted.

        Returns:
            The canned response.
        """
        self.requests.append(request)
        return self._responder(request)

    @property
    def urls(self) -> list[str]:
        """Return the fetched URLs as strings, for readable assertions."""
        return [str(request.url) for request in self.requests]


class _RefusingSocket:
    """Stand-in for the ``socket`` module as ``tls_checks`` uses it.

    Rebound onto the module rather than patched process-globally: the real
    ``socket.create_connection`` is shared with psycopg and every other client
    in the interpreter, and hijacking it is a far wider blast radius than this
    assertion needs.

    Attributes:
        attempts: Addresses a connect was attempted against. Must stay empty.
    """

    def __init__(self) -> None:
        """Start with a clean attempt log."""
        self.attempts: list[tuple[str, int]] = []

    def create_connection(
        self,
        address: tuple[str, int],
        timeout: float | None = None,
    ) -> NoReturn:
        """Record and refuse a connect attempt.

        Args:
            address: The ``(host, port)`` pair the check tried to reach.
            timeout: Ignored; present for signature parity with ``socket``.

        Raises:
            AssertionError: Always. Reaching this function is the failure.
        """
        self.attempts.append(address)
        raise AssertionError(f"TLS check opened a socket to {address!r}")


def _responds(status: int, headers: dict[str, str] | None = None) -> Responder:
    """Build a handler returning one fixed response for every request.

    Args:
        status: Status code to answer with.
        headers: Optional response headers.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {})

    return _respond


def _client(recorder: _Recorder) -> httpx.AsyncClient:
    """Build a client whose only transport is *recorder*.

    Args:
        recorder: Handler-and-log for the mock transport.

    Returns:
        A client that can reach nothing but the recorder.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder))


def _install_transport(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """Route every ``httpx.AsyncClient`` the scanner builds through *recorder*.

    The scanner modules do ``import httpx`` and construct clients inline, so the
    seam is the class on the shared module object. ``functools.partial`` keeps
    the constructor kwargs the production code passes (``timeout``,
    ``follow_redirects``, ``verify``) working untouched.

    Args:
        monkeypatch: Undoes the rebind on teardown.
        recorder: Handler-and-log to install.
    """
    transport = httpx.MockTransport(recorder)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


class TestEnsureSafeUrl:
    """The bare predicate wrapper, over the shared conformance vector."""

    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_blocked_url_raises(self, url: str) -> None:
        """Every vector entry is refused before a connection can be opened."""
        with pytest.raises(ScannerSsrfBlockedError):
            ensure_safe_url(url)

    @pytest.mark.parametrize("url", ALLOWED_URLS)
    def test_allowed_url_passes(self, url: str) -> None:
        """The guard must not have become a blanket deny.

        ``100.63.255.255`` and ``100.128.0.0`` bracket the CGNAT /10. If either
        raises, the implementation is prefix-matching ``100.`` instead of
        testing membership of ``100.64.0.0/10``.
        """
        ensure_safe_url(url)


class TestGuardedGetHopWalk:
    """``guarded_get`` re-screens the target on every hop, not just the first."""

    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_blocked_initial_url_is_never_fetched(self, url: str) -> None:
        """The screen precedes the request, so the transport sees nothing."""
        recorder = _Recorder(_responds(200))
        async with _client(recorder) as client:
            with pytest.raises(ScannerSsrfBlockedError):
                await guarded_get(client, url)
        assert recorder.requests == []

    async def test_public_url_redirecting_into_tailnet_raises_on_hop_two(self) -> None:
        """The whole point of the fix.

        Hop 1 is a legitimate public host the entry screen admits. Its
        ``Location`` points at prod Postgres. The tailnet address must be
        refused *and* must never appear in the request log -- if it does, httpx
        followed the redirect itself and the per-hop screen came too late.
        """
        recorder = _Recorder(_responds(302, {"location": f"https://{WORKER4_POSTGRES}/latest"}))
        async with _client(recorder) as client:
            with pytest.raises(ScannerSsrfBlockedError):
                await guarded_get(client, PUBLIC_URL)
        assert recorder.urls == [PUBLIC_URL]

    async def test_relative_redirect_into_tailnet_is_resolved_then_screened(self) -> None:
        """A protocol-relative ``Location`` is joined first, then screened.

        ``//100.64.0.5/`` carries no scheme, so a screen applied to the raw
        header value would see a hostname-less string and wave it through.
        """
        recorder = _Recorder(_responds(302, {"location": f"//{WORKER4_POSTGRES}/"}))
        async with _client(recorder) as client:
            with pytest.raises(ScannerSsrfBlockedError):
                await guarded_get(client, PUBLIC_URL)
        assert recorder.urls == [PUBLIC_URL]

    async def test_public_redirect_to_public_is_followed(self) -> None:
        """Control: the guard blocks the range, not redirection as such."""

        def _respond(request: httpx.Request) -> httpx.Response:
            if request.url.host == "start.example.com":
                return httpx.Response(302, headers={"location": "https://final.example.com/page"})
            return httpx.Response(200, text="final")

        recorder = _Recorder(_respond)
        async with _client(recorder) as client:
            response = await guarded_get(client, "https://start.example.com/")
        assert response.status_code == 200
        assert recorder.urls == ["https://start.example.com/", "https://final.example.com/page"]

    async def test_redirect_without_location_returns_the_response(self) -> None:
        """A misconfigured site is not a blocked one.

        The walk ends and hands back the ``30x`` it could not follow, so the
        caller produces no finding rather than a spurious block report.
        """
        recorder = _Recorder(_responds(302))
        async with _client(recorder) as client:
            response = await guarded_get(client, PUBLIC_URL)
        assert response.status_code == 302
        assert recorder.urls == [PUBLIC_URL]

    async def test_hop_budget_exhausted_returns_last_response(self) -> None:
        """A chain longer than the budget stops and returns, it does not raise.

        ``max_redirects=2`` permits the initial fetch plus two follows: three
        requests, then the walk gives up on a still-redirecting response.
        """
        hops = iter(range(100))

        def _respond(_request: httpx.Request) -> httpx.Response:
            location = f"https://hop{next(hops)}.example.com/"
            return httpx.Response(302, headers={"location": location})

        recorder = _Recorder(_respond)
        async with _client(recorder) as client:
            response = await guarded_get(client, PUBLIC_URL, max_redirects=2)
        assert response.status_code == 302
        assert len(recorder.requests) == 3

    async def test_max_redirects_zero_never_follows(self) -> None:
        """``max_redirects=0`` fetches exactly one URL.

        ``_probe_exposed_file`` depends on this: it has always declined to
        follow, and a ``30x`` simply is not the ``200`` that would make it emit
        a finding.
        """
        recorder = _Recorder(_responds(302, {"location": "https://elsewhere.example.com/"}))
        async with _client(recorder) as client:
            response = await guarded_get(client, PUBLIC_URL, max_redirects=0)
        assert response.status_code == 302
        assert recorder.urls == [PUBLIC_URL]

    async def test_headers_are_passed_through_on_the_wire(self) -> None:
        """CORS probing needs its ``Origin`` to survive the guard wrapper.

        Asserted on the outgoing ``httpx.Request`` rather than on call kwargs:
        the header has to reach the socket, and only the transport can prove
        that.
        """
        recorder = _Recorder(_responds(200))
        async with _client(recorder) as client:
            await guarded_get(client, PUBLIC_URL, headers={"Origin": "https://evil.example.com"})
        assert recorder.requests[0].headers["origin"] == "https://evil.example.com"


class TestSinksNeverFetchBlockedTargets:
    """Each HTTP sink refuses a blocked target without issuing a request, and
    still degrades to its fail-soft value rather than raising."""

    async def test_probe_exposed_file_returns_none_and_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``file_url`` is ``urljoin``-derived, so it is screened here or nowhere."""
        recorder = _Recorder(_responds(200, {"content-type": "text/plain"}))
        _install_transport(monkeypatch, recorder)

        result = await SecurityScanner._probe_exposed_file(
            f"https://{WORKER4_POSTGRES}",
            ".env",
            "sec-exposed-env",
            "critical",
            asyncio.Semaphore(5),
        )

        assert result is None
        assert recorder.requests == []

    async def test_check_cors_returns_empty_and_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CORS runs on its own connection and used to follow redirects."""
        recorder = _Recorder(_responds(200, {"access-control-allow-origin": "*"}))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_cors("https://registry.internal/")

        assert findings == []
        assert recorder.requests == []

    async def test_check_directory_listing_returns_empty_and_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every candidate directory is derived from the origin, and every one
        of them is refused -- the loop degrades per candidate and overall."""
        recorder = _Recorder(_responds(200, {"content-type": "text/html"}))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_directory_listing(f"https://{WORKER4_POSTGRES}/")

        assert findings == []
        assert recorder.requests == []

    async def test_check_redirect_chain_returns_empty_and_never_fetches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reference sink -- unchanged by the fix, asserted so it stays that way."""
        recorder = _Recorder(_responds(200))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_redirect_chain("https://postgres.internal/")

        assert findings == []
        assert recorder.requests == []

    async def test_public_directory_listing_still_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the sink still does its job against a permitted origin."""

        def _respond(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<h1>Index of /images/</h1>")

        recorder = _Recorder(_respond)
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_directory_listing(PUBLIC_URL)

        assert [f["check_id"] for f in findings] == ["sec-directory-listing"]
        assert recorder.urls == ["https://public.example.com/images/"]


class TestTlsCheckGuard:
    """The one sink with no HTTP layer: a raw connect to an attacker-chosen host."""

    def test_tailnet_host_yields_blocked_finding_and_no_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Emits ``sec-ssrf-blocked`` and never reaches ``create_connection``."""
        refusing = _RefusingSocket()
        monkeypatch.setattr(tls_checks_mod, "socket", refusing)

        findings = SecurityScanner()._check_tls(f"https://{WORKER4_POSTGRES}/")

        assert [f["check_id"] for f in findings] == ["sec-ssrf-blocked"]
        assert [f["severity"] for f in findings] == ["info"]
        assert refusing.attempts == []

    @pytest.mark.parametrize("host", ["registry.internal", "postgres.internal", MASTER_K3S])
    def test_internal_names_and_addresses_all_blocked(
        self, monkeypatch: pytest.MonkeyPatch, host: str
    ) -> None:
        """Names resolve into the tailnet via split-DNS; addresses are in it
        outright. Both must die on the string, before the connect."""
        refusing = _RefusingSocket()
        monkeypatch.setattr(tls_checks_mod, "socket", refusing)

        findings = SecurityScanner()._check_tls(f"https://{host}/")

        assert [f["check_id"] for f in findings] == ["sec-ssrf-blocked"]
        assert refusing.attempts == []

    def test_plain_http_tailnet_target_also_opens_no_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``http`` scheme short-circuits above the SSRF branch, so this
        path reports ``sec-no-https`` instead -- pinned because the property
        that matters (no connect) is reached by a different route, and a future
        reorder of those two branches must not lose it.
        """
        refusing = _RefusingSocket()
        monkeypatch.setattr(tls_checks_mod, "socket", refusing)

        findings = SecurityScanner()._check_tls(f"http://{WORKER4_POSTGRES}/")

        assert [f["check_id"] for f in findings] == ["sec-no-https"]
        assert refusing.attempts == []


class TestScanEmitsBlockedFinding:
    """End-to-end: the coordinator surfaces the block instead of crashing."""

    async def test_blocked_entry_url_emits_blocked_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-existing entry screen, still intact after the refactor."""
        recorder = _Recorder(_responds(200))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner().scan("https://registry.internal/")

        assert [f["check_id"] for f in findings] == ["sec-ssrf-blocked"]
        assert recorder.requests == []

    async def test_redirect_into_tailnet_emits_blocked_not_scan_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clause ordering is the assertion.

        ``ScannerSsrfBlockedError`` subclasses ``OSError``, so if its ``except``
        ever slips below the broad ``(ValueError, ..., OSError, RuntimeError)``
        clause the block is silently relabelled ``sec-scan-error``. The scan
        must also never have touched the tailnet host.
        """

        def _respond(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(302, headers={"location": f"https://{WORKER4_POSTGRES}/"})
            return httpx.Response(200)

        recorder = _Recorder(_respond)
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner().scan(PUBLIC_URL)

        check_ids = [f["check_id"] for f in findings]
        assert "sec-ssrf-blocked" in check_ids
        assert "sec-scan-error" not in check_ids
        assert recorder.urls == [PUBLIC_URL]


class TestBlockedErrorIsOsError:
    """The base class is a contract, not an implementation detail."""

    def test_blocked_error_subclasses_oserror(self) -> None:
        """Five sinks degrade through an ``except OSError`` clause they already
        had. Rebasing this exception on ``Exception`` would convert every one of
        those fail-soft degradations into a hard failure, and no other test in
        this repo would notice -- so the base class is asserted directly.
        """
        assert issubclass(ScannerSsrfBlockedError, OSError)

    def test_a_bare_except_oserror_catches_it(self) -> None:
        """The same property as the call sites actually spell it."""
        caught = False
        try:
            ensure_safe_url(f"https://{WORKER4_POSTGRES}/")
        except OSError:
            caught = True
        assert caught
