"""Unit tests for the opt-in SEO micro-LLM analyzers (mocked WMClient, no network).

Covers: structured-output coercion, WM errors propagating from the analyzer
classes but coming back as REPORTED ``failed`` outcomes from the runners, and the
invariant that LLM output lands in ``ai.annotations[]`` — never in the scored
findings — **without moving the deterministic score**.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo.keyword import top_keywords
from src.domains.critique.services.scanners.seo.seo_llm import (
    SeoContentGapAnalyzer,
    SeoEeatDepthAnalyzer,
    SeoIntentClassifier,
    enrich_seo_audit,
)

_BODY = " ".join(
    [
        "Cloud backup keeps your files safe across devices and disasters.",
        "This cloud backup guide explains how modern backup works and why it matters.",
        "A good cloud backup runs automatically, encrypts data, and restores quickly.",
    ]
    * 8
)
_HTML = f"<html><head><title>Cloud backup guide</title></head><body><main><h1>Cloud backup</h1><p>{_BODY}</p></main></body></html>"


def _wm_returning(output: dict) -> AsyncMock:
    client = AsyncMock()
    client.message = AsyncMock(return_value=SimpleNamespace(output=output, usage_metrics=None))
    return client


class TestIntentClassifier:
    async def test_valid_intent_parsed(self):
        wm = _wm_returning({"intent": "informational", "confidence": 0.9})
        result = await SeoIntentClassifier(wm).classify("cloud backup", "Cloud backup guide")
        assert result == {"intent": "informational", "confidence": 0.9}

    async def test_invalid_intent_rejected(self):
        wm = _wm_returning({"intent": "banana"})
        assert await SeoIntentClassifier(wm).classify("x", "y") is None

    async def test_wm_error_propagates_so_the_caller_can_report_it(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=RuntimeError("wm down"))
        with pytest.raises(RuntimeError, match="wm down"):
            await SeoIntentClassifier(wm).classify("x", "y")


class TestEeatDepthAnalyzer:
    async def test_valid_depth_parsed(self):
        wm = _wm_returning({"experience": "weak", "expertise": "present", "evidence": "e"})
        result = await SeoEeatDepthAnalyzer(wm).analyze("some copy", "present")
        assert result is not None
        assert result["experience"] == "weak"

    async def test_missing_fields_rejected(self):
        wm = _wm_returning({"experience": "strong"})  # expertise missing
        assert await SeoEeatDepthAnalyzer(wm).analyze("x", "") is None


class TestEnrichSeoAudit:
    async def test_ok_outcomes_carry_structured_annotations(self):
        wm = AsyncMock()

        async def _message(**kwargs):
            if kwargs["worker_name"] == "seo-intent-classify":
                return SimpleNamespace(output={"intent": "commercial"}, usage_metrics=None)
            return SimpleNamespace(
                output={"experience": "weak", "expertise": "weak", "evidence": "thin"},
                usage_metrics=None,
            )

        wm.message = AsyncMock(side_effect=_message)
        soup = BeautifulSoup(_HTML, "lxml")
        outcomes = await enrich_seo_audit(soup, wm_client=wm)
        by_layer = {o.layer: o for o in outcomes}
        assert by_layer["seo_intent"].status == "ok"
        assert by_layer["seo_eeat"].status == "ok"
        intent = by_layer["seo_intent"].annotation
        assert intent is not None
        assert intent["kind"] == "search_intent"
        assert intent["data"]["intent"] == "commercial"
        assert intent["data"]["keyword"] == "backup"
        eeat = by_layer["seo_eeat"].annotation
        assert eeat is not None
        assert eeat["kind"] == "eeat_depth"
        assert eeat["data"]["experience"] == "weak"

    async def test_wm_failure_is_reported_not_swallowed(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=RuntimeError("down"))
        soup = BeautifulSoup(_HTML, "lxml")
        outcomes = await enrich_seo_audit(soup, wm_client=wm)
        assert {o.layer: o.status for o in outcomes} == {
            "seo_intent": "failed",
            "seo_eeat": "failed",
        }
        assert all(isinstance(o.error, RuntimeError) for o in outcomes)
        assert all(o.annotation is None for o in outcomes)

    async def test_thin_page_skips_by_design_without_calling_wm(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=AssertionError("must not be called"))
        soup = BeautifulSoup("<html><body><main><p>short</p></main></body></html>", "lxml")
        outcomes = await enrich_seo_audit(soup, wm_client=wm)
        assert {o.layer: o.status for o in outcomes} == {
            "seo_intent": "skipped",
            "seo_eeat": "skipped",
        }
        assert all(o.by_design for o in outcomes)
        wm.message.assert_not_awaited()


class TestTopKeywords:
    def test_returns_top_terms_by_frequency(self):
        soup = BeautifulSoup(_HTML, "lxml")
        kws = top_keywords(soup, n=5)
        assert kws  # non-empty on substantial content
        assert len(kws) <= 5
        # "backup" is the dominant repeated term in _BODY.
        assert "backup" in kws
        assert all(isinstance(k, str) for k in kws)

    def test_thin_content_returns_empty(self):
        soup = BeautifulSoup("<html><body><main><p>too short</p></main></body></html>", "lxml")
        assert top_keywords(soup) == []


class TestContentGapAnalyzer:
    async def test_valid_gaps_parsed(self):
        wm = _wm_returning(
            {"gaps": [{"topic": "cold storage", "covered_by": ["A"], "rationale": "r"}]}
        )
        result = await SeoContentGapAnalyzer(wm).analyze(
            ["backup"], [("https://comp.test", ["cold", "storage"])], "Cloud backup guide"
        )
        assert result is not None
        assert result["gaps"][0]["topic"] == "cold storage"

    async def test_fabricated_gaps_without_topic_dropped(self):
        # A gap with no usable topic string is filtered; nothing survives -> None.
        wm = _wm_returning({"gaps": [{"covered_by": ["A"]}, {"topic": "   "}]})
        assert await SeoContentGapAnalyzer(wm).analyze(["backup"], [("u", ["x"])], "t") is None

    async def test_empty_gap_list_is_a_result_not_malformed(self):
        wm = _wm_returning({"gaps": []})
        assert await SeoContentGapAnalyzer(wm).analyze(["backup"], [("u", ["x"])], "t") == {
            "gaps": []
        }

    async def test_wm_error_propagates(self):
        wm = AsyncMock()
        wm.message = AsyncMock(side_effect=RuntimeError("wm down"))
        with pytest.raises(RuntimeError):
            await SeoContentGapAnalyzer(wm).analyze(["backup"], [("u", ["x"])], "t")


class TestScoreUnaffectedByLlm:
    async def test_deterministic_score_identical_with_and_without_llm(self):
        from src.domains.seo_audit.service import audit_seo

        wm = AsyncMock()

        async def _message(**kwargs):
            if kwargs["worker_name"] == "seo-intent-classify":
                return SimpleNamespace(output={"intent": "informational"}, usage_metrics=None)
            return SimpleNamespace(
                output={"experience": "strong", "expertise": "strong"}, usage_metrics=None
            )

        wm.message = AsyncMock(side_effect=_message)

        base = await audit_seo(html=_HTML, url=None, include_llm=False)
        enriched = await audit_seo(html=_HTML, url=None, include_llm=True, wm_client=wm)

        assert enriched["llm_enriched"] is True
        assert enriched["overall_score"] == base["overall_score"]
        assert enriched["group_scores"] == base["group_scores"]
        kinds = {a["kind"] for a in enriched["ai"]["annotations"]}
        assert kinds == {"search_intent", "eeat_depth"}
        # SEO-G7: the LLM output is NOT a finding any more.
        assert enriched["findings"] == base["findings"]


class TestContentGapWiring:
    async def test_run_content_gap_returns_annotated_outcome(self):
        from src.domains.seo_audit.service import _run_content_gap

        wm = _wm_returning({"gaps": [{"topic": "cold storage"}]})
        soup = BeautifulSoup(_HTML, "lxml")
        comp_fetch = AsyncMock(return_value=_HTML)
        outcome = await _run_content_gap(
            soup=soup,
            competitor_urls=["https://comp.test/a"],
            fetcher=comp_fetch,
            wm_client=wm,
            model_override=None,
        )
        assert outcome.layer == "content_gap"
        assert outcome.status == "ok"
        assert outcome.annotation is not None
        assert outcome.annotation["kind"] == "content_gap"
        assert outcome.annotation["data"]["gaps"] == [{"topic": "cold storage"}]
        assert outcome.annotation["data"]["competitors"] == ["https://comp.test/a"]

    async def test_dead_competitors_are_a_degraded_skip(self):
        from src.domains.seo_audit.service import _run_content_gap

        wm = _wm_returning({"gaps": [{"topic": "cold storage"}]})
        outcome = await _run_content_gap(
            soup=BeautifulSoup(_HTML, "lxml"),
            competitor_urls=["https://comp.test/a"],
            fetcher=AsyncMock(side_effect=RuntimeError("dead")),
            wm_client=wm,
            model_override=None,
        )
        assert outcome.status == "skipped"
        assert outcome.by_design is False
        wm.message.assert_not_awaited()

    async def test_content_gap_is_score_neutral(self):
        from src.domains.seo_audit.service import audit_seo

        wm = AsyncMock()

        async def _message(**kwargs):
            if kwargs["worker_name"] == "seo-content-gap":
                return SimpleNamespace(
                    output={"gaps": [{"topic": "cold storage"}]}, usage_metrics=None
                )
            if kwargs["worker_name"] == "seo-intent-classify":
                return SimpleNamespace(output={"intent": "informational"}, usage_metrics=None)
            return SimpleNamespace(
                output={"experience": "strong", "expertise": "strong"}, usage_metrics=None
            )

        wm.message = AsyncMock(side_effect=_message)
        comp_fetch = AsyncMock(return_value=_HTML)

        base = await audit_seo(html=_HTML, url=None, include_llm=False)
        enriched = await audit_seo(
            html=_HTML,
            url=None,
            include_llm=True,
            wm_client=wm,
            competitor_urls=["https://comp.test/a"],
            fetch_html=comp_fetch,
        )

        assert enriched["llm_enriched"] is True
        assert enriched["overall_score"] == base["overall_score"]
        assert enriched["group_scores"] == base["group_scores"]
        assert "seo-content-gap" not in {f["check_id"] for f in enriched["findings"]}
        kinds = {a["kind"] for a in enriched["ai"]["annotations"]}
        assert {"search_intent", "eeat_depth", "content_gap"} <= kinds
