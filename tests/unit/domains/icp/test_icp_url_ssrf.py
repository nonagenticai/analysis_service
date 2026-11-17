"""RED-proof for the tenant-supplied ICP target URL as an SSRF sink.

WHAT WAS UNGUARDED
------------------
``ICPFromUrlRequest.url`` is a plain ``str`` with no scheme or host validation
at the schema layer, and the value flowed untouched from the request body into
the KAG scrape client's ``scrape_url(body.url)``. Every sibling tenant-URL route in this
service already screens its target — ``critique/routes/jobs.py`` and
``critique/routes/batch.py`` call ``validate_callback_url``; the brand domain
runs ``ensure_public_url`` at its fetch stage — so this one endpoint was the
hole in an otherwise uniform surface.

WHY "THE SCRAPER DOES THE FETCHING" IS NOT AN ANSWER
-----------------------------------------------------
The dereference happens one hop away, inside ``webscraper_service``. That makes this a
*delegated* SSRF, not somebody else's: this service accepts the attacker's input
and asks a peer to fetch it, and the peer hands the body back, so the 200 is an
exfiltration channel and not merely a blind request. It is also the last point
at which the request is still an HTTP request that can be answered with a 4xx.

Measured, not assumed: when this was written the hop went through the KAG
gateway, which had **no** SSRF screen (its ``/ingest/scrape/*`` handlers typed
the target as pydantic ``HttpUrl`` — pure syntax, which accepts
``http://169.254.169.254/`` — and proxied it onward). KAG is retired and the
route now calls ``webscraper_service`` directly. webscraper does resolve-screen,
but it is another repo with its own config switches (``ssrf_protection_enabled``,
``ssrf_allowed_hosts``), so relying on it makes this service's safety a property
of somebody else's deployment.

WHY THESE ADDRESSES ARE THE ONES THAT MATTER
--------------------------------------------
``ipaddress.ip_address("100.64.0.5").is_private`` is **False**, and so is
``.is_reserved``: CPython classifies RFC 6598 shared address space
(``100.64.0.0/10``) as neither. On this fleet that /10 is the entire Tailscale
tailnet — bare-metal prod Postgres (``100.64.0.5``), the Docker registry
(``100.64.0.6``), the k3s control plane (``100.64.0.7``). A guard written
as "block the private ranges" declares the whole estate a legitimate target.
``100.63.255.255`` / ``100.128.0.0`` bracket the range, so an implementation
that prefix-matches ``100.`` instead of testing CIDR membership is caught by the
allow-list cases rather than passing them by luck.

WHAT EACH GROUP LOCKS DOWN
--------------------------
``TestIcpTargetUrlIsScreened``  the route answers **400** and never constructs a
    scrape client, so nothing is dereferenced and no cache/DB row is written.
``TestIcpAllowsPublicTargets``  the false-positive control. Without it a guard
    that rejected *everything* would pass every test above. Also asserts the
    scrape actually happened, so "allowed" means "reached the sink".
``TestDnsAnswerBypass``  a public-looking NAME whose A record points into the
    tailnet — the case a literal-IP-only check misses entirely, and the reason
    the guard resolves rather than pattern-matching.
``TestSameDomainHarvestIsClosedByTheEntryGuard``  proves the second-order claim
    rather than asserting it in a comment: ``_select_extra_urls`` derives its
    same-domain filter from the landing URL, so an internal landing URL used to
    make internal links "same-domain" and therefore fetchable.
``TestHarvestedUrlsAreRescreened``  the residue the entry guard does *not*
    close: the same-domain test compares ``netloc.removeprefix("www.")``, so
    ``www.x`` and ``x`` count as one domain while being two DNS names with two
    A records. Enrichment drops the bad one and keeps going — a footer link to
    ``localhost`` must not turn a good extraction into a 502.

Every test here is hermetic: DNS comes from the autouse ``dns_stub`` fixture in
this directory's ``conftest.py`` (see ``tests/dns_stub.py``), and the scraper client
is a ``MagicMock``, so no query and no request leaves the test process.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from w2v_common.clients.webscraper import ScrapeResponse

from src.domains.icp.routes import _fetch_extra_pages, _select_extra_urls, extract_icp_from_url
from src.domains.icp.schemas import ICPFromUrlRequest
from tests.dns_stub import TAILNET_ANSWER, DnsStub
from tests.llm_test_doubles import FakeWMClient

_ROUTES_LOGGER = "src.domains.icp.routes"

# URLs a tenant could put in ``ICPFromUrlRequest.url`` today, each reaching
# something this service must never point the scraper at.
BLOCKED_TARGET_URLS = (
    # Bare-metal prod Postgres on the tailnet. ``is_private`` is False.
    "http://100.64.0.5/",
    # ...the same address wearing an IPv6 hat. Matches no IPv6 network and is
    # not ``is_private``; the embedded v4 must be unwrapped and re-checked.
    "http://[::ffff:100.64.0.5]/",
    # Cloud metadata. The canonical SSRF payload.
    "http://169.254.169.254/latest/meta-data/",
    # The Docker registry by name. Split-DNS answers it with a public-LOOKING
    # CGNAT address, so it has to die on the NAME, before the resolver is asked.
    # This is also the exact landing URL that poisons the same-domain harvest
    # filter -- see TestSameDomainHarvestIsClosedByTheEntryGuard.
    "http://registry.internal:5000/v2/_catalog",
    # In-cluster service name, resolvable only from inside the pod network.
    "http://pgbouncer.pgbouncer.svc.cluster.local:6432/",
    # Loopback, naming a port only the pod itself can reach.
    "http://localhost:6379/",
    # Non-HTTP scheme: a local file read rather than a network fetch.
    "file:///etc/passwd",
)

# Public targets that must keep working. The two ``100.`` literals bracket the
# CGNAT /10: if either is rejected, the implementation is prefix-matching
# ``100.`` rather than testing membership of ``100.64.0.0/10``.
ALLOWED_TARGET_URLS = (
    "https://merqi.nl/",
    "https://8.8.8.8/",
    "https://100.63.255.255/",
    "https://100.128.0.0/",
)


# A minimal grounded LLM answer, so the *allowed* cases fail only if the guard
# is wrong -- never because the fake gateway returned something unusable.
_GROUNDED_ICP = {
    "company": "Merqi builds whitelabel growth tooling",
    "product": "Merqi Platform",
    "value_props": ["Ship campaigns fast"],
}


def _make_request(*, wm_client: object | None = None) -> MagicMock:
    """Build a mock Request carrying a tenant and a usable WM client.

    Matches the direct-coroutine-invocation house style of
    ``test_icp_routes.py``: this repo has no ``TestClient``/``app`` fixture, and
    the guard under test lives in the handler, not in middleware.

    Args:
        wm_client: Stand-in for ``app.state.wm_client``. Defaults to a
            ``FakeWMClient`` so a *reached* LLM call cannot be what fails.

    Returns:
        A ``MagicMock`` request with tenant, license token and app state.
    """
    request = MagicMock()
    request.state.tenant_id = "52156f07-b22b-5b2f-a65d-d14389db4da0"
    request.state.license_token = "lic-token-abc"
    request.app.state.wm_client = (
        wm_client if wm_client is not None else FakeWMClient(outputs=[dict(_GROUNDED_ICP)])
    )
    request.app.state.icp_cache = None
    return request


def _fake_scraper(
    *,
    url: str = "https://merqi.nl",
    links: list[str] | None = None,
    bulk: list[ScrapeResponse] | None = None,
) -> MagicMock:
    """Build a scraper client double that records every URL it is handed.

    The recording IS the assertion for the blocked cases. A route raising 400
    proves nothing about *when* it raised; ``scrape_url`` never having been
    called proves the guard short-circuited before anything was dereferenced.

    Args:
        url: URL echoed back on the landing ``ScrapeResponse``.
        links: Anchor entries the landing page "discovered".
        bulk: Ordered responses ``scrape_bulk`` returns.

    Returns:
        A ``MagicMock`` with async ``scrape_url`` / ``scrape_bulk`` / ``close``.
    """
    client = MagicMock()
    client.scrape_url = AsyncMock(
        return_value=ScrapeResponse(
            url=url,
            title="Merqi",
            text="Merqi homepage copy about the product and its buyers.",
            error=None,
            links=links or [],
        )
    )
    client.scrape_bulk = AsyncMock(return_value=bulk or [])
    client.close = AsyncMock()
    return client


def _patch_scraper(client: MagicMock):
    """Patch the route module's ``WebscraperClient`` to yield *client*."""
    return patch("src.domains.icp.routes.WebscraperClient", return_value=client)


