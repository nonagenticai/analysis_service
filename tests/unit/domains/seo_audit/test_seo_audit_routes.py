"""Unit tests for the synchronous SEO audit route + service.

Routes are called directly with a mock ``Request`` (the cro/artifact_score house
style). No live TestClient, no HTTP traffic, no LLM/DB. The fetch is faked for
URL input; HTML input runs the real deterministic scanner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.domains.critique.services.scanners.seo.scoring import GROUP_WEIGHTS
from src.domains.seo_audit.routes import audit_seo_route
from src.domains.seo_audit.schemas import SeoAuditRequest
from src.domains.seo_audit.service import SeoAuditError, audit_seo

# A recent date keeps the GEO freshness check clean regardless of when tests run.
_RECENT = (datetime.now(UTC) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

# A genuinely well-optimized page: clean across all nine audit groups (on-page,
# technical, content, keyword, schema, eeat, internal_linking, snippet, geo).
_STRONG = f"""
<html lang="en"><head>
<title>How to brew better coffee at home</title>
<meta name="description" content="A friendly guide to brewing better coffee at home with simple, repeatable steps and clear advice.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="author" content="Jane Doe">
<meta property="article:modified_time" content="{_RECENT}">
<link rel="canonical" href="https://example.test/coffee">
<link rel="icon" href="/favicon.ico">
<meta property="og:title" content="How to brew better coffee at home">
<meta property="og:description" content="A friendly guide to brewing better coffee at home.">
<meta property="og:image" content="https://example.test/a.jpg">
<meta property="og:url" content="https://example.test/coffee">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="How to brew better coffee at home">
<meta name="twitter:description" content="A friendly guide to brewing better coffee.">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}}</script>
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"Organization","name":"Bean Co","url":"https://example.test"}}</script>
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"Article","headline":"How to brew better coffee at home","author":{{"@type":"Person","name":"Jane Doe"}},"datePublished":"{_RECENT}","dateModified":"{_RECENT}","image":"https://example.test/a.jpg"}}</script>
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"WebSite","name":"Bean Co"}}</script>
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"FAQPage"}}</script>
</head><body><main>
<h1>How to brew better coffee at home</h1>
<p>Coffee is one of the most popular drinks in the world, and making great coffee
at home is simple once you learn a few basics. This guide explains how to make a
brew that tastes fresh, balanced and smooth every single morning without any
expensive equipment or special training at all. Anyone can do it with patience.</p>
<h2>Why does fresh coffee taste better?</h2>
<p>Fresh coffee tastes better because the roasted material keeps its natural oils
and aromas for only a short time after roasting. When you grind just before
brewing, the flavour stays bright and the drink feels far more lively and rich
than stale, pre-ground material poured straight from a bag that has been open.</p>
<p>Good water matters as much as a good roast. Soft, clean water lets the flavour
of the coffee come through clearly without any metallic edge or bitterness. Warm
the mug first so the drink stays hot while you sip it slowly and relax at the
quiet start of the day, before the rush of everything else begins to build up.</p>
<p>Temperature and timing also change the result more than most people expect. If
the water is too hot it scorches the grind, and if it is too cool the flavour
comes out flat and thin. A gentle, steady pour helps the coffee bloom evenly and
release its best character into every single mouthful, as noted by the
<a href="https://nist.gov/coffee">standards reference</a> on extraction.</p>
<p>With a little practice, almost anyone can make coffee that rivals a good local
cafe, as our <a href="/brewing-basics">brewing basics tutorial</a> shows. Keep
gear clean, store the roast away from light, and buy small amounts often so every
cup of coffee is as fresh as it can possibly be for you and those you share with.</p>
</main>
<footer><a href="/privacy">Privacy policy</a><a href="/contact">Contact us</a></footer>
</body></html>
"""

_WEAK = "<html><head><title>x</title></head><body><main><h1>x</h1><p>tiny</p></main></body></html>"


def _make_request(tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e") -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = tenant_id
    return request


class TestRequestValidation:
    def test_neither_source_invalid(self):
        with pytest.raises(ValidationError):
            SeoAuditRequest()

    def test_both_sources_invalid(self):
        with pytest.raises(ValidationError):
            SeoAuditRequest(url="https://x.test", html="<html></html>")

    def test_url_only_ok(self):
        assert SeoAuditRequest(url="https://x.test").url == "https://x.test"

    def test_include_llm_defaults_false(self):
        assert SeoAuditRequest(html="<html></html>").include_llm is False

    def test_new_optin_fields_default_off(self):
        req = SeoAuditRequest(html="<html></html>")
        assert req.competitor_urls == []
        assert req.cross_page_linking is False
        assert req.max_pages == 20

    def test_max_pages_capped_at_50(self):
        with pytest.raises(ValidationError):
            SeoAuditRequest(url="https://x.test", max_pages=100)


class TestAuditSeoRoute:
    async def test_html_input_scores(self):
        result = await audit_seo_route(SeoAuditRequest(html=_STRONG), _make_request())
        assert result.source == "inline-html"
        assert 0.0 <= result.overall_score <= 100.0
        assert result.overall_score >= 85.0
        assert result.grade in {"A", "A+", "B"}
        assert set(result.group_scores) == set(GROUP_WEIGHTS)
        assert result.llm_enriched is False

    async def test_weak_page_scores_lower(self):
        result = await audit_seo_route(SeoAuditRequest(html=_WEAK), _make_request())
        assert result.overall_score < 90.0
        content = result.group_scores["content"]
        # Not `is not None` for tidiness: `content` is a MEASURED group here, so a
        # null would itself be the bug (an unmeasured group must be declared).
        assert content is not None
        assert content < 100.0

    async def test_url_input_fetches_then_scores(self):
        fake_fetch = AsyncMock(return_value=_STRONG)
        body = SeoAuditRequest(url="https://merqi.nl")
        with patch("src.domains.seo_audit.service._default_fetch_html", fake_fetch):
            result = await audit_seo_route(body, _make_request())
        fake_fetch.assert_awaited_once_with("https://merqi.nl")
        assert result.source == "https://merqi.nl"

    async def test_fetch_failure_is_502(self):
        failing = AsyncMock(side_effect=RuntimeError("boom"))
        body = SeoAuditRequest(url="https://merqi.nl")
        with patch("src.domains.seo_audit.service._default_fetch_html", failing):
            with pytest.raises(HTTPException) as exc:
                await audit_seo_route(body, _make_request())
        assert exc.value.status_code == 502

    async def test_competitor_content_gap_enriches_without_moving_score(self):
        """Content-gap opt-in: stub competitor fetches via _default_fetch_html + a
        WMClient returning a gaps payload; the info finding must not move the score.
        """

        async def _message(**kwargs):
            if kwargs["worker_name"] == "seo-content-gap":
                return SimpleNamespace(
                    output={"gaps": [{"topic": "cold brew"}]}, usage_metrics=None
                )
            if kwargs["worker_name"] == "seo-intent-classify":
                return SimpleNamespace(output={"intent": "informational"}, usage_metrics=None)
            return SimpleNamespace(
                output={"experience": "strong", "expertise": "strong"}, usage_metrics=None
            )

        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=_message)
        request = _make_request()
        request.app.state.wm_client = wm

        base = await audit_seo_route(SeoAuditRequest(html=_STRONG), _make_request())
        body = SeoAuditRequest(
            html=_STRONG, include_llm=True, competitor_urls=["https://competitor.test/a"]
        )
        comp_fetch = AsyncMock(return_value=_STRONG)
        with patch("src.domains.seo_audit.service._default_fetch_html", comp_fetch):
            result = await audit_seo_route(body, request)

        comp_fetch.assert_awaited()  # competitor page was fetched via the stub
        assert result.llm_enriched is True
        assert result.overall_score == base.overall_score


class TestAuditSeoService:
    async def test_empty_html_raises(self):
        with pytest.raises(SeoAuditError):
            await audit_seo(html="   ", url=None)

    async def test_quick_wins_and_strategic_are_disjoint_refs(self):
        result = await audit_seo(html=_WEAK, url=None)
        qw = {r["check_id"] for r in result["quick_wins"]}
        strat = {r["check_id"] for r in result["strategic"]}
        assert qw.isdisjoint(strat)

    async def test_cross_page_linking_opt_in_extends_findings(self):
        link_finding = {
            "check_id": "seo-linking-orphan",
            "category": "seo",
            "severity": "medium",
            "title": "Orphaned page",
        }

        async def _ran(*_a: object, observe: dict | None = None, **_k: object) -> list[dict]:
            if observe is not None:
                observe.update({"ran": True, "reason": None, "pages_crawled": 2})
            return [link_finding]

        scan = AsyncMock(side_effect=_ran)
        with patch(
            "src.domains.critique.services.scanners.seo.internal_linking_live.scan_internal_linking_live",
            scan,
        ):
            result = await audit_seo(
                html=None,
                url="https://example.test",
                cross_page_linking=True,
                max_pages=15,
                fetch_html=AsyncMock(return_value=_STRONG),
            )
        scan.assert_awaited_once()
        assert result["group_scores"]["internal_linking"] <= 100.0

    async def test_cross_page_linking_default_off_does_not_crawl(self):
        scan = AsyncMock(return_value=[])
        with patch(
            "src.domains.critique.services.scanners.seo.internal_linking_live.scan_internal_linking_live",
            scan,
        ):
            await audit_seo(
                html=None, url="https://example.test", fetch_html=AsyncMock(return_value=_STRONG)
            )
        scan.assert_not_awaited()
