"""Unit tests for the ICP-from-URL route and its grounding filter.

Routes are called directly with a mock ``Request`` (the brand/analyze house
style) — no live TestClient. The webscraper and WM clients are faked so no HTTP
traffic leaves the test.
"""

from __future__ import annotations

import functools
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from w2v_common.auth.middleware import AuthMiddleware
from w2v_common.clients.webscraper import ScrapeResponse

from src.domains.icp.routes import (
    _select_extra_urls,
    extract_icp_from_url,
)
from src.domains.icp.schemas import ICPFromUrlRequest, ICPResponse
from src.domains.icp.service import (
    ICP_OUTPUT_SCHEMA,
    REQUIRED_ICP_KEYS,
    compute_confidence,
    filter_grounded,
    merge_page_sections,
)
from tests.llm_test_doubles import FakeWMClient

_FULL_ICP = {
    "company": "Merqi builds whitelabel growth tooling",
    "product": "Merqi Platform",
    "offering": "Marketing + Sales + CRM in one suite",
    "value_props": ["Ship campaigns fast", "One tenant, one login"],
    "pricing_signals": "Tiered SaaS pricing from €49/mo",
    "brand_identity": {"name": "Merqi", "tagline": "Growth, whitelabelled"},
    "positioning": "The GTM OS for agencies",
    "category": "Marketing automation",
    "competitors": ["HubSpot", "ActiveCampaign"],
    "pains": ["Fragmented tools", "No single source of truth"],
    "problems": ["Manual campaign ops"],
}

# Route path is under /api and NOT /api/health* → auth-required by design.
_ICP_PATH = "/api/brand/icp-from-url"


@pytest.fixture(autouse=True)
def _no_db_persist():
    """Neutralize best-effort DB persistence so route tests stay pure unit tests.

    Real persistence (its own tenant-bound session) is exercised separately in
    ``test_icp_persistence.py`` against a live DB.
    """
    with patch("src.domains.icp.routes._persist_icp", new=AsyncMock()) as mock:
        yield mock


def _make_request(
    *,
    tenant_id: str | None = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
    wm_client: object | None = None,
    icp_cache: object | None = None,
) -> MagicMock:
    """Build a mock Request exposing app.state.wm_client + request.state.

    ``icp_cache`` defaults to ``None`` (cache disabled / Redis down) so the route
    runs the full extraction path; pass a fake cache to exercise hit/write.
    """
    request = MagicMock()
    request.state.tenant_id = tenant_id
    request.state.license_token = "lic-token-abc"
    request.app.state.wm_client = wm_client
    request.app.state.icp_cache = icp_cache
    # _startup sets a real TenantLicenceLane; a bare MagicMock attribute would be
    # a truthy fake lane. None = the shared default (WEBSCRAPER_AUTH_MODE unset).
    request.app.state.webscraper_lane = None
    return request


def _patch_scraper(
    text: str = "Merqi homepage copy",
    title: str = "Merqi",
    error: str | None = None,
    *,
    url: str = "https://merqi.nl",
    links: list[str] | None = None,
    bulk: list[ScrapeResponse] | None = None,
):
    """Patch the WebscraperClient in the route module to return canned scrapes.

    ``links`` populates the landing page's discovered links (drives extra-page
    selection); ``bulk`` is the ordered list ``scrape_bulk`` returns for the
    selected URLs.
    """
    fake_ws = MagicMock()
    fake_ws.scrape_url = AsyncMock(
        return_value=ScrapeResponse(url=url, title=title, text=text, error=error, links=links or [])
    )
    fake_ws.scrape_bulk = AsyncMock(return_value=bulk or [])
    # The route awaits ``ws_client.close()`` in a ``finally`` to release the
    # httpx pool; make it awaitable so tests exercise that path (and can spy it).
    fake_ws.close = AsyncMock()
    # The context manager yields the patched WebscraperClient class; its
    # ``.return_value`` is ``fake_ws`` (what ``WebscraperClient(...)`` returns).
    return patch("src.domains.icp.routes.WebscraperClient", return_value=fake_ws)


