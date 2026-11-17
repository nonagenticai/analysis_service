"""Unit tests for the synchronous marketing scorecard route + service.

Routes are called directly with a mock ``Request`` (the cro/seo_audit house
style). No live TestClient, no HTTP traffic, no LLM/DB. The fetch is faked for
URL input; HTML input runs the real deterministic scanners (CRO + SEO + marketing).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.domains.marketing.routes import score_marketing_route
from src.domains.marketing.schemas import MarketingScorecardRequest
from src.domains.marketing.service import MarketingScoreError, score_marketing

# A page that presents well across all six dimensions: differentiation + comparison
# + reputation (competitive), schema/og/favicon/logo/about (brand), pricing/referral/
# newsletter (growth), plus strong CRO + SEO signals.
_STRONG = """
<html lang="en"><head>
<title>Merqi — the only autonomous marketing platform for SMBs</title>
<meta name="description" content="Merqi is the only fully autonomous marketing and sales platform. Book a demo to cut CAC by 40%.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:image" content="https://merqi.nl/og.png">
<link rel="icon" href="/favicon.ico">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Merqi"}</script>
</head>
<body>
<header><img class="logo" alt="Merqi logo" src="/logo.svg"><nav><a href="/about">About</a><a href="/pricing">Pricing</a></nav></header>
<h1>The only autonomous marketing platform that cuts CAC by 40%</h1>
<h2>Unlike agencies, Merqi runs your whole funnel</h2>
<ul><li>Fast</li><li>Secure</li><li>12,000+ customers</li></ul>
<a class="btn" href="/demo">Start my free trial</a><p>No credit card required.</p>
<section><h2>Merqi vs traditional agencies</h2><table><tr><td>Merqi</td><td>Agency</td></tr></table></section>
<p>As featured in Forbes and rated 4.8 on G2.</p>
<blockquote>"Cut our CAC 40%" — Jane Doe, CEO at Foo</blockquote>
<div class="logos"><img><img><img></div><div class="rating">★★★★★</div>
<section id="pricing"><h2>Pricing</h2><p>$99 / month per seat</p></section>
<p>Refer a friend and earn credit. <a href="/newsletter">Subscribe to our newsletter</a>.</p>
<details><summary>FAQ</summary>a</details><p>Money-back guarantee <a href="/privacy">Privacy</a></p>
<footer><a class="btn" href="/demo">Start</a><a href="mailto:h@a.co">Contact</a></footer>
</body></html>
"""

_WEAK = "<html><body><p>we do stuff</p></body></html>"
_NO_SNIPPETS = "<html><body><div>just a div</div></body></html>"

_DIMENSIONS = {"content", "conversion", "seo", "competitive", "brand", "growth"}

_JUDGE_OUTPUT = {
    "judgments": [
        {"element": "headline", "score": 82, "assessment": "Specific.", "suggestion": "Keep."},
        {"element": "value_prop", "score": 71, "assessment": "Clear.", "suggestion": "Tighten."},
        {"element": "bogus", "score": 99, "assessment": "drop me", "suggestion": "drop"},
    ]
}


def _make_request(
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", wm_client: object | None = None
) -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = tenant_id
    request.app.state.wm_client = wm_client
    return request


def _wm_returning(output: object, usage: dict | None = None) -> AsyncMock:
    wm = AsyncMock()
    wm.message = AsyncMock(return_value=SimpleNamespace(output=output, usage_metrics=usage))
    return wm


class TestRequestValidation:
    def test_neither_source_invalid(self):
        with pytest.raises(ValidationError):
            MarketingScorecardRequest()

    def test_both_sources_invalid(self):
        with pytest.raises(ValidationError):
            MarketingScorecardRequest(url="https://x.test", html="<html></html>")

    def test_url_only_ok(self):
        assert MarketingScorecardRequest(url="https://x.test").url == "https://x.test"

    def test_include_llm_defaults_false(self):
        assert MarketingScorecardRequest(html="<html></html>").include_llm is False


class TestScoreMarketingRoute:
    async def test_html_input_scores_all_dimensions(self):
        result = await score_marketing_route(
            MarketingScorecardRequest(html=_STRONG), _make_request()
        )
        assert result.source == "inline-html"
        assert 0.0 <= result.overall_score <= 100.0
        assert result.grade in {"A+", "A", "B", "C", "D", "F"}
        assert set(result.dimension_scores) == _DIMENSIONS
        assert {d.dimension for d in result.dimensions} == _DIMENSIONS
        assert result.llm_enriched is False

    async def test_strong_page_beats_weak_page(self):
        strong = await score_marketing_route(
            MarketingScorecardRequest(html=_STRONG), _make_request()
        )
        weak = await score_marketing_route(MarketingScorecardRequest(html=_WEAK), _make_request())
        assert strong.overall_score > weak.overall_score
        # The weak page fails several go-to-market dimensions outright.
        assert weak.dimension_scores["competitive"] < 100.0
        assert weak.dimension_scores["growth"] < 100.0
        assert weak.dimension_scores["brand"] < 100.0

    async def test_url_input_fetches_then_scores(self):
        fake_fetch = AsyncMock(return_value=_STRONG)
        body = MarketingScorecardRequest(url="https://merqi.nl")
        with patch("src.domains.marketing.service._default_fetch_html", fake_fetch):
            result = await score_marketing_route(body, _make_request())
        fake_fetch.assert_awaited_once_with("https://merqi.nl")
        assert result.source == "https://merqi.nl"

    async def test_fetch_failure_is_502(self):
        failing = AsyncMock(side_effect=RuntimeError("boom"))
        body = MarketingScorecardRequest(url="https://merqi.nl")
        with patch("src.domains.marketing.service._default_fetch_html", failing):
            with pytest.raises(HTTPException) as exc:
                await score_marketing_route(body, _make_request())
        assert exc.value.status_code == 502


class TestScoreMarketingService:
    async def test_empty_html_raises(self):
        with pytest.raises(MarketingScoreError):
            await score_marketing(html="   ", url=None)

    async def test_quick_wins_and_strategic_are_disjoint(self):
        result = await score_marketing(html=_WEAK, url=None)
        qw = {r["check_id"] for r in result["quick_wins"]}
        strat = {r["check_id"] for r in result["strategic"]}
        assert qw.isdisjoint(strat)

    async def test_composite_matches_weighted_dimensions(self):
        from src.domains.critique.services.scanners.marketing import DIMENSION_WEIGHTS

        result = await score_marketing(html=_STRONG, url=None)
        expected = round(
            sum(result["dimension_scores"][d] * w for d, w in DIMENSION_WEIGHTS.items())
            / sum(DIMENSION_WEIGHTS.values()),
            1,
        )
        assert result["overall_score"] == expected


_DETERMINISTIC_KEYS = (
    "overall_score",
    "grade",
    "dimension_scores",
    "dimensions",
    "quick_wins",
    "strategic",
)


class TestLlmEnrichment:
    async def test_include_llm_enriches_without_changing_score(self):
        wm = _wm_returning(_JUDGE_OUTPUT, usage={"total_tokens": 42})
        plain = await score_marketing(html=_STRONG, url=None, include_llm=False)
        enriched = await score_marketing(html=_STRONG, url=None, include_llm=True, wm_client=wm)

        for key in _DETERMINISTIC_KEYS:
            assert enriched[key] == plain[key], f"deterministic key drifted: {key}"
        assert enriched["llm_enriched"] is True
        assert {a["element"] for a in enriched["llm_assessments"]} == {"headline", "value_prop"}
        assert all(a["dimension"] == "content" for a in enriched["llm_assessments"])
        wm.message.assert_awaited_once()

    async def test_llm_failure_is_fail_soft(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=RuntimeError("wm down"))
        plain = await score_marketing(html=_STRONG, url=None, include_llm=False)
        result = await score_marketing(html=_STRONG, url=None, include_llm=True, wm_client=wm)
        assert result["llm_enriched"] is False
        assert result["llm_assessments"] == []
        for key in _DETERMINISTIC_KEYS:
            assert result[key] == plain[key]

    async def test_no_extractable_snippets_skips_llm(self):
        wm = AsyncMock()
        wm.message = AsyncMock()
        result = await score_marketing(html=_NO_SNIPPETS, url=None, include_llm=True, wm_client=wm)
        assert result["llm_enriched"] is False
        wm.message.assert_not_awaited()

    async def test_route_default_path_has_no_llm(self):
        result = await score_marketing_route(
            MarketingScorecardRequest(html=_STRONG), _make_request()
        )
        assert result.llm_enriched is False
        assert result.llm_assessments == []
