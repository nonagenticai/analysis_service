"""Unit tests for FontStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.fonts import FontStage, categorize_font


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestCategorizFont:
    def test_sans_serif(self) -> None:
        assert categorize_font("Roboto") == "sans-serif"

    def test_serif(self) -> None:
        assert categorize_font("Georgia") == "serif"

    def test_monospace(self) -> None:
        assert categorize_font("Fira Code") == "monospace"

    def test_handwriting(self) -> None:
        assert categorize_font("Dancing Script") == "handwriting"

    def test_display(self) -> None:
        assert categorize_font("Lobster") == "display"


class TestFontStage:
    def setup_method(self) -> None:
        self.stage = FontStage()

    def test_name(self) -> None:
        assert self.stage.name == "fonts"

    def test_should_not_run_when_fonts_disabled(self) -> None:
        from src.domains.brand.services.extraction_config import BrandExtractConfig

        ctx = PipelineContext(config=BrandExtractConfig(extract_fonts=False))
        ctx.soup = BeautifulSoup("<html></html>", "html.parser")
        assert self.stage.should_run(ctx) is False

    def test_should_not_run_when_include_fonts_false(self) -> None:
        ctx = PipelineContext(include_fonts=False)
        ctx.soup = BeautifulSoup("<html></html>", "html.parser")
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_extracts_google_fonts(self) -> None:
        html = '<html><head><link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;700" rel="stylesheet"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="fonts")
        await self.stage.run(ctx, brand, result)
        assert len(brand.fonts) >= 1
        assert any(f.family == "Roboto" for f in brand.fonts)

    @pytest.mark.asyncio
    async def test_extracts_inline_fonts(self) -> None:
        html = """<html><head><style>
            body { font-family: 'Open Sans', sans-serif; }
        </style></head></html>"""
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="fonts")
        await self.stage.run(ctx, brand, result)
        assert len(brand.fonts) >= 1
        assert any(f.family == "Open Sans" for f in brand.fonts)
