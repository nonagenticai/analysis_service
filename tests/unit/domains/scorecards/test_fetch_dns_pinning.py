"""``fetch_html_capped`` must PIN every hop to the IP it just validated.

The defect these tests pin
--------------------------
``fetch.py`` re-validated every redirect hop with a *resolving* screen and was
proud of it in its docstring. Re-validating is necessary; it is not sufficient.
The screen returned a bare ``bool`` and threw the resolved address away, and
``client.stream("GET", current)`` was then handed the **hostname** — so httpx
performed its own, second, independent DNS lookup at connect time::

    validate:  rebind.example.com -> 8.8.8.8      (public — allowed)
    connect:   rebind.example.com -> 10.0.0.7     (attacker flipped the record)

Every hop was checked and every hop was exploitable. Checking a name twice does
nothing when the two checks are two different lookups; only pinning the
*validated address* into the connection closes the window.

How the second lookup is observed
---------------------------------
``tests/dns_stub`` is the *validation-time* resolver — it backs
``url_validator``/``safe_fetch`` and is deliberately scoped to those two modules
so ``socket.getaddrinfo`` is never patched globally (psycopg and httpx keep the
real resolver). It therefore cannot express "and httpx resolved it differently".

:class:`_ConnectTimeResolver` supplies the missing half. It stands in for the
transport's own lookup: an IP **literal** in the request URL means the caller
pinned and no second lookup happens, so the literal *is* the address reached; a
**hostname** in the request URL means the transport must resolve it again, and
the flip table decides what it gets. The address every request actually reaches
is recorded, and that recording is the assertion.

No test here makes a real DNS query or opens a real socket.
"""

from __future__ import annotations

import functools
import ipaddress
from collections.abc import Callable

import httpx
import pytest

import src.domains.scorecards.fetch as fetch_mod
from src.domains.scorecards.fetch import fetch_html_capped
from tests.dns_stub import DnsStub

#: Validation-time answer for the rebinding host. ``8.8.8.8`` on purpose:
#: TEST-NET-3 (``203.0.113.0/24``) is in this fleet's ``_BLOCKED_NETWORKS`` and
#: ``100.64.0.0/10`` IS the tailnet here, so neither can play "public".
PUBLIC = "8.8.8.8"

#: A second, *different* public answer — proves a hop is pinned to its OWN
#: resolution rather than inheriting the previous hop's address.
OTHER_PUBLIC = "1.1.1.1"

#: What the attacker's authoritative server returns to the *connect-time*
#: lookup. RFC 1918 — the address the fetch must never reach.
PRIVATE = "10.0.0.7"


class _ConnectTimeResolver:
    """Stands in for the transport's own (second) DNS lookup.

    Attributes:
        flips: ``hostname -> address`` returned to the connect-time lookup,
            i.e. what the attacker flips the record to after validation.
        reached: The address each request actually connected to, in order.
        seen: ``(request_url_host, host_header, sni_hostname)`` per request.
    """

    def __init__(self, flips: dict[str, str]) -> None:
        """Store the flip table and start with an empty recording."""
        self.flips = flips
        self.reached: list[str] = []
        self.seen: list[tuple[str, str | None, str | None]] = []

    def _target(self, request: httpx.Request) -> str:
        """Return the address *request* lands on.

        A literal address in the request URL means the caller pinned: the
        transport has nothing left to resolve, so that literal is where the
        bytes go. A hostname means the transport resolves it independently —
        the TOCTOU window — and the flip table answers.
        """
        host = request.url.host
        try:
            return str(ipaddress.ip_address(host.strip("[]")))
        except ValueError:
            return self.flips.get(host, PUBLIC)

    def record(self, request: httpx.Request) -> str:
        """Record one request and return the address it reached."""
        target = self._target(request)
        self.reached.append(target)
        self.seen.append(
            (
                request.url.host,
                request.headers.get("host"),
                request.extensions.get("sni_hostname"),
            )
        )
        return target


