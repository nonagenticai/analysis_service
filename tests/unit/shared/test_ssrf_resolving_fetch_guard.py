"""The fetch guards must RESOLVE, not just parse.

The defect these tests pin
--------------------------
Every *fetch* path in this service screened its target with a structural,
no-DNS predicate (``_is_ssrf_safe``). A structural check inspects the URL
*string*, so the cheapest attack in existence walks straight through it::

    http://cdn.attacker.example/logo.png     A -> 100.64.0.5

``cdn.attacker.example`` is a perfectly ordinary public name. Nothing about the
string is suspicious. The ``A`` record -- which the attacker owns and can change
at will -- points at worker4, the fleet's bare-metal production Postgres. The
per-hop redirect revalidation added earlier did not help, because re-applying a
name-blind predicate on every hop just re-applies the blindness.

So the decisive assertion in this file is never "a bad-looking URL is rejected".
Every literal-IP vector already passed before this change and would pass again
if the fix were reverted. The decisive assertion is:

    a hostname that is structurally unremarkable and RESOLVES into the estate
    must be REJECTED.

Every test below drives the resolver through ``tests/dns_stub`` (autouse from
``tests/conftest.py``). No test in this file makes a real outbound connection or
a real DNS query.

The control
-----------
``TestPublicHostsStillAllowed`` is the false-positive half. A guard that blocks
everything satisfies every rejection assertion in this file and is useless, so
the same hosts, resolved publicly, must come back allowed -- through the same
call, in the same file.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from unittest.mock import AsyncMock

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.brand.security import (
    BrandSsrfBlockedError,
    ensure_public_url_resolved,
    get_guarded,
)
from src.domains.critique.services.scanners.security_scanner import (
    _is_ssrf_safe,
    _is_ssrf_safe_resolved,
    _is_ssrf_safe_resolved_blocking,
)
from src.domains.critique.services.scanners.seo import internal_linking_live
from src.shared.security.url_validator import (
    validate_fetch_url,
    validate_fetch_url_blocking,
)
from tests.dns_stub import PUBLIC_ANSWER, TAILNET_ANSWER, DnsStub

# A name with nothing wrong with it. Structurally it is indistinguishable from
# any CDN hostname, which is the entire point: the string carries no signal.
ATTACKER_HOST = "cdn.attacker.example"
ATTACKER_URL = f"http://{ATTACKER_HOST}/logo.png"

# Where an attacker-controlled A record can point. Each of these is a real
# address class in this estate, and NONE of them appears in the URL string.
INTERNAL_ANSWERS = [
    pytest.param(TAILNET_ANSWER, id="cgnat-tailnet-worker4-postgres"),
    pytest.param("100.64.0.6", id="cgnat-tailnet-registry"),
    pytest.param("10.0.0.1", id="rfc1918-10"),
    pytest.param("192.168.1.119", id="rfc1918-192"),
    pytest.param("172.16.4.4", id="rfc1918-172"),
    pytest.param("169.254.169.254", id="cloud-metadata-link-local"),
    pytest.param("127.0.0.1", id="loopback"),
    pytest.param("::1", id="loopback-ipv6"),
    pytest.param("::ffff:100.64.0.1", id="ipv4-mapped-ipv6-tailnet"),
    pytest.param("::ffff:10.0.0.1", id="ipv4-mapped-ipv6-rfc1918"),
]


# --------------------------------------------------------------------------- #
# The shared predicate
# --------------------------------------------------------------------------- #
class TestStructuralScreenIsBlindOnPurpose:
    """Characterise the OLD behaviour, so the fix has something to be measured against.

    These are not regression guards -- they assert the hole. They exist so that
    "the resolving guard rejects it" below cannot be misread as "the URL looked
    bad": the very same URL passes the structural screen, in this same file,
    under the same resolver.
    """

    @pytest.mark.parametrize("answer", INTERNAL_ANSWERS)
    def test_structural_screen_admits_the_attacker_url(
        self, answer: str, dns_stub: DnsStub
    ) -> None:
        dns_stub.map(ATTACKER_HOST, answer)
        assert _is_ssrf_safe(ATTACKER_URL) is True


class TestResolvingScreenRejectsInternalAnswers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", INTERNAL_ANSWERS)
    async def test_async_predicate(self, answer: str, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, answer)
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False

    @pytest.mark.parametrize("answer", INTERNAL_ANSWERS)
    def test_blocking_predicate(self, answer: str, dns_stub: DnsStub) -> None:
        """The TLS check's twin must agree, or the two entry points diverge."""
        dns_stub.map(ATTACKER_HOST, answer)
        assert _is_ssrf_safe_resolved_blocking(ATTACKER_URL) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", INTERNAL_ANSWERS)
    async def test_validator_raises(self, answer: str, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, answer)
        with pytest.raises(ValueError, match="resolves to private IP"):
            await validate_fetch_url(ATTACKER_URL)