@pytest.fixture(autouse=True)
def _no_db_persist():
    """Neutralise best-effort DB persistence so these stay pure unit tests."""
    with patch("src.domains.icp.routes._persist_icp", new=AsyncMock()):
        yield


class TestIcpTargetUrlIsScreened:
    """``POST /api/brand/icp-from-url`` must refuse an estate-reachable target."""

    @pytest.mark.parametrize("url", BLOCKED_TARGET_URLS)
    async def test_blocked_target_is_400_and_never_scraped(self, url: str) -> None:
        """Reject with 400 *before* a scrape client is even constructed.

        Asserting only the status code would let a 400 raised for an unrelated
        reason (a broken WM client, say) pass as a security guarantee. The
        un-called ``scrape_url`` is what proves nothing was dereferenced.
        """
        client = _fake_scraper()
        request = _make_request()
        with _patch_scraper(client), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(ICPFromUrlRequest(url=url), request)
        assert exc.value.status_code == 400
        assert "Invalid URL" in str(exc.value.detail)
        client.scrape_url.assert_not_called()
        client.scrape_bulk.assert_not_called()

    @pytest.mark.parametrize("url", BLOCKED_TARGET_URLS)
    async def test_blocked_target_is_not_served_from_cache(self, url: str) -> None:
        """A refused URL is refused outright, not answered from a stale record.

        The guard runs before the cache lookup on purpose. A call made while
        this endpoint was unguarded could have left a cached extraction of an
        internal host; reading it back afterwards would hand over exactly the
        content the guard exists to withhold.
        """
        cache = MagicMock()
        cache.get_by_url = AsyncMock(return_value=None)
        request = _make_request()
        request.app.state.icp_cache = cache
        with _patch_scraper(_fake_scraper()), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(ICPFromUrlRequest(url=url), request)
        assert exc.value.status_code == 400
        cache.get_by_url.assert_not_called()


