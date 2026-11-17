"""Unit tests for the opt-in cross-page internal-linking scanner (mocked transport).

No live network: the crawl runs against an ``httpx.MockTransport`` and the SSRF
gate is patched. Asserts the three ``seo-linking-*`` check_ids fire (orphan /
broken / hub), that they map to the ``internal_linking`` scoring group, that the
shared ``home_soup`` is never mutated by the crawl, and that no redirect hop or
sitemap entry pointing into the estate is ever dereferenced.

WHY A REAL CLIENT OVER A MOCK TRANSPORT
---------------------------------------
These tests used to patch ``httpx.AsyncClient`` with hand-rolled doubles exposing
only ``get`` and ``head``. That double was *narrower than the real client*, and
the gap became load-bearing the moment ``guarded_get`` started streaming under a
byte cap: the capped path calls ``client.stream(...)`` and uses it as an async
context manager, which no ``get``/``head``-only double -- and no bare
``AsyncMock``, whose ``stream()`` returns a coroutine rather than a context
manager -- can model. The doubles did not fail informatively; they raised
``AttributeError``/``TypeError`` from inside ``http_guard``, which says nothing
about the scanner under test.

Substituting the transport instead of the client keeps every assertion these
tests already made and removes the class of failure entirely: the code builds its
own real ``httpx.AsyncClient`` with its own arguments, so ``stream``, ``get`` and
``head`` all behave exactly as in production and only the socket is replaced.

It also *strengthens* the redirect assertions. The old doubles asserted the
``follow_redirects=False`` **argument**; a real client asserts the
**consequence** -- if that flag ever regressed to ``True``, httpx itself would
walk the chain, the internal host would appear in ``requested``, and the test
would fail on what actually happened rather than on how it was spelled.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo import internal_linking_live
from src.domains.critique.services.scanners.seo.internal_linking_live import (
    scan_internal_linking_live,
)
from src.domains.critique.services.scanners.seo.scoring import group_of
from tests.dns_stub import TAILNET_ANSWER

_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.test/about</loc></url>
<url><loc>https://example.test/blog</loc></url>
<url><loc>https://example.test/popular</loc></url>
<url><loc>https://example.test/broken</loc></url>
<url><loc>https://example.test/orphan</loc></url>
</urlset>
"""

_HOME_HTML = """
<html><body><nav>
<a href="/about">About</a>
<a href="/blog">Blog</a>
<a href="/popular">Popular</a>
<a href="/broken">Broken</a>
<a href="https://external.test/x">External</a>
</nav></body></html>
"""

# Two interior pages both link to /popular -> inbound >= 2 -> hub.
_LINKS_POPULAR = '<html><body><a href="/popular">Popular</a></body></html>'
_EMPTY = "<html><body><p>no links</p></body></html>"

_GET_ROUTES = {
    "https://example.test/sitemap.xml": (200, _SITEMAP),
    "https://example.test/about": (200, _LINKS_POPULAR),
    "https://example.test/blog": (200, _LINKS_POPULAR),
    "https://example.test/popular": (200, _EMPTY),
    "https://example.test/orphan": (200, _EMPTY),
}
_HEAD_ROUTES = {
    "https://example.test/about": 200,
    "https://example.test/blog": 200,
    "https://example.test/popular": 200,
    "https://example.test/orphan": 200,
    "https://example.test/broken": 404,
}


_Handler = Callable[[httpx.Request], httpx.Response]


def _client_factory(handler: _Handler) -> functools.partial[httpx.AsyncClient]:
    """Build the ``httpx.AsyncClient`` replacement to patch into the scanner.

    The scanner still constructs the client itself, with its own timeout and
    ``follow_redirects=False``; only the transport is ours. That is what keeps
    ``stream`` / ``get`` / ``head`` behaving as they do in production.

    Args:
        handler: Responds to each request.

    Returns:
        A callable accepting the scanner's own ``AsyncClient`` keyword arguments.
    """
    return functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))


