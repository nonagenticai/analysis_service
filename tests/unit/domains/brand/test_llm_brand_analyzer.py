"""Unit tests for llm_brand_analyzer service."""

import json

import pytest

from src.domains.brand.services.llm_brand_analyzer import (
    LLMBrandAnalysis,
    _parse_response,
    _strip_markdown_fences,
    analyze_brand_with_llm,
    build_prompt,
)
from tests.llm_test_doubles import FakeWMClient


class TestStripMarkdownFences:
    def test_no_fences(self) -> None:
        assert _strip_markdown_fences('{"key": "value"}') == '{"key": "value"}'

    def test_json_fences(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        assert _strip_markdown_fences(text) == '{"key": "value"}'

    def test_plain_fences(self) -> None:
        text = '```\n{"key": "value"}\n```'
        assert _strip_markdown_fences(text) == '{"key": "value"}'

    def test_whitespace_around_fences(self) -> None:
        text = '  ```json\n{"key": "value"}\n```  '
        assert _strip_markdown_fences(text) == '{"key": "value"}'


class TestBuildPrompt:
    def test_basic_prompt(self) -> None:
        prompt = build_prompt(page_text="Hello world", domain="example.com")
        assert "example.com" in prompt
        assert "Hello world" in prompt

    def test_all_fields(self) -> None:
        prompt = build_prompt(
            page_text="Content here",
            title="My Site",
            meta_description="A great site",
            headings=["Welcome", "About"],
            domain="example.com",
            schema_org_type="Organization",
        )
        assert "My Site" in prompt
        assert "A great site" in prompt
        assert "Welcome" in prompt
        assert "Organization" in prompt

    def test_truncates_long_text(self) -> None:
        long_text = "x" * 30000
        prompt = build_prompt(page_text=long_text)
        assert len(prompt) < 20000  # Should be truncated to ~16000 chars

    def test_empty_fields_excluded(self) -> None:
        prompt = build_prompt(page_text="content")
        assert "Domain:" not in prompt
        assert "Page title:" not in prompt


class TestParseResponse:
    def test_valid_json(self) -> None:
        data = {
            "brand_voice": "Professional",
            "tone": "Confident",
            "formality_score": 0.8,
            "tagline": "We build things",
            "key_messages": ["msg1", "msg2"],
            "target_audience": "Developers",
            "brand_personality": ["innovative", "bold"],
            "industry": "Technology",
            "sub_industry": "SaaS",
            "industry_confidence": 0.9,
            "tone_confidence": 0.85,
        }
        result = _parse_response(json.dumps(data))
        assert result.brand_voice == "Professional"
        assert result.tone == "Confident"
        assert result.industry == "Technology"
        assert result.formality_score == 0.8
        assert len(result.key_messages) == 2

    def test_partial_json(self) -> None:
        data = {"brand_voice": "Friendly", "tone": "Warm"}
        result = _parse_response(json.dumps(data))
        assert result.brand_voice == "Friendly"
        assert result.industry == ""
        assert result.formality_score == 0.5  # default

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_response("not json")


class TestLLMBrandAnalysis:
    def test_to_content_dict(self) -> None:
        analysis = LLMBrandAnalysis(
            brand_voice="Bold",
            tagline="Just do it",
            key_messages=["msg"],
            target_audience="Athletes",
            tone_confidence=0.9,
        )
        d = analysis.to_content_dict()
        assert d["tagline"] == "Just do it"
        assert d["brand_voice"] == "Bold"
        assert d["source"] == "llm_analysis"

    def test_to_tone_dict(self) -> None:
        analysis = LLMBrandAnalysis(tone="Confident", formality_score=0.7, tone_confidence=0.8)
        d = analysis.to_tone_dict()
        assert d["tone"] == "Confident"
        assert d["formality"] == 0.7
        assert d["source"] == "llm_analysis"

    def test_to_industry_dict(self) -> None:
        analysis = LLMBrandAnalysis(industry="Tech", sub_industry="SaaS", industry_confidence=0.95)
        d = analysis.to_industry_dict()
        assert d["industry"] == "Tech"
        assert d["sub_industry"] == "SaaS"
        assert d["source"] == "llm_analysis"


class TestAnalyzeBrandWithLLM:
    @pytest.mark.asyncio
    async def test_calls_wm_and_parses_dict_output(self) -> None:
        wm = FakeWMClient(
            outputs=[
                {
                    "brand_voice": "Professional",
                    "tone": "Authoritative",
                    "formality_score": 0.8,
                    "tagline": "Test tagline",
                    "key_messages": [],
                    "target_audience": "Devs",
                    "brand_personality": ["smart"],
                    "industry": "Tech",
                    "sub_industry": "SaaS",
                    "industry_confidence": 0.9,
                    "tone_confidence": 0.85,
                }
            ]
        )

        result = await analyze_brand_with_llm(wm, "test prompt")
        assert result.brand_voice == "Professional"
        assert result.industry == "Tech"
        assert len(wm.calls) == 1
        assert wm.calls[0].worker_name == "brand-analysis"

    @pytest.mark.asyncio
    async def test_raises_on_wm_error(self) -> None:
        wm = FakeWMClient(raise_after="always", error=RuntimeError("API error"))

        with pytest.raises(RuntimeError, match="API error"):
            await analyze_brand_with_llm(wm, "test prompt")

    @pytest.mark.asyncio
    async def test_handles_markdown_fenced_string_response(self) -> None:
        wm = FakeWMClient(
            outputs=[
                '```json\n{"brand_voice": "Casual", "tone": "Friendly"}\n```',
            ]
        )

        result = await analyze_brand_with_llm(wm, "test prompt")
        assert result.brand_voice == "Casual"
        assert result.tone == "Friendly"

    @pytest.mark.asyncio
    async def test_passes_model_override(self) -> None:
        wm = FakeWMClient(outputs=[{"brand_voice": "x", "tone": "y", "industry": "z"}])
        await analyze_brand_with_llm(wm, "p", model_override="custom-model")
        assert wm.calls[0].model_override == "custom-model"