class TestIcpAllowsPublicTargets:
    """False-positive control: a guard that denied everything would pass above."""

    @pytest.mark.parametrize("url", ALLOWED_TARGET_URLS)
    async def test_public_target_reaches_the_scrape(self, url: str) -> None:
        """A legitimate public URL is still fetched and still returns a record."""
        client = _fake_scraper(url=url)
        request = _make_request()
        with _patch_scraper(client):
            result = await extract_icp_from_url(ICPFromUrlRequest(url=url), request)
        client.scrape_url.assert_awaited_once_with(url)
        assert result.source_url == url
        assert result.pages_fetched == [url]


class TestDnsAnswerBypass:
    """A public-looking NAME whose A record points into the tailnet."""

    async def test_name_resolving_into_the_tailnet_is_rejected(self, dns_stub: DnsStub) -> None:
        """The guard resolves; it does not merely pattern-match the hostname.

        Nothing about ``rebind.example.com`` looks internal, so a name-only or
        literal-IP-only screen waves it through. Only asking the resolver
        catches it.
        """
        dns_stub.map("rebind.example.com", TAILNET_ANSWER)
        client = _fake_scraper()
        request = _make_request()
        with _patch_scraper(client), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(
                ICPFromUrlRequest(url="https://rebind.example.com/"), request
            )
        assert exc.value.status_code == 400
        client.scrape_url.assert_not_called()

    async def test_unresolvable_name_fails_closed(self, dns_stub: DnsStub) -> None:
        """A name we cannot resolve is a name we cannot vouch for.

        Delivery happens later and from a different network position
        (in-cluster, where split-DNS *does* answer estate names), so "it will
        fail naturally at fetch time" is not true here.
        """
        dns_stub.fail("nxdomain.example.com")
        client = _fake_scraper()
        request = _make_request()
        with _patch_scraper(client), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(
                ICPFromUrlRequest(url="https://nxdomain.example.com/"), request
            )
        assert exc.value.status_code == 400
        client.scrape_url.assert_not_called()