class TestMultiAnswerRecords:
    """One public address in the answer set must not launder the rest.

    ``getaddrinfo`` returns every address a name carries and promises nothing
    about their order, so a guard that inspects ``resolved[0]`` is defeated by
    listing the public address first. Both orders are asserted; a first-answer
    guard passes exactly one of them.
    """

    @pytest.mark.asyncio
    async def test_internal_address_second(self, dns_stub: DnsStub) -> None:
        dns_stub.map_many(ATTACKER_HOST, [PUBLIC_ANSWER, TAILNET_ANSWER])
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False

    @pytest.mark.asyncio
    async def test_internal_address_first(self, dns_stub: DnsStub) -> None:
        dns_stub.map_many(ATTACKER_HOST, [TAILNET_ANSWER, PUBLIC_ANSWER])
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False

    @pytest.mark.asyncio
    async def test_internal_address_buried_mid_answer(self, dns_stub: DnsStub) -> None:
        dns_stub.map_many(
            ATTACKER_HOST, [PUBLIC_ANSWER, "93.184.216.35", TAILNET_ANSWER, "93.184.216.36"]
        )
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False

    @pytest.mark.asyncio
    async def test_all_public_answers_allowed(self, dns_stub: DnsStub) -> None:
        """The control for this class: a genuine multi-A CDN must still work."""
        dns_stub.map_many(ATTACKER_HOST, [PUBLIC_ANSWER, "93.184.216.35", "93.184.216.36"])
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is True


class TestFailsClosed:
    @pytest.mark.asyncio
    async def test_unresolvable_name_is_refused(self, dns_stub: DnsStub) -> None:
        dns_stub.fail(ATTACKER_HOST)
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False


# --------------------------------------------------------------------------- #
# Requirement 3: the lookup must not run on the event loop
# --------------------------------------------------------------------------- #
class TestResolutionIsOffTheEventLoop:
    """``getaddrinfo`` blocks, and these paths fan out over many sub-resources.

    A synchronous lookup inside the loop stalls every other coroutine in the
    process for the duration of the query -- which an attacker controls, by
    simply not answering.
    """

    @pytest.mark.asyncio
    async def test_lookup_runs_on_a_worker_thread(self, dns_stub: DnsStub) -> None:
        lookups: list[tuple[str, int]] = []
        dns_stub.observe(lambda host, thread: lookups.append((host, thread)))

        await validate_fetch_url("https://public.example/page")

        assert lookups, "the resolver was never consulted -- the guard did not resolve at all"
        loop_thread = threading.get_ident()
        assert all(thread != loop_thread for _host, thread in lookups), (
            f"getaddrinfo ran on the event loop thread ({loop_thread}); "
            f"it must be dispatched off-loop, saw {lookups}"
        )

    @pytest.mark.asyncio
    async def test_ip_literal_never_touches_the_resolver(self, dns_stub: DnsStub) -> None:
        """An IP literal is fully decided structurally, so it must cost no lookup."""
        lookups: list[tuple[str, int]] = []
        dns_stub.observe(lambda host, thread: lookups.append((host, thread)))

        with pytest.raises(ValueError, match="private IP"):
            await validate_fetch_url("http://169.254.169.254/latest/meta-data/")
        assert lookups == []

    @pytest.mark.asyncio
    async def test_fanout_over_one_host_costs_one_lookup(self, dns_stub: DnsStub) -> None:
        """The verdict cache is what makes resolving affordable on a fan-out path.

        A brand extraction pulls a page, its manifest, its icons and up to ten
        stylesheets -- all from one host. Without the cache that is one DNS
        query per sub-resource.
        """
        lookups: list[tuple[str, int]] = []
        dns_stub.observe(lambda host, thread: lookups.append((host, thread)))

        await asyncio.gather(
            *(validate_fetch_url(f"https://public.example/style-{i}.css") for i in range(10))
        )
        assert len(lookups) == 1, f"expected one memoised lookup, got {len(lookups)}"

    @pytest.mark.asyncio
    async def test_cache_does_not_launder_a_blocked_verdict(self, dns_stub: DnsStub) -> None:
        """A cached *deny* must stay a deny; a cached *allow* must not cover a new host."""
        dns_stub.map(ATTACKER_HOST, TAILNET_ANSWER)
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False
        # Second call served from cache -- still denied, not "already seen, fine".
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is False
        # A different host is decided on its own merits, not the cached entry.
        assert await _is_ssrf_safe_resolved("https://public.example/ok") is True


