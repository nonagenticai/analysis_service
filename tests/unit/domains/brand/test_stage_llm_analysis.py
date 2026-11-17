"""Unit tests for LLMBrandAnalysisStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.llm_analysis import LLMBrandAnalysisStage
from tests.llm_test_doubles import FakeWMClient


def _make_ctx(
    html: str = "<html><head><title>Test</title></head><body>Hello</body></html>",
) -> PipelineContext:
    soup = BeautifulSoup(html, "lxml")
    return PipelineContext(
        url="https://example.com",
        soup=soup,
        html=html,
        page_text="Hello world content",
    )


def _make_fake_wm(response_data: dict | None = None) -> FakeWMClient:
    if response_data is None:
        response_data = {
            "brand_voice": "Professional",
            "tone": "Confident",
            "formality_score": 0.8,
            "tagline": "Test tagline",
            "key_messages": ["msg1"],
            "target_audience": "Developers",
            "brand_personality": ["innovative"],
            "industry": "Technology",
            "sub_industry": "SaaS",
            "industry_confidence": 0.9,
            "tone_confidence": 0.85,
        }
    return FakeWMClient(outputs=[response_data])


class TestLLMBrandAnalysisStage:
    def setup_method(self) -> None:
        self.stage = LLMBrandAnalysisStage()

    def test_name(self) -> None:
        assert self.stage.name == "llm_analysis"

    def test_should_run_with_soup(self) -> None:
        ctx = _make_ctx()
        assert self.stage.should_run(ctx) is True

    def test_should_not_run_without_soup(self) -> None:
        ctx = PipelineContext()
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_raises_without_wm_client(self) -> None:
        ctx = _make_ctx()
        ctx.wm_client = None
        brand = BrandResponse()
        result = StageResult(stage_name="llm_analysis")

        with pytest.raises(RuntimeError, match="WMClient not configured"):
            await self.stage.run(ctx, brand, result)

    @pytest.mark.asyncio
    async def test_populates_brand_fields(self) -> None:
        ctx = _make_ctx()
        ctx.wm_client = _make_fake_wm()
        brand = BrandResponse()
        result = StageResult(stage_name="llm_analysis")

        await self.stage.run(ctx, brand, result)

        assert brand.tagline == "Test tagline"
        assert brand.brand_voice == "Professional"
        assert brand.tone_result is not None
        assert brand.tone_result["tone"] == "Confident"
        assert brand.tone_result["source"] == "llm_analysis"
        assert brand.content_analysis is not None
        assert brand.content_analysis["source"] == "llm_analysis"
        assert brand.industry is not None
        assert brand.industry["industry"] == "Technology"
        assert brand.brand_personality == ["innovative"]

    @pytest.mark.asyncio
    async def test_records_extractions(self) -> None:
        ctx = _make_ctx()
        ctx.wm_client = _make_fake_wm()
        brand = BrandResponse()
        result = StageResult(stage_name="llm_analysis")

        await self.stage.run(ctx, brand, result)

        methods = ctx.tracker.get_methods_used()
        assert "llm_analysis" in methods

    @pytest.mark.asyncio
    async def test_uses_page_text_from_context(self) -> None:
        ctx = _make_ctx()
        ctx.page_text = "Custom page text content"
        wm = _make_fake_wm()
        ctx.wm_client = wm
        brand = BrandResponse()
        result = StageResult(stage_name="llm_analysis")

        await self.stage.run(ctx, brand, result)

        assert len(wm.calls) == 1
        assert "Custom page text content" in wm.calls[0].message

    @pytest.mark.asyncio
    async def test_error_propagates(self) -> None:
        ctx = _make_ctx()
        ctx.wm_client = FakeWMClient(raise_after="always", error=RuntimeError("API down"))
        brand = BrandResponse()

        result = await self.stage.execute(ctx, brand)

        assert result.success is False
        assert len(result.errors) == 1