class TestSameDomainHarvestIsClosedByTheEntryGuard:
    """The second-order surface, demonstrated rather than asserted in prose."""

    def test_internal_landing_url_makes_internal_links_same_domain(self) -> None:
        """Why guarding the ENTRY url closes the harvest surface.

        ``_select_extra_urls`` computes ``base_netloc`` from the landing URL it
        is handed. Hand it an internal one and the internal links on that page
        pass its same-domain test — which is precisely the state the route was
        in before the entry guard existed. This test does not assert the bug is
        acceptable; it pins the mechanism, so that if someone ever removes the
        route guard the reason it mattered is written down and executable.
        """
        # ``links`` is *annotated* ``list[str]``; the retired KAG hop returned anchor
        # dicts -- see ``_link_href``. The suppression is the house pattern in
        # ``test_icp_routes.py``; feeding the real wire shape is the point.
        picked = _select_extra_urls(
            "http://registry.internal:5000/",
            [{"url": "http://registry.internal:5000/pricing"}],  # ty: ignore[invalid-argument-type]
            3,
        )
        assert picked == [("pricing", "http://registry.internal:5000/pricing")]

    def test_offdomain_links_are_dropped_regardless(self) -> None:
        """Same-domain filtering is real, so a public landing page cannot pivot."""
        picked = _select_extra_urls(
            "https://merqi.nl/",
            [  # ty: ignore[invalid-argument-type]
                {"url": "http://169.254.169.254/pricing"},
                {"url": "http://registry.internal:5000/pricing"},
                {"url": "https://merqi.nl/pricing"},
            ],
            3,
        )
        assert picked == [("pricing", "https://merqi.nl/pricing")]


class TestHarvestedUrlsAreRescreened:
    """Defence in depth for what same-domain STRING matching cannot see."""

    async def test_www_stripping_lets_a_sibling_name_through_selection(self) -> None:
        """The gap the second guard exists for, shown at the selection layer.

        ``_select_extra_urls`` compares ``netloc.removeprefix("www.")``, so a
        landing page on ``www.merqi.nl`` counts links on the bare ``merqi.nl``
        as same-domain. Those are two distinct DNS names that can hold two
        distinct A records, and only the landing one was resolved at admission.
        """
        picked = _select_extra_urls(
            "https://www.merqi.nl/",
            [{"url": "https://merqi.nl/pricing"}],  # ty: ignore[invalid-argument-type]
            3,
        )
        assert picked == [("pricing", "https://merqi.nl/pricing")]

    async def test_harvested_url_resolving_into_the_tailnet_is_dropped(
        self, dns_stub: DnsStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        """...and is then caught at the sink, before ``scrape_bulk``."""
        dns_stub.map("merqi.nl", TAILNET_ANSWER)
        client = _fake_scraper()
        with caplog.at_level(logging.WARNING, logger=_ROUTES_LOGGER):
            extra = await _fetch_extra_pages(client, [("pricing", "https://merqi.nl/pricing")])
        assert extra == []
        client.scrape_bulk.assert_not_called()
        assert any("refusing harvested URL" in record.message for record in caplog.records)

    async def test_bad_harvested_url_is_dropped_not_fatal(self, dns_stub: DnsStub) -> None:
        """One refused link degrades enrichment; it does not fail the request.

        A legitimate site whose footer links ``http://localhost:3000`` must
        still get a good extraction. This is also the false-positive control for
        the harvest guard: the surviving URL proves it is filtering, not
        blanket-denying.
        """
        dns_stub.map("evil.merqi.nl", TAILNET_ANSWER)
        good = ScrapeResponse(
            url="https://merqi.nl/pricing", title="Pricing", text="From EUR 49/mo", error=None
        )
        client = _fake_scraper(bulk=[good])
        extra = await _fetch_extra_pages(
            client,
            [
                ("pricing", "https://evil.merqi.nl/pricing"),
                ("about", "https://merqi.nl/pricing"),
            ],
        )
        client.scrape_bulk.assert_awaited_once_with(["https://merqi.nl/pricing"])
        assert extra == [("about", "https://merqi.nl/pricing", "From EUR 49/mo")]

    async def test_public_harvested_urls_all_survive(self) -> None:
        """False-positive control: nothing is dropped when everything is public."""
        pages = [
            ScrapeResponse(url="https://merqi.nl/pricing", title="P", text="p", error=None),
            ScrapeResponse(url="https://merqi.nl/about", title="A", text="a", error=None),
        ]
        client = _fake_scraper(bulk=pages)
        extra = await _fetch_extra_pages(
            client,
            [("pricing", "https://merqi.nl/pricing"), ("about", "https://merqi.nl/about")],
        )
        client.scrape_bulk.assert_awaited_once_with(
            ["https://merqi.nl/pricing", "https://merqi.nl/about"]
        )
        assert [label for label, _, _ in extra] == ["pricing", "about"]