# --------------------------------------------------------------------------- #
# The brand sink -- the reported entry point
# --------------------------------------------------------------------------- #
def _response(
    status: int, url: str, *, headers: dict[str, str] | None = None, body: bytes = b""
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        content=body,
        request=httpx.Request("GET", url),
    )


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _recording_client(
    handler: Callable[[httpx.Request], httpx.Response], seen: list[str]
) -> httpx.AsyncClient:
    """A hermetic client recording every URL the transport is actually asked for.

    ``follow_redirects=True`` is deliberate and is the opposite of what the
    services construct: the assertion below passes only because the guard
    refuses the hop itself, never because the client declined to follow it.
    """

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(record), follow_redirects=True)


class TestBrandFetchSink:
    """``POST /api/brand/logo/download`` and the css/manifest/logo sub-fetches."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", INTERNAL_ANSWERS)
    async def test_ensure_public_url_resolved_rejects(self, answer: str, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, answer)
        with pytest.raises(BrandSsrfBlockedError):
            await ensure_public_url_resolved(ATTACKER_URL)

    @pytest.mark.asyncio
    async def test_get_guarded_never_issues_the_request(self, dns_stub: DnsStub) -> None:
        """The side effect, not just the exception: no request may leave."""
        dns_stub.map(ATTACKER_HOST, TAILNET_ANSWER)
        client = AsyncMock()
        with pytest.raises(BrandSsrfBlockedError):
            await get_guarded(client, ATTACKER_URL)
        client.get.assert_not_called()
        client.stream.assert_not_called()


class TestRedirectLocationIsResolved:
    """A ``302`` whose ``Location`` names a host that RESOLVES into the estate.

    Both hops are structurally impeccable public names. Only the second one's
    ``A`` record is hostile, so nothing but resolution can catch this.
    """

    @pytest.mark.asyncio
    async def test_second_hop_is_not_taken(self, dns_stub: DnsStub) -> None:
        dns_stub.map("start.example", PUBLIC_ANSWER)
        dns_stub.map("redirect-target.example", TAILNET_ANSWER)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(
                302,
                str(request.url),
                headers={"location": "https://redirect-target.example/admin"},
            )

        async with _recording_client(handler, seen) as client:
            with pytest.raises(BrandSsrfBlockedError):
                await get_guarded(client, "https://start.example/page")

        assert seen == ["https://start.example/page"], (
            f"the redirect into the estate was followed: {seen}"
        )

    @pytest.mark.asyncio
    async def test_public_redirect_is_still_followed(self, dns_stub: DnsStub) -> None:
        """Control: the guard must not break ordinary redirects."""
        dns_stub.map("start.example", PUBLIC_ANSWER)
        dns_stub.map("elsewhere.example", PUBLIC_ANSWER)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "start.example":
                return _response(
                    302, str(request.url), headers={"location": "https://elsewhere.example/ok"}
                )
            return _response(200, str(request.url), body=b"ok")

        async with _recording_client(handler, seen) as client:
            response = await get_guarded(client, "https://start.example/page")

        assert response.status_code == 200
        assert seen == ["https://start.example/page", "https://elsewhere.example/ok"]


# --------------------------------------------------------------------------- #
# Sitemap <loc>
# --------------------------------------------------------------------------- #
_SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://evil-child.example/sitemap-2.xml</loc></sitemap>
</sitemapindex>
"""