# ---------------------------------------------------------------------------
# The structured-output contract — what the model is actually CONSTRAINED by
# ---------------------------------------------------------------------------


def _unsatisfied_required(schema: dict, instance: dict) -> list[str]:
    """The JSON-Schema ``required`` keyword, evaluated for an object instance.

    Draft 2020-12 §6.5.3: an object instance is valid against ``required`` iff
    every name in the array is a property name of the instance. That is the whole
    keyword — this is not an approximation of it. Written out here rather than
    imported because ``jsonschema`` is not a dependency of this service, and
    adding one so a test can restate three lines of the spec is the wrong trade.

    Returns the names that are missing (empty list == valid).
    """
    required = schema.get("required") or []
    return [name for name in required if name not in instance]


class TestIcpOutputSchemaContract:
    """The schema is the only thing that can stop `{}` being GENERATED.

    Everything else in this file is a backstop that runs after the model has
    already answered. These tests pin the contract that goes out on the request,
    because that is what vLLM's guided decoder compiles into a grammar.
    """

    def test_required_is_populated(self):
        # The 2026-08-26 regression in one line: this list was empty, so `{}`
        # was a schema-VALID answer and the decoder was free to stop after
        # three tokens. A schema that cannot reject anything is not a contract.
        assert ICP_OUTPUT_SCHEMA["required"], "ICP output schema declares no required keys"
        assert ICP_OUTPUT_SCHEMA["required"] == list(REQUIRED_ICP_KEYS)

    def test_every_required_key_is_a_declared_property(self):
        # worker_management warns (`_warn_if_unsatisfiable`) when a required name
        # is absent from `properties`: the decoder then cannot emit it, so no
        # completion can ever validate and the agent burns its budget on doomed
        # retries. Assert we never ship that shape.
        properties = ICP_OUTPUT_SCHEMA["properties"]
        assert [k for k in REQUIRED_ICP_KEYS if k not in properties] == []

    def test_the_empty_object_does_not_satisfy_the_schema(self):
        # The exact live payload: `{}` in 3 completion tokens. Under the old
        # `"required": []` this validated; it must not.
        assert _unsatisfied_required(ICP_OUTPUT_SCHEMA, {}) == list(REQUIRED_ICP_KEYS)

    def test_a_full_extraction_still_satisfies_the_schema(self):
        # The contract must not have been tightened past what a good extraction
        # already produces — a required set that reds a working answer would be
        # a worse bug than the one it fixes.
        assert _unsatisfied_required(ICP_OUTPUT_SCHEMA, dict(_FULL_ICP)) == []

    async def test_the_request_carries_the_populated_schema(self):
        # The contract only matters if it reaches WM. `WMClient.message` sends
        # `output_schema` in the body, and worker_management applies it as a
        # per-message override — which is what makes the NULL `output_schema`
        # column on the `icp-analysis` worker row irrelevant.
        fake = FakeWMClient(outputs=[dict(_FULL_ICP)])
        request = _make_request(wm_client=fake)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            await extract_icp_from_url(body, request)
        assert len(fake.calls) == 1
        sent = fake.calls[0].output_schema
        assert sent is not None
        assert sent["required"], "the request went out with an unenforceable schema"
        assert _unsatisfied_required(sent, {}), "`{}` would satisfy the schema we sent"

    async def test_structured_output_not_applied_is_logged(self, caplog):
        # WM echoes what it used. A plain-string answer means guided decoding
        # never engaged, so `required` was not enforced at all — the silent state
        # this module sat in for three days. It stays non-fatal (the text parses
        # to a grounded record here) but it must not be silent.
        fake = FakeWMClient(outputs=[json.dumps(_FULL_ICP)])
        request = _make_request(wm_client=fake)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with caplog.at_level(logging.WARNING, logger="src.domains.icp.service"), _patch_scraper():
            result = await extract_icp_from_url(body, request)
        assert result.company.startswith("Merqi")
        assert any("structured output was NOT applied" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# filter_grounded — the anti-fabrication gate
# ---------------------------------------------------------------------------


class TestFilterGrounded:
    def test_keeps_all_grounded_keys(self):
        out = filter_grounded(dict(_FULL_ICP))
        assert out["company"].startswith("Merqi")
        assert out["value_props"] == ["Ship campaigns fast", "One tenant, one login"]
        assert out["competitors"] == ["HubSpot", "ActiveCampaign"]
        assert out["brand_identity"] == {"name": "Merqi", "tagline": "Growth, whitelabelled"}

    def test_drops_empty_values(self):
        out = filter_grounded(
            {
                "company": "Acme",
                "product": "",
                "value_props": [],
                "competitors": [""],
                "pains": None,
            }
        )
        assert out == {"company": "Acme"}

    def test_normalizes_camelcase_aliases(self):
        out = filter_grounded({"valueProps": ["a"], "brandIdentity": {"name": "X"}})
        assert out["value_props"] == ["a"]
        assert out["brand_identity"] == {"name": "X"}

    def test_ignores_unknown_keys(self):
        out = filter_grounded({"company": "Acme", "hallucinated_key": "nope"})
        assert out == {"company": "Acme"}

    def test_pricing_signals_accepts_str_or_list(self):
        assert filter_grounded({"pricing_signals": "€49/mo"})["pricing_signals"] == "€49/mo"
        assert filter_grounded({"pricing_signals": ["a", "b"]})["pricing_signals"] == ["a", "b"]


# ---------------------------------------------------------------------------
# extract_icp_from_url — the route
# ---------------------------------------------------------------------------


class TestExtractIcpFromUrl:
    async def test_happy_path_returns_flat_grounded_record(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            result = await extract_icp_from_url(body, request)
        assert result.company.startswith("Merqi")
        assert result.competitors == ["HubSpot", "ActiveCampaign"]
        assert result.source_url == "https://merqi.nl"
        assert result.token_usage is not None
        assert result.token_usage["total_tokens"] == 2
        # All 11 contract facets grounded → full coverage.
        assert result.confidence == 1.0
        # No links on the landing page → landing page is the only source.
        assert result.pages_fetched == ["https://merqi.nl"]
        # Only grounded keys (+ metadata) reach the wire.
        dumped = result.model_dump(exclude_unset=True)
        assert set(dumped) == set(_FULL_ICP) | {
            "source_url",
            "token_usage",
            "confidence",
            "pages_fetched",
        }

    async def test_scrape_client_is_closed_after_request(self):
        # The lazily-opened httpx pool must be released on every request, else
        # sockets/FDs to webscraper leak until GC and exhaust the pod under load.
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper() as ws_cls:
            await extract_icp_from_url(body, request)
        ws_cls.return_value.close.assert_awaited_once()

    async def test_scrape_client_is_closed_even_on_llm_failure(self):
        # A failure mid-request must still release the client (close in finally).
        request = _make_request(
            wm_client=FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper() as ws_cls:
            with pytest.raises(HTTPException):
                await extract_icp_from_url(body, request)
        ws_cls.return_value.close.assert_awaited_once()

    async def test_ungrounded_keys_omitted(self):
        llm_out = {"company": "Acme", "value_props": ["fast"], "competitors": []}
        request = _make_request(wm_client=FakeWMClient(outputs=[llm_out]))
        body = ICPFromUrlRequest(url="https://acme.test")
        with _patch_scraper():
            result = await extract_icp_from_url(body, request)
        dumped = result.model_dump(exclude_unset=True)
        assert dumped["company"] == "Acme"
        assert dumped["value_props"] == ["fast"]
        # empty list and unmentioned keys are absent
        assert "competitors" not in dumped
        assert "product" not in dumped
        assert "pains" not in dumped

    async def test_vacuous_extraction_is_502_not_a_200(self):
        # The live shape of the 2026-08-26 regression: the model answered `{}`
        # in 3 completion tokens. It is schema-valid (the ICP output schema
        # marks every key optional), so nothing below this route rejects it —
        # it used to be returned 200 with confidence 0.0 and persisted as an
        # audit row. That is the extraction not happening, not a result.
        request = _make_request(wm_client=FakeWMClient(outputs=[{}]))
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            with pytest.raises(HTTPException) as exc:
                await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502
        assert "no grounded ICP fields" in str(exc.value.detail)

    async def test_vacuous_extraction_is_not_cached_or_persisted(self):
        # Worse than the bogus 200: `_cache_icp` pinned the empty record under
        # tenant:domain for cache.icp_ttl (1h), so every later caller got the
        # vacuum back without a retry — one bad generation suppressed the whole
        # domain for an hour. Neither side effect may run.
        request = _make_request(wm_client=FakeWMClient(outputs=[{}]))
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            with (
                patch("src.domains.icp.routes._cache_icp", new=AsyncMock()) as cache,
                patch("src.domains.icp.routes._persist_icp", new=AsyncMock()) as persist,
                pytest.raises(HTTPException),
            ):
                await extract_icp_from_url(body, request)
        cache.assert_not_awaited()
        persist.assert_not_awaited()

    async def test_all_keys_filtered_out_is_also_502(self):
        # The anti-fabrication filter can empty a NON-empty completion: every
        # value here is empty or the wrong type, so filter_grounded returns {}.
        # Same vacuum, same fail-closed answer.
        request = _make_request(
            wm_client=FakeWMClient(outputs=[{"company": "", "value_props": [], "pains": None}])
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            with pytest.raises(HTTPException) as exc:
                await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502

    async def test_fetch_error_is_502(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://down.test")
        with _patch_scraper(error="connection refused"):
            with pytest.raises(HTTPException) as exc:
                await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502

    async def test_llm_failure_is_502(self):
        request = _make_request(
            wm_client=FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            with pytest.raises(HTTPException) as exc:
                await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502

    async def test_missing_wm_client_is_502(self):
        request = _make_request(wm_client=None)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Multi-page fetch — discover pricing/about pages and merge into the prompt
# ---------------------------------------------------------------------------


class TestMultiPageFetch:
    async def test_extra_pages_are_fetched_and_merged(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl", max_pages=3)
        links = [
            "https://merqi.nl/pricing",
            "https://merqi.nl/about",
            "https://merqi.nl/blog/post-1",  # not a wanted type → ignored
        ]
        bulk = [
            ScrapeResponse(
                url="https://merqi.nl/pricing", text="Plans from €49/mo", title="Pricing"
            ),
            ScrapeResponse(
                url="https://merqi.nl/about", text="We compete with HubSpot", title="About"
            ),
        ]
        with _patch_scraper(links=links, bulk=bulk) as ws_cls:
            result = await extract_icp_from_url(body, request)
            fake_ws = ws_cls.return_value
        # scrape_bulk was called with exactly the two wanted (pricing, about) URLs, in priority order.
        fake_ws.scrape_bulk.assert_awaited_once()
        called_urls = fake_ws.scrape_bulk.await_args.args[0]
        assert called_urls == ["https://merqi.nl/pricing", "https://merqi.nl/about"]
        # Provenance lists the landing page + fetched extras.
        assert result.pages_fetched == [
            "https://merqi.nl",
            "https://merqi.nl/pricing",
            "https://merqi.nl/about",
        ]
        # The LLM saw a labelled multi-section prompt.
        prompt = request.app.state.wm_client.calls[0].message
        assert "## PRICING PAGE: https://merqi.nl/pricing" in prompt
        assert "## ABOUT PAGE: https://merqi.nl/about" in prompt
        assert "Plans from €49/mo" in prompt

    async def test_max_pages_caps_selection(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl", max_pages=1)
        links = ["https://merqi.nl/pricing", "https://merqi.nl/about"]
        bulk = [ScrapeResponse(url="https://merqi.nl/pricing", text="Plans", title="Pricing")]
        with _patch_scraper(links=links, bulk=bulk) as ws_cls:
            await extract_icp_from_url(body, request)
            fake_ws = ws_cls.return_value
        # Only the highest-priority page (pricing) is fetched under max_pages=1.
        called_urls = fake_ws.scrape_bulk.await_args.args[0]
        assert called_urls == ["https://merqi.nl/pricing"]

    async def test_bulk_error_entries_dropped(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl", max_pages=3)
        links = ["https://merqi.nl/pricing", "https://merqi.nl/about"]
        bulk = [
            ScrapeResponse(url="https://merqi.nl/pricing", text="Plans", title="Pricing"),
            ScrapeResponse(url="https://merqi.nl/about", error="timeout"),
        ]
        with _patch_scraper(links=links, bulk=bulk):
            result = await extract_icp_from_url(body, request)
        # The errored /about page is not in provenance; pricing survives.
        assert result.pages_fetched == ["https://merqi.nl", "https://merqi.nl/pricing"]

    async def test_no_links_falls_back_to_single_page(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl", max_pages=3)
        with _patch_scraper(links=[]) as ws_cls:
            result = await extract_icp_from_url(body, request)
            fake_ws = ws_cls.return_value
        fake_ws.scrape_bulk.assert_not_awaited()
        assert result.pages_fetched == ["https://merqi.nl"]

    async def test_bulk_failure_is_non_fatal(self):
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        body = ICPFromUrlRequest(url="https://merqi.nl", max_pages=3)
        with _patch_scraper(links=["https://merqi.nl/pricing"]) as ws_cls:
            ws_cls.return_value.scrape_bulk = AsyncMock(side_effect=RuntimeError("bulk down"))
            result = await extract_icp_from_url(body, request)
        # Landing extraction still succeeds; only the landing page is in provenance.
        assert result.company.startswith("Merqi")
        assert result.pages_fetched == ["https://merqi.nl"]


# ---------------------------------------------------------------------------
# Extra-URL selection + section merge + coverage math (service/route helpers)
# ---------------------------------------------------------------------------


class TestSelectExtraUrls:
    def test_selects_wanted_types_in_priority_order(self):
        picked = _select_extra_urls(
            "https://x.io",
            [
                "https://x.io/about",
                "https://x.io/features",  # PRODUCT
                "https://x.io/pricing",
                "https://x.io/blog",  # not wanted
            ],
            max_pages=5,
        )
        # pricing (0) < about (1) < product (2)
        assert picked == [
            ("pricing", "https://x.io/pricing"),
            ("about", "https://x.io/about"),
            ("product", "https://x.io/features"),
        ]

    def test_skips_offdomain_and_landing_and_dupes(self):
        picked = _select_extra_urls(
            "https://x.io",
            [
                "https://x.io/",  # landing itself
                "https://other.com/pricing",  # off-domain
                "https://x.io/pricing",
                "https://x.io/pricing#plans",  # dupe of pricing (fragment stripped)
                "/about",  # relative → resolved on-domain
            ],
            max_pages=5,
        )
        assert picked == [
            ("pricing", "https://x.io/pricing"),
            ("about", "https://x.io/about"),
        ]

    def test_relative_and_www_are_same_domain(self):
        picked = _select_extra_urls(
            "https://www.x.io/home",
            ["https://x.io/pricing"],  # bare host vs www → same registrable domain
            max_pages=5,
        )
        assert picked == [("pricing", "https://x.io/pricing")]

    def test_non_str_links_are_skipped_not_fatal(self):
        # Real scraped pages yield None / non-str entries from malformed anchors;
        # urljoin would raise "Cannot mix str and non-str arguments" and 500 the
        # request. Enrichment is best-effort → skip the junk, keep the good link.
        # why: deliberately passes non-str junk to verify it is skipped, not fatal
        bad_links = [None, "", 123, "https://x.io/pricing"]
        picked = _select_extra_urls("https://x.io", bad_links, max_pages=5)  # ty: ignore[invalid-argument-type]
        assert picked == [("pricing", "https://x.io/pricing")]

    def test_dict_shape_links_are_extracted(self):
        # GROUND TRUTH (verified live): the webscraper returns anchor DICTS —
        # {"text", "url", "rel", "type"} — NOT bare strings, despite
        # ScrapeResponse.links being annotated list[str]. The prior
        # `isinstance(link, str)` guard skipped every dict, so multi-page
        # enrichment never fired in production. Verify the real shape is honored
        # and unusable dicts are still skipped.
        links = [
            {"text": "Tarieven", "url": "https://x.io/pricing", "rel": None, "type": "internal"},
            {"text": "About", "url": "https://x.io/about", "type": "internal"},
            {"text": "", "url": "https://x.io/", "type": "internal"},  # landing → skipped
            {"text": "Off", "url": "https://other.com/pricing"},  # off-domain → skipped
            {"text": "junk", "url": None},  # unusable url → skipped
            {"text": "no-url-key"},  # missing url → skipped
        ]
        picked = _select_extra_urls("https://x.io", links, max_pages=5)  # ty: ignore[invalid-argument-type]
        assert picked == [
            ("pricing", "https://x.io/pricing"),
            ("about", "https://x.io/about"),
        ]

    def test_mixed_str_and_dict_links(self):
        # Tolerate both shapes in one list (defensive: some callers/tests still
        # pass plain strings).
        links = [
            "https://x.io/pricing",
            {"url": "https://x.io/about", "type": "internal"},
        ]
        picked = _select_extra_urls("https://x.io", links, max_pages=5)  # ty: ignore[invalid-argument-type]
        assert picked == [
            ("pricing", "https://x.io/pricing"),
            ("about", "https://x.io/about"),
        ]


class TestMergeAndConfidence:
    def test_merge_labels_and_orders_sections(self):
        merged = merge_page_sections(
            "landing body",
            [
                ("pricing", "https://x/pricing", "price body"),
                ("about", "https://x/about", "about body"),
            ],
        )
        assert merged.startswith("landing body")
        assert "## PRICING PAGE: https://x/pricing\nprice body" in merged
        assert merged.index("PRICING") < merged.index("ABOUT")

    def test_merge_no_extras_is_landing_only(self):
        assert merge_page_sections("just landing") == "just landing"

    def test_confidence_is_coverage_ratio(self):
        assert compute_confidence(dict(_FULL_ICP)) == 1.0
        assert compute_confidence({"company": "Acme"}) == round(1 / 11, 3)
        assert compute_confidence({}) == 0.0


# ---------------------------------------------------------------------------
# Redis cache — hit short-circuits; miss writes; absence degrades gracefully
# ---------------------------------------------------------------------------


def _fake_cache(hit: ICPResponse | None = None) -> MagicMock:
    """A fake IcpCacheService: get_by_url returns ``hit``; set records the call."""
    cache = MagicMock()
    cache.get_by_url = AsyncMock(return_value=hit)
    cache.set = AsyncMock(return_value=True)
    return cache


class TestIcpCache:
    async def test_cache_hit_short_circuits(self):
        cached = ICPResponse(company="Cached Co", confidence=0.5)
        cache = _fake_cache(hit=cached)
        # No WM client and a scraper that would raise — proving neither runs on a hit.
        request = _make_request(wm_client=None, icp_cache=cache)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper() as ws_cls:
            ws_cls.return_value.scrape_url = AsyncMock(side_effect=AssertionError("scraped on hit"))
            result = await extract_icp_from_url(body, request)
        assert result.company == "Cached Co"
        cache.get_by_url.assert_awaited_once_with(
            "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "https://merqi.nl"
        )
        cache.set.assert_not_awaited()

    async def test_miss_extracts_and_writes(self):
        cache = _fake_cache(hit=None)
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]), icp_cache=cache)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            result = await extract_icp_from_url(body, request)
        assert result.company.startswith("Merqi")
        # Written under the URL's bare domain, with the freshly extracted response.
        cache.set.assert_awaited_once()
        tenant_arg, domain_arg, value_arg = cache.set.await_args.args
        assert (tenant_arg, domain_arg) == ("e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "merqi.nl")
        assert value_arg.company.startswith("Merqi")

    async def test_vacuum_never_reaches_a_live_cache(self):
        # The sibling test in `TestExtractIcpFromUrl` patches `_cache_icp` and so
        # proves the route does not CALL it. This one keeps the real helper and a
        # live cache object in place, so it proves nothing lands in Redis either
        # — the harm was never the 502, it was the hour the vacuum then served
        # from `tenant:domain` without re-invoking the LLM.
        cache = _fake_cache(hit=None)
        request = _make_request(wm_client=FakeWMClient(outputs=[{}]), icp_cache=cache)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper(), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(body, request)
        assert exc.value.status_code == 502
        cache.set.assert_not_awaited()

    async def test_no_cache_degrades_to_extraction(self):
        # icp_cache=None (Redis down) → full extraction still succeeds.
        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]), icp_cache=None)
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            result = await extract_icp_from_url(body, request)
        assert result.company.startswith("Merqi")


# ---------------------------------------------------------------------------
# Persistence invocation — best-effort audit write for a real tenant only
# ---------------------------------------------------------------------------


class TestPersistInvocation:
    async def test_persists_for_real_tenant(self, _no_db_persist):
        request = _make_request(
            tenant_id="d193a869-ec4a-5a1e-a016-947255fb39bc",
            wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]),
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            result = await extract_icp_from_url(body, request)
        _no_db_persist.assert_awaited_once()
        tenant_arg, url_arg, value_arg = _no_db_persist.await_args.args
        assert (tenant_arg, url_arg) == ("d193a869-ec4a-5a1e-a016-947255fb39bc", "https://merqi.nl")
        assert value_arg.company == result.company

    async def test_persists_unconditionally_for_a_real_tenant(self, _no_db_persist):
        """DELIBERATE INVERSION. This asserted persist was SKIPPED, because the
        route used to strip a ``"default"`` sentinel tenant and refuse to write
        an audit row for it. ``_get_tenant_id`` now answers an absent or
        sentinel tenant with 401, so by the time this line runs the tenant is
        always a real registered one — and the audit trail must be written for
        it. Keeping the old assertion would have silently dropped every ICP
        audit row.
        """
        tenant = "46f24daf-c3de-5399-a669-b829092bfe31"
        request = _make_request(
            tenant_id=tenant,
            wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]),
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper():
            await extract_icp_from_url(body, request)
        _no_db_persist.assert_awaited_once()
        assert _no_db_persist.await_args.args[0] == tenant

    async def test_an_absent_tenant_is_401_before_any_persist(self, _no_db_persist):
        """The refusal must happen at the door, not after the LLM has been paid for."""
        request = _make_request(
            tenant_id=None,
            wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]),
        )
        body = ICPFromUrlRequest(url="https://merqi.nl")
        with _patch_scraper(), pytest.raises(HTTPException) as exc:
            await extract_icp_from_url(body, request)
        assert exc.value.status_code == 401
        _no_db_persist.assert_not_awaited()


# ---------------------------------------------------------------------------
# Auth — the route must require the license token (not exempt)
# ---------------------------------------------------------------------------


class TestIcpRouteRequiresAuth:
    def _make_request(self, headers: dict | None = None, client_host: str = "8.8.8.8") -> MagicMock:
        request = MagicMock()
        request.url.path = _ICP_PATH
        request.headers = MagicMock()
        request.headers.get = MagicMock(
            side_effect=lambda key, default=None: (headers or {}).get(key, default)
        )
        request.client = MagicMock()
        request.client.host = client_host
        request.state = MagicMock()
        return request

    def test_path_is_not_public(self):
        from w2v_common.auth.middleware import PUBLIC_ENDPOINTS, PUBLIC_PREFIXES

        assert _ICP_PATH not in PUBLIC_ENDPOINTS
        assert not _ICP_PATH.startswith(PUBLIC_PREFIXES)

    async def test_no_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()
        request = self._make_request(headers={})
        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()


# ---------------------------------------------------------------------------
# The webscraper hop under the cell lane (CELL_AUTH_MODE=enforce)
# ---------------------------------------------------------------------------


class TestWebscraperHopUsesIdentityNotCallerCredential:
    """The outbound webscraper call must be built from IDENTITY, not a caller token.

    A request admitted by an ``X-W2V-Cell-Token`` (the only lane under
    ``CELL_AUTH_MODE=enforce``) has ``request.state.license_token = None`` and a
    ``CELL_JWT``-vouched tenant bound. webscraper requires a Bearer licence in
    ``X-Tenant-Id`` and is NOT a metered/delegating destination, so the hop goes
    out as this service's licence with no ``X-Effective-Tenant-Id`` -- and never
    as the inbound caller's raw token.

    The real ``WebscraperClient`` and the real ``identity_event_hook`` run here;
    only the socket is replaced, so these assert the headers webscraper would see.
    """

    _TENANT = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"

    async def _run_and_capture(
        self, monkeypatch: pytest.MonkeyPatch, *, caller_license_token: str | None
    ) -> list[httpx.Request]:
        from w2v_common.auth.identity_context import clear_identity_context, set_identity_context
        from w2v_common.clients import webscraper
        from w2v_common.tenant import TenantSource

        monkeypatch.setenv("DOWNSTREAM_LICENSE_TOKEN", "sa-licence")
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "url": "https://merqi.nl",
                    "status": "completed",
                    "title": "Merqi",
                    "text_content": "Merqi copy",
                },
            )

        monkeypatch.setattr(
            webscraper.httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
        )

        request = _make_request(wm_client=FakeWMClient(outputs=[dict(_FULL_ICP)]))
        request.state.license_token = caller_license_token
        set_identity_context(
            tenant_id=self._TENANT,
            license_id=None,
            subject_id=None,
            caller_license_token=caller_license_token,
            tenant_source=TenantSource.CELL_JWT
            if caller_license_token is None
            else TenantSource.LICENSE_TOKEN,
        )
        try:
            await extract_icp_from_url(ICPFromUrlRequest(url="https://merqi.nl"), request)
        finally:
            clear_identity_context()
        return captured

    async def test_cell_lane_request_uses_the_service_licence(self, monkeypatch):
        captured = await self._run_and_capture(monkeypatch, caller_license_token=None)
        assert captured, "the route never reached webscraper"
        for sent in captured:
            assert sent.headers.get("X-Tenant-Id") == "Bearer sa-licence"
            # Not a metered destination: nothing is delegated.
            assert "X-Effective-Tenant-Id" not in sent.headers

    async def test_licence_lane_never_forwards_the_callers_raw_token(self, monkeypatch):
        captured = await self._run_and_capture(monkeypatch, caller_license_token="caller-raw-token")
        assert captured, "the route never reached webscraper"
        for sent in captured:
            assert "caller-raw-token" not in " ".join(sent.headers.values())
            assert sent.headers.get("X-Tenant-Id") == "Bearer sa-licence"
            assert "X-Effective-Tenant-Id" not in sent.headers