def _install(
    monkeypatch: pytest.MonkeyPatch,
    resolver: _ConnectTimeResolver,
    responder: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Point ``fetch_html_capped``'s internal client at a recording transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        resolver.record(request)
        return responder(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        fetch_mod.httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


async def test_initial_hop_is_pinned_to_the_validated_address(
    monkeypatch: pytest.MonkeyPatch, dns_stub: DnsStub
) -> None:
    """The entry URL connects to the IP the guard validated, not a re-resolution."""
    dns_stub.map("rebind.example.com", PUBLIC)
    resolver = _ConnectTimeResolver({"rebind.example.com": PRIVATE})
    _install(monkeypatch, resolver, lambda _r: httpx.Response(200, text="<html>ok</html>"))

    await fetch_html_capped("https://rebind.example.com/", user_agent="t")

    assert resolver.reached == [PUBLIC], (
        f"fetch reached {resolver.reached} — the connect-time lookup re-resolved the "
        f"hostname and landed on the flipped private address instead of the "
        f"validated {PUBLIC}"
    )


async def test_pinned_request_preserves_host_header_and_sni(
    monkeypatch: pytest.MonkeyPatch, dns_stub: DnsStub
) -> None:
    """Pinning rewrites the connect target only — vhost + cert checks survive."""
    dns_stub.map("rebind.example.com", PUBLIC)
    resolver = _ConnectTimeResolver({"rebind.example.com": PRIVATE})
    _install(monkeypatch, resolver, lambda _r: httpx.Response(200, text="<html>ok</html>"))

    await fetch_html_capped("https://rebind.example.com/", user_agent="t")

    url_host, host_header, sni = resolver.seen[0]
    assert url_host == PUBLIC
    assert host_header == "rebind.example.com"
    assert sni == "rebind.example.com"


async def test_redirect_hop_is_pinned_too(
    monkeypatch: pytest.MonkeyPatch, dns_stub: DnsStub
) -> None:
    """Hop 2 is where the sibling repos' guards caught this — pin it as well."""
    dns_stub.map("start.example.com", PUBLIC)
    dns_stub.map("rebind.example.com", PUBLIC)
    resolver = _ConnectTimeResolver({"rebind.example.com": PRIVATE})

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "start.example.com":
            return httpx.Response(302, headers={"location": "https://rebind.example.com/page"})
        return httpx.Response(200, text="<html>final</html>")

    _install(monkeypatch, resolver, responder)

    await fetch_html_capped("https://start.example.com/", user_agent="t")

    assert PRIVATE not in resolver.reached, (
        f"redirect hop reached {resolver.reached} — hop 2 was re-validated but not "
        f"re-pinned, so httpx re-resolved it onto the private address"
    )
    assert resolver.reached == [PUBLIC, PUBLIC]


async def test_each_hop_pins_its_own_resolution(
    monkeypatch: pytest.MonkeyPatch, dns_stub: DnsStub
) -> None:
    """The previous hop's IP is never carried forward onto the next hostname."""
    dns_stub.map("start.example.com", PUBLIC)
    dns_stub.map("second.example.com", OTHER_PUBLIC)
    resolver = _ConnectTimeResolver({})

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers.get("host") == "start.example.com":
            return httpx.Response(302, headers={"location": "https://second.example.com/page"})
        return httpx.Response(200, text="<html>final</html>")

    _install(monkeypatch, resolver, responder)

    html = await fetch_html_capped("https://start.example.com/", user_agent="t")

    assert resolver.reached == [PUBLIC, OTHER_PUBLIC]
    assert "final" in html


async def test_relative_redirect_resolves_against_the_hostname_not_the_pin(
    monkeypatch: pytest.MonkeyPatch, dns_stub: DnsStub
) -> None:
    """A relative ``Location`` joins onto the real URL, never the pinned literal.

    Joining against ``response.url`` after pinning would produce
    ``https://8.8.8.8/next`` — silently converting every relative redirect into
    an IP-literal fetch that skips the hostname rules and breaks vhosts.
    """
    dns_stub.map("start.example.com", PUBLIC)
    resolver = _ConnectTimeResolver({})
    paths: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, text="<html>final</html>")

    _install(monkeypatch, resolver, responder)

    await fetch_html_capped("https://start.example.com/", user_agent="t")

    assert paths == ["/", "/next"]
    assert [h for _u, h, _s in resolver.seen] == ["start.example.com", "start.example.com"]
