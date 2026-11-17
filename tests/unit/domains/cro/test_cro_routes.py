"""Unit tests for the synchronous CRO score route + service.

Routes are called directly with a mock ``Request`` (the artifact_score/brand
house style). No live TestClient, no HTTP traffic, no LLM/DB. The fetch is faked
for URL input; HTML input runs the real deterministic scanner.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.domains.cro.llm_judge import CRO_JUDGE_OUTPUT_SCHEMA, judge_cro_snippets
from src.domains.cro.routes import score_cro_route
from src.domains.cro.schemas import CroScoreRequest
from src.domains.cro.service import CroScoreError, score_cro

_STRONG = """
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><header><nav><a>Home</a></nav></header>
<h1>Cut onboarding time by 40%</h1><h2>Why</h2>
<ul><li>Fast</li><li>Secure</li><li>12,000+ customers</li></ul>
<a class="btn" href="/x">Start my free trial</a><p>No credit card required.</p>
<blockquote>"Great" — Jane Doe, CEO at Foo</blockquote>
<div class="logos"><img><img><img></div><div class="rating">★★★★★</div>
<details><summary>FAQ</summary>a</details><p>Money-back guarantee <a href="/privacy">Privacy</a></p>
<footer><a class="btn" href="/x">Start</a><a href="mailto:h@a.co">Contact</a></footer>
</body></html>
"""

_WEAK = "<html><body><p>no headline</p></body></html>"
# No h1 / h2 / p / blockquote / testimonial-class → no snippet is extractable.
_NO_SNIPPETS = "<html><body><div>just a div</div></body></html>"


def _make_request(
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", wm_client: object | None = None
) -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = tenant_id
    request.app.state.wm_client = wm_client
    return request


def _wm_returning(output: object, usage: dict | None = None) -> AsyncMock:
    """An AsyncMock WMClient whose ``message`` returns a WMResponse-like object."""
    wm = AsyncMock()
    wm.message = AsyncMock(return_value=SimpleNamespace(output=output, usage_metrics=usage))
    return wm


_JUDGE_OUTPUT = {
    "judgments": [
        {"element": "headline", "score": 82, "assessment": "Specific.", "suggestion": "Keep."},
        {
            "element": "testimonial",
            "score": 70,
            "assessment": "Named.",
            "suggestion": "Add result.",
        },
        {"element": "bogus", "score": 99, "assessment": "drop me", "suggestion": "drop"},
    ]
}


class TestRequestValidation:
    def test_neither_source_invalid(self):
        with pytest.raises(ValidationError):
            CroScoreRequest()

    def test_both_sources_invalid(self):
        with pytest.raises(ValidationError):
            CroScoreRequest(url="https://x.test", html="<html></html>")

    def test_url_only_ok(self):
        assert CroScoreRequest(url="https://x.test").url == "https://x.test"

    def test_html_only_ok(self):
        assert CroScoreRequest(html="<html></html>").html == "<html></html>"

    def test_include_llm_defaults_false(self):
        assert CroScoreRequest(html="<html></html>").include_llm is False


class TestScoreCroRoute:
    async def test_html_input_scores(self):
        result = await score_cro_route(CroScoreRequest(html=_STRONG), _make_request())
        assert result.source == "inline-html"
        assert 0.0 <= result.overall_score <= 100.0
        assert result.overall_score >= 90.0
        assert result.grade in {"A", "A+"}
        assert set(result.subsection_scores) == {
            "form",
            "hero",
            "cta",
            "social",
            "valueprop",
            "objection",
            "tech",
            "footer",
        }
        assert result.llm_enriched is False

    async def test_weak_page_scores_lower_with_findings(self):
        result = await score_cro_route(CroScoreRequest(html=_WEAK), _make_request())
        assert result.overall_score < 90.0
        # A page with no H1 / no CTA yields strategic-bucket items.
        hero = result.subsection_scores["hero"]
        assert hero is not None
        assert hero < 100.0

    async def test_url_input_fetches_then_scores(self):
        fake_fetch = AsyncMock(return_value=_STRONG)
        body = CroScoreRequest(url="https://merqi.nl")
        with patch("src.domains.cro.service._default_fetch_html", fake_fetch):
            result = await score_cro_route(body, _make_request())
        fake_fetch.assert_awaited_once_with("https://merqi.nl")
        assert result.source == "https://merqi.nl"

    async def test_fetch_failure_is_502(self):
        failing = AsyncMock(side_effect=RuntimeError("boom"))
        body = CroScoreRequest(url="https://merqi.nl")
        with patch("src.domains.cro.service._default_fetch_html", failing):
            with pytest.raises(HTTPException) as exc:
                await score_cro_route(body, _make_request())
        assert exc.value.status_code == 502


class TestScoreCroService:
    async def test_empty_html_raises(self):
        # The route/schema block empty html, but the service is defensive too.
        with pytest.raises(CroScoreError):
            await score_cro(html="   ", url=None)

    async def test_quick_wins_and_strategic_are_disjoint_refs(self):
        result = await score_cro(html=_WEAK, url=None)
        qw = {r["check_id"] for r in result["quick_wins"]}
        strat = {r["check_id"] for r in result["strategic"]}
        assert qw.isdisjoint(strat)


_DETERMINISTIC_KEYS = (
    "overall_score",
    "grade",
    "subsection_scores",
    "subsections",
    "quick_wins",
    "strategic",
)


class TestLlmEnrichment:
    async def test_include_llm_enriches_without_changing_score(self):
        wm = _wm_returning(_JUDGE_OUTPUT, usage={"total_tokens": 42})
        plain = await score_cro(html=_STRONG, url=None, include_llm=False)
        enriched = await score_cro(html=_STRONG, url=None, include_llm=True, wm_client=wm)

        # The deterministic score is byte-identical with LLM on vs off.
        for key in _DETERMINISTIC_KEYS:
            assert enriched[key] == plain[key], f"deterministic key drifted: {key}"

        assert enriched["llm_enriched"] is True
        # The bogus element is dropped; only known elements survive.
        elements = {j["element"] for j in enriched["llm_judgments"]}
        assert elements == {"headline", "testimonial"}
        wm.message.assert_awaited_once()

    async def test_llm_failure_is_fail_soft(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=RuntimeError("wm down"))
        plain = await score_cro(html=_STRONG, url=None, include_llm=False)
        result = await score_cro(html=_STRONG, url=None, include_llm=True, wm_client=wm)

        assert result["llm_enriched"] is False
        assert result["llm_judgments"] == []
        for key in _DETERMINISTIC_KEYS:
            assert result[key] == plain[key]

    async def test_no_extractable_snippets_skips_llm(self):
        wm = AsyncMock()
        wm.message = AsyncMock()
        result = await score_cro(html=_NO_SNIPPETS, url=None, include_llm=True, wm_client=wm)
        assert result["llm_enriched"] is False
        assert result["llm_judgments"] == []
        wm.message.assert_not_awaited()

    async def test_route_passes_wm_client_and_enriches(self):
        wm = _wm_returning(_JUDGE_OUTPUT)
        body = CroScoreRequest(html=_STRONG, include_llm=True)
        result = await score_cro_route(body, _make_request(wm_client=wm))
        assert result.llm_enriched is True
        assert {j.element for j in result.llm_judgments} == {"headline", "testimonial"}
        # A fail-closed 502 is still reserved for fetch/scan failure, not LLM failure.
        assert result.overall_score >= 90.0

    async def test_route_default_path_has_no_llm(self):
        # include_llm defaults False → response_model_exclude_unset keeps llm_judgments out,
        # and the deterministic body is unchanged from Phase 1/2.
        result = await score_cro_route(CroScoreRequest(html=_STRONG), _make_request())
        assert result.llm_enriched is False
        assert result.llm_judgments == []


class TestJudgeCroSnippets:
    async def test_maps_output_and_usage_and_drops_unknown(self):
        wm = _wm_returning(
            _JUDGE_OUTPUT,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
        snippets = {"headline": "Cut onboarding time by 40%", "testimonial": "Great — Jane, CEO"}
        judgments, usage = await judge_cro_snippets(wm, snippets)

        # The structured schema is passed and the worker name is pinned.
        _, kwargs = wm.message.call_args
        assert kwargs["output_schema"] is CRO_JUDGE_OUTPUT_SCHEMA
        assert kwargs["worker_name"] == "cro-micro-judge"

        assert {j["element"] for j in judgments} == {"headline", "testimonial"}
        assert all(0.0 <= j["score"] <= 100.0 for j in judgments)
        assert usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    async def test_string_json_output_is_parsed(self):
        wm = _wm_returning('```json\n{"judgments": [{"element": "headline", "score": 50}]}\n```')
        judgments, usage = await judge_cro_snippets(wm, {"headline": "x"})
        assert judgments == [
            {"element": "headline", "score": 50.0, "assessment": "", "suggestion": ""}
        ]
        assert usage is None
