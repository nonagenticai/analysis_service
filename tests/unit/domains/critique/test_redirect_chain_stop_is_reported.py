"""Pins that a redirect walk stopped for a security reason says so out loud.

The refusal itself is NOT new: ``_check_redirect_chain`` has always declined to
follow a hop into a non-public target. What was missing is the REPORT -- it
``break``\\ ed in silence, so a chain truncated by the scanner's own guard looked
identical to one that simply ended, and the defensive action was invisible in
the finding list. These tests lock the ``sec-ssrf-blocked`` finding in place
while re-asserting that the blocked URL is still never fetched.
"""

from __future__ import annotations

import functools
from collections.abc import Callable

import httpx
import pytest

from src.domains.critique.services.scanners.security_scanner import SecurityScanner

#: Prod Postgres on the tailnet. ``100.64.0.0/10`` is RFC 6598 shared address
#: space: ``ipaddress.ip_address("100.64.0.5").is_private`` is **False**,
#: which is exactly why this range has to be denied explicitly.
TAILNET_HOST = "100.64.0.5"
TAILNET_URL = f"http://{TAILNET_HOST}/"

#: Public literals, so nothing here needs a name lookup. TEST-NET-3
#: (``203.0.113.0/24``) is deliberately avoided: CPython reports it as private,
#: so the guard blocks it and it cannot play the "public" role.
PUBLIC_HOST = "8.8.8.8"
PUBLIC_NEXT_HOST = "1.1.1.1"

#: Same alias the sibling SSRF suite uses for a ``MockTransport`` handler.
Responder = Callable[[httpx.Request], httpx.Response]


class _Recorder:
    """A ``MockTransport`` handler that logs everything it was asked to fetch.

    The log is the evidence behind the "never fetched" assertion: a guard that
    reported the block *after* opening the connection would still produce the
    finding, and would still be a live SSRF probe.

    Attributes:
        requests: Every request the transport received, in order.
    """

    def __init__(self, responder: Responder) -> None:
        """Wrap *responder* with request recording.

        Args:
            responder: Callable turning an ``httpx.Request`` into a response.
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


def _install_transport(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """Route every ``httpx.AsyncClient`` the scanner builds through *recorder*.

    Mirrors the seam ``test_security_scanner_ssrf.py`` already uses: the scanner
    modules do ``import httpx`` and construct clients inline, so the class on
    the shared module object is the interception point, and ``functools.partial``
    keeps the production constructor kwargs working.

    Args:
        monkeypatch: Undoes the rebind on teardown.
        recorder: Handler-and-log to install.
    """
    transport = httpx.MockTransport(recorder)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


def _redirect_once(entry_host: str, location: str) -> Responder:
    """Build a handler that 302s the entry host once, then answers ``200``.

    Args:
        entry_host: Host whose request gets the redirect.
        location: ``Location`` header served for that request.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == entry_host:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, text="ok")

    return _respond


class TestRedirectChainStopIsReported:
    """The stop is recorded as a finding, and the hop is still not taken."""

    async def test_hop_into_tailnet_emits_blocked_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The previously silent case: the walk stops and now says why."""
        recorder = _Recorder(_redirect_once(PUBLIC_HOST, TAILNET_URL))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_redirect_chain(f"http://{PUBLIC_HOST}/")

        blocked = [f for f in findings if f["check_id"] == "sec-ssrf-blocked"]
        assert len(blocked) == 1
        assert blocked[0]["severity"] == "info"
        assert TAILNET_URL in blocked[0]["evidence"]

    async def test_blocked_hop_is_never_fetched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The finding must come from refusing the hop, not from taking it.

        Reporting after following the redirect would satisfy the assertion
        above while still being a live probe into the tailnet, so the request
        log is asserted directly.
        """
        recorder = _Recorder(_redirect_once(PUBLIC_HOST, TAILNET_URL))
        _install_transport(monkeypatch, recorder)

        await SecurityScanner()._check_redirect_chain(f"http://{PUBLIC_HOST}/")

        assert recorder.urls == [f"http://{PUBLIC_HOST}/"]
        assert all(TAILNET_HOST not in url for url in recorder.urls)

    async def test_downgrade_finding_for_the_same_hop_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One hop can be both a downgrade and a blocked target.

        The screen sits *below* the HTTPS-to-HTTP check precisely so the
        downgrade is still reported; reordering the two would silently drop it.
        """
        recorder = _Recorder(_redirect_once(PUBLIC_HOST, TAILNET_URL))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_redirect_chain(f"https://{PUBLIC_HOST}/")

        check_ids = [f["check_id"] for f in findings]
        assert "sec-redirect-to-http" in check_ids
        assert "sec-ssrf-blocked" in check_ids
        assert recorder.urls == [f"https://{PUBLIC_HOST}/"]

    async def test_public_chain_is_followed_and_emits_no_blocked_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: without it the suite would pass on an unconditional emit."""
        final_url = f"https://{PUBLIC_NEXT_HOST}/final"
        recorder = _Recorder(_redirect_once(PUBLIC_HOST, final_url))
        _install_transport(monkeypatch, recorder)

        findings = await SecurityScanner()._check_redirect_chain(f"https://{PUBLIC_HOST}/")

        assert [f["check_id"] for f in findings] == []
        assert recorder.urls == [f"https://{PUBLIC_HOST}/", final_url]
