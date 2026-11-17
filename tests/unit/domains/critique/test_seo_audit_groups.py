"""Unit tests for the PR2 SEO audit groups (eeat / schema / internal_linking / geo).

Pure deterministic checks over fixture HTML — no DB, no LLM. The one network
check (robots.txt AI-crawler access) is covered by testing its pure parser plus a
mocked-client path. Assertions key on specific ``check_id``s.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from unittest.mock import patch

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo import geo
from src.domains.critique.services.scanners.seo.eeat import check_eeat
from src.domains.critique.services.scanners.seo.geo import (
    _ai_bots_blocked,
    check_geo,
    scan_geo_live,
)
from src.domains.critique.services.scanners.seo.internal_linking import check_internal_linking
from src.domains.critique.services.scanners.seo.schema_coverage import check_schema_coverage
from src.domains.critique.services.scanners.seo.signals import (
    extract_content_date,
    extract_schema_types,
    has_author_entity,
)
from tests.dns_stub import TAILNET_ANSWER

# ~230-word content body reused across groups.
_BODY = " ".join(
    [
        "Cloud backup keeps your files safe across devices and disasters.",
        "This guide explains how modern cloud backup works and why it matters.",
        "A good backup runs automatically, encrypts data, and restores quickly.",
    ]
    * 8
)


def _page(head: str = "", body_extra: str = "", *, main: str | None = None) -> BeautifulSoup:
    content = (
        main
        if main is not None
        else f"<main><h1>Cloud backup guide</h1><p>{_BODY}</p>{body_extra}</main>"
    )
    html = f"<html><head>{head}</head><body>{content}</body></html>"
    return BeautifulSoup(html, "lxml")


def _ids(findings: list[dict]) -> set[str | None]:
    return {f.get("check_id") for f in findings}


class TestEeat:
    def test_bare_page_flags_trust_and_authority_gaps(self):
        ids = _ids(
            check_eeat(_page(), "http://example.com/x")
        )  # http, no privacy/contact/author/date
        assert "seo-eeat-no-https" in ids
        assert "seo-eeat-no-privacy-link" in ids
        assert "seo-eeat-no-contact" in ids
        assert "seo-eeat-no-author" in ids
        assert "seo-eeat-no-citations" in ids
        assert "seo-eeat-no-date" in ids

    def test_well_signalled_page_is_clean(self):
        head = (
            '<meta name="author" content="Jane Doe">'
            '<meta property="article:modified_time" content="2026-06-20T00:00:00Z">'
        )
        body_extra = (
            '<a href="/privacy">Privacy</a><a href="/contact">Contact</a>'
            '<a href="https://nist.gov/backup">source</a>'
        )
        ids = _ids(check_eeat(_page(head, body_extra), "https://example.com/x"))
        assert ids == set() or (
            None in ids
            and not {
                "seo-eeat-no-https",
                "seo-eeat-no-privacy-link",
                "seo-eeat-no-contact",
                "seo-eeat-no-author",
                "seo-eeat-no-citations",
                "seo-eeat-no-date",
            }
            & ids
        )


class TestSchemaCoverage:
    def test_no_org_schema_flagged(self):
        ids = _ids(check_schema_coverage(_page(), "https://example.com/x"))
        assert "seo-schema-no-organization" in ids
        assert "seo-schema-coverage-gap" in ids

    def test_org_schema_satisfies_org_check(self):
        head = '<script type="application/ld+json">{"@type":"Organization","name":"Acme","url":"https://a.co"}</script>'
        ids = _ids(check_schema_coverage(_page(head), "https://example.com/x"))
        assert "seo-schema-no-organization" not in ids

    def test_incomplete_article_flagged(self):
        head = '<script type="application/ld+json">{"@type":"Article","headline":"Hi"}</script>'
        ids = _ids(check_schema_coverage(_page(head), "https://example.com/x"))
        assert "seo-schema-article-incomplete" in ids


class TestInternalLinking:
    def test_generic_anchors_flagged(self):
        extra = '<a href="/a">click here</a><a href="/b">read more</a><a href="/c">learn more</a>'
        ids = _ids(check_internal_linking(_page(body_extra=extra), "https://example.com/x"))
        assert "seo-linking-generic-anchors" in ids

    def test_no_internal_links_flagged(self):
        ids = _ids(check_internal_linking(_page(), "https://example.com/x"))
        assert "seo-linking-no-internal" in ids

    def test_descriptive_internal_link_clean(self):
        extra = '<a href="/cloud-backup-pricing">See cloud backup pricing plans</a>'
        ids = _ids(check_internal_linking(_page(body_extra=extra), "https://example.com/x"))
        assert "seo-linking-no-internal" not in ids
        assert "seo-linking-generic-anchors" not in ids


class TestGeoSync:
    def test_no_author_entity_flagged(self):
        ids = _ids(check_geo(_page(), "https://example.com/x"))
        assert "seo-geo-no-author-entity" in ids

    def test_stale_content_flagged(self):
        head = '<meta property="article:modified_time" content="2019-01-01T00:00:00Z">'
        ids = _ids(check_geo(_page(head), "https://example.com/x"))
        assert "seo-geo-stale-content" in ids

    def test_person_entity_satisfies(self):
        head = '<script type="application/ld+json">{"@type":"Person","name":"Jane Doe"}</script>'
        ids = _ids(check_geo(_page(head), "https://example.com/x"))
        assert "seo-geo-no-author-entity" not in ids


class TestSignals:
    def test_extract_schema_types_flattens_graph(self):
        soup = _page(
            '<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":"WebSite"}]}</script>'
        )
        assert extract_schema_types(soup) == {"Organization", "WebSite"}

    def test_author_entity_from_nested_author(self):
        soup = _page(
            '<script type="application/ld+json">{"@type":"Article","author":{"@type":"Person","name":"J"}}</script>'
        )
        assert has_author_entity(soup) is True

    def test_extract_content_date_picks_most_recent(self):
        soup = _page(
            '<script type="application/ld+json">{"datePublished":"2020-01-01","dateModified":"2024-05-05"}</script>'
        )
        d = extract_content_date(soup)
        assert d is not None and d.year == 2024 and d.month == 5


class TestRobotsAiParser:
    def test_specific_bot_disallow_root(self):
        robots = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow: /admin\n"
        assert "GPTBot" in _ai_bots_blocked(robots)
        assert "ClaudeBot" not in _ai_bots_blocked(robots)

    def test_wildcard_disallow_blocks_all_ai(self):
        robots = "User-agent: *\nDisallow: /\n"
        blocked = _ai_bots_blocked(robots)
        assert "GPTBot" in blocked and "ClaudeBot" in blocked

    def test_open_robots_blocks_none(self):
        robots = "User-agent: *\nDisallow:\n"
        assert _ai_bots_blocked(robots) == []


def _geo_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> functools.partial[httpx.AsyncClient]:
    """Build the ``httpx.AsyncClient`` replacement ``scan_geo_live`` will construct.

    A real client over a mock transport, not a ``get``-only double or a bare
    ``AsyncMock``. ``guarded_get`` caps the body by default, and the capped path
    goes through ``client.stream(...)`` used as an async context manager --
    something an ``AsyncMock`` cannot model (its ``stream()`` returns a
    coroutine) and a hand-rolled ``get``-only double does not have at all. Both
    fail inside ``http_guard`` with an error that says nothing about the scanner
    under test. Replacing only the transport keeps ``stream``/``get``/``head``
    behaving exactly as they do in production.

    Args:
        handler: Responds to each request.

    Returns:
        A callable accepting ``scan_geo_live``'s own ``AsyncClient`` kwargs.
    """
    return functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))


class TestGeoLive:
    async def test_blocked_ai_crawler_flagged(self):
        routes = {
            "https://example.com/robots.txt": "User-agent: GPTBot\nDisallow: /\n",
            "https://example.com/llms.txt": "ok",
        }

        def _handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=routes[str(request.url)])

        # The SSRF guard is deliberately NOT mocked. It resolves now, and
        # ``example.com`` is answered publicly by the hermetic stub, so this
        # exercises the real predicate instead of a stand-in that cannot fail
        # when the predicate's signature or semantics change under it.
        with patch.object(geo.httpx, "AsyncClient", _geo_client_factory(_handle)):
            findings = await scan_geo_live("https://example.com/page")
        assert "seo-geo-ai-crawler-blocked" in _ids(findings)

    async def test_ssrf_unsafe_returns_empty(self):
        """Real guard, no mock: an IP literal is refused before any resolution."""
        assert await scan_geo_live("http://169.254.169.254/") == []

    async def test_name_resolving_into_the_estate_returns_empty(self, dns_stub):
        """The case the structural screen missed: a public name, hostile A record."""
        dns_stub.map("cdn.attacker.example", TAILNET_ANSWER)
        assert await scan_geo_live("https://cdn.attacker.example/") == []

    async def test_redirect_into_link_local_is_never_dereferenced(self):
        """A public origin that ``302``s into the estate must not be followed.

        The entry screen passes ``https://example.com/page`` — as it should, it
        is public. The internal host only appears in the ``Location`` header, so
        nothing but a per-hop screen can stop it. Asserted on the transport
        (which URLs a request was issued for), not on the findings: an empty
        finding list is also what a silently-broken scan returns.
        """
        requested: list[str] = []
        internal = "http://169.254.169.254/latest/meta-data/"

        def _handle(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(302, headers={"location": internal})

        with patch.object(geo.httpx, "AsyncClient", _geo_client_factory(_handle)):
            findings = await scan_geo_live("https://example.com/page")

        assert requested == ["https://example.com/robots.txt"], requested
        assert not any("169.254.169.254" in u for u in requested)
        assert findings == []