_SITEMAP_URLSET = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://evil-child.example/secret</loc></url>
</urlset>
"""


class TestSitemapLocIsResolved:
    """A sitemap index ``<loc>`` is an arbitrary attacker-chosen host.

    ``_parse_sitemap`` recurses one level into a ``<sitemapindex>``, and the
    child ``<loc>`` is NOT constrained to the origin -- it is fetched. So the
    ``<loc>`` value is a first-class SSRF sink, and it was screened with the
    same blind structural predicate as everything else.
    """

    @pytest.mark.asyncio
    async def test_child_sitemap_resolving_internal_is_not_fetched(self, dns_stub: DnsStub) -> None:
        dns_stub.map("site.example", PUBLIC_ANSWER)
        dns_stub.map("evil-child.example", TAILNET_ANSWER)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = _SITEMAP_INDEX if "sitemap.xml" in str(request.url) else _SITEMAP_URLSET
            return _response(200, str(request.url), body=body.encode())

        async with _recording_client(handler, seen) as client:
            locs = await internal_linking_live._parse_sitemap(
                client, "https://site.example/sitemap.xml", depth=0
            )

        assert seen == ["https://site.example/sitemap.xml"], (
            f"the child <loc> pointing into the estate was fetched: {seen}"
        )
        assert locs == []

    @pytest.mark.asyncio
    async def test_public_child_sitemap_is_still_fetched(self, dns_stub: DnsStub) -> None:
        """Control: sitemap-index recursion must keep working for public children."""
        dns_stub.map("site.example", PUBLIC_ANSWER)
        dns_stub.map("evil-child.example", PUBLIC_ANSWER)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = _SITEMAP_INDEX if "sitemap.xml" in str(request.url) else _SITEMAP_URLSET
            return _response(200, str(request.url), body=body.encode())

        async with _recording_client(handler, seen) as client:
            locs = await internal_linking_live._parse_sitemap(
                client, "https://site.example/sitemap.xml", depth=0
            )

        assert seen == [
            "https://site.example/sitemap.xml",
            "https://evil-child.example/sitemap-2.xml",
        ]
        assert locs == ["https://evil-child.example/secret"]


# --------------------------------------------------------------------------- #
# The false-positive control
# --------------------------------------------------------------------------- #
class TestPublicHostsStillAllowed:
    """A guard that blocks everything satisfies every assertion above.

    Same hosts, same calls, public answers -- all must be allowed.
    """

    @pytest.mark.asyncio
    async def test_predicate_allows_public(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        assert await _is_ssrf_safe_resolved(ATTACKER_URL) is True

    def test_blocking_predicate_allows_public(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        assert _is_ssrf_safe_resolved_blocking(ATTACKER_URL) is True

    @pytest.mark.asyncio
    async def test_validator_returns_the_url(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        assert await validate_fetch_url(ATTACKER_URL) == ATTACKER_URL

    def test_blocking_validator_returns_the_url(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        assert validate_fetch_url_blocking(ATTACKER_URL) == ATTACKER_URL

    @pytest.mark.asyncio
    async def test_brand_guard_allows_public(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        await ensure_public_url_resolved(ATTACKER_URL)  # must not raise

    @pytest.mark.asyncio
    async def test_brand_fetch_succeeds_for_public(self, dns_stub: DnsStub) -> None:
        dns_stub.map(ATTACKER_HOST, PUBLIC_ANSWER)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            return _response(200, str(request.url), body=b"<html></html>")

        async with _recording_client(handler, seen) as client:
            response = await get_guarded(client, ATTACKER_URL)

        assert response.status_code == 200
        assert seen == [ATTACKER_URL]


# --------------------------------------------------------------------------- #
# The SEO scanners share the predicate -- prove the blast radius landed
# --------------------------------------------------------------------------- #
class TestSeoScannersResolve:
    @pytest.mark.asyncio
    async def test_geo_live_skips_a_name_resolving_internal(self, dns_stub: DnsStub) -> None:
        from src.domains.critique.services.scanners.seo import geo

        dns_stub.map(ATTACKER_HOST, TAILNET_ANSWER)
        assert await geo.scan_geo_live(f"https://{ATTACKER_HOST}/") == []

    @pytest.mark.asyncio
    async def test_technical_live_skips_a_name_resolving_internal(self, dns_stub: DnsStub) -> None:
        from src.domains.critique.services.scanners.seo import technical

        dns_stub.map(ATTACKER_HOST, TAILNET_ANSWER)
        assert await technical.scan_live(f"https://{ATTACKER_HOST}/") == []

    @pytest.mark.asyncio
    async def test_security_scanner_entry_blocks_a_name_resolving_internal(
        self, dns_stub: DnsStub
    ) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        dns_stub.map(ATTACKER_HOST, TAILNET_ANSWER)
        findings = await SecurityScanner().scan(
            ATTACKER_URL, html="<html></html>", soup=BeautifulSoup("<html></html>", "html.parser")
        )
        assert [f["check_id"] for f in findings] == ["sec-ssrf-blocked"]