def _routed_handler(
    get_routes: dict[str, tuple[int, str]],
    head_routes: dict[str, int],
    recorder: list[str] | None = None,
) -> _Handler:
    """Serve a static routing table, optionally recording every URL requested.

    Args:
        get_routes: ``url -> (status, body)`` for ``GET``.
        head_routes: ``url -> status`` for ``HEAD``.
        recorder: If given, every requested URL is appended to it. Asserting on
            the transport rather than on the findings is deliberate: a scan that
            silently failed also produces no findings.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if recorder is not None:
            recorder.append(url)
        if request.method == "HEAD":
            return httpx.Response(head_routes.get(url, 404))
        status, text = get_routes.get(url, (404, ""))
        return httpx.Response(status, text=text)

    return _handle


class TestScanInternalLinkingLive:
    async def test_orphan_broken_hub_fire_and_soup_not_mutated(self):
        home_soup = BeautifulSoup(_HOME_HTML, "lxml")
        before = str(home_soup)

        # The SSRF guard is deliberately NOT mocked -- it resolves now, and the
        # hermetic stub answers ``example.test`` publicly. Asserting through the
        # real predicate is what keeps this test honest about the crawl actually
        # being allowed to reach those pages.
        with patch.object(
            internal_linking_live.httpx,
            "AsyncClient",
            _client_factory(_routed_handler(_GET_ROUTES, _HEAD_ROUTES)),
        ):
            findings = await scan_internal_linking_live(
                "https://example.test/", home_soup=home_soup
            )

        ids = {f["check_id"] for f in findings}
        assert "seo-linking-orphan" in ids
        assert "seo-linking-broken" in ids
        assert "seo-linking-hub" in ids

        broken = next(f for f in findings if f["check_id"] == "seo-linking-broken")
        assert broken["severity"] == "high"
        assert "https://example.test/broken" in broken["url"]

        orphan = next(f for f in findings if f["check_id"] == "seo-linking-orphan")
        assert orphan["severity"] == "medium"
        assert "https://example.test/orphan" in orphan["url"]

        hub = next(f for f in findings if f["check_id"] == "seo-linking-hub")
        assert hub["severity"] == "info"
        assert "https://example.test/popular" in hub["url"]

        # The shared home soup must never be mutated by the crawl.
        assert str(home_soup) == before

    async def test_check_ids_map_to_internal_linking_group(self):
        assert group_of("seo-linking-orphan") == "internal_linking"
        assert group_of("seo-linking-broken") == "internal_linking"
        assert group_of("seo-linking-hub") == "internal_linking"

    async def test_ssrf_unsafe_url_returns_no_findings(self):
        """Real guard, no mock: a loopback literal is refused structurally."""
        findings = await scan_internal_linking_live("http://127.0.0.1/")
        assert findings == []

    async def test_name_resolving_into_the_estate_returns_no_findings(self, dns_stub):
        """The case the structural screen missed: a public name, hostile A record."""
        dns_stub.map("cdn.attacker.example", TAILNET_ANSWER)
        findings = await scan_internal_linking_live("https://cdn.attacker.example/")
        assert findings == []


# A sitemap index whose children point at an internal IP (SSRF) and a same-origin
# child. The internal child must NEVER be fetched.
_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>http://169.254.169.254/sitemap.xml</loc></sitemap>
<sitemap><loc>https://example.test/sitemap-pages.xml</loc></sitemap>
</sitemapindex>
"""
_CHILD = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.test/page-a</loc></url>
</urlset>
"""


#: Routes for the sitemap-index case. The ``169.254.x`` child is deliberately
#: absent: it must never be requested, and a handler that could answer it would
#: let the test pass on a scanner that fetched it anyway.
_INDEX_GET_ROUTES = {
    "https://example.test/sitemap.xml": (200, _INDEX),
    "https://example.test/sitemap-pages.xml": (200, _CHILD),
    "https://example.test/": (200, _HOME_HTML),
    "https://example.test/page-a": (200, _EMPTY),
}


def _recording_handler(recorder: list[str]) -> _Handler:
    """Sitemap-index handler that records every URL a connection was opened to.

    Args:
        recorder: Collects each requested URL.

    Returns:
        A ``MockTransport``-compatible handler.
    """
    return _routed_handler(
        _INDEX_GET_ROUTES,
        dict.fromkeys(_INDEX_GET_ROUTES, 200),
        recorder=recorder,
    )


def _redirecting_handler(recorder: list[str], internal: str) -> _Handler:
    """Answer every URL but the home page with a ``302`` into the estate.

    Models what the entry-only screen could not see: every crawl target is
    public and passes ``_is_ssrf_safe``, and the internal host appears only in
    the ``Location`` header.

    Args:
        recorder: Collects each requested URL.
        internal: The internal target the redirects point at.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        recorder.append(url)
        if url == "https://example.test/":
            return httpx.Response(200, text=_HOME_HTML)
        return httpx.Response(302, headers={"location": internal})

    return _handle


_INTERNAL = "http://169.254.169.254/latest/meta-data/"


class TestRedirectHopsAreRescreened:
    async def test_redirect_into_link_local_is_never_dereferenced(self):
        """A 302 to 169.254.169.254 must not be followed.

        The entry URL and every crawl target here are public, so the pre-existing
        entry check passes them all. Only a per-hop screen stops the chain.

        Because the client is real, this also covers the ``follow_redirects``
        flag by consequence rather than by argument: were it to regress to
        ``True``, httpx would walk the chain itself and the link-local host would
        appear in ``requested``.
        """
        requested: list[str] = []
        with patch.object(
            internal_linking_live.httpx,
            "AsyncClient",
            _client_factory(_redirecting_handler(requested, _INTERNAL)),
        ):
            await scan_internal_linking_live("https://example.test/")

        assert requested, "the crawl never issued a request at all"
        assert not any("169.254.169.254" in u for u in requested), (
            f"a redirect hop into link-local space was dereferenced: {requested}"
        )


class TestSitemapSsrfGating:
    async def test_internal_child_sitemap_is_never_fetched(self):
        # Real _is_ssrf_safe (NOT patched) must block the 169.254.x child sitemap
        # while the same-origin child is followed normally.
        requested: list[str] = []
        with patch.object(
            internal_linking_live.httpx,
            "AsyncClient",
            _client_factory(_recording_handler(requested)),
        ):
            await scan_internal_linking_live("https://example.test/")
        assert not any("169.254.169.254" in u for u in requested), (
            "SSRF-unsafe child sitemap was fetched"
        )
        # the safe same-origin child sitemap WAS followed
        assert "https://example.test/sitemap-pages.xml" in requested
