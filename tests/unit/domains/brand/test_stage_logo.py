"""Unit tests for LogoStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.logo import LogoStage


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestLogoStage:
    def setup_method(self) -> None:
        self.stage = LogoStage()

    def test_name(self) -> None:
        assert self.stage.name == "logo"

    @pytest.mark.asyncio
    async def test_extract_og_image(self) -> None:
        html = '<html><head><meta property="og:image" content="https://example.com/og.png"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="logo")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_url == "https://example.com/og.png"
        assert brand.has_logo is True

    @pytest.mark.asyncio
    async def test_extract_favicon(self) -> None:
        html = '<html><head><link rel="icon" href="/favicon.ico"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="logo")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_url == "https://example.com/favicon.ico"

    @pytest.mark.asyncio
    async def test_extract_img_with_logo_class(self) -> None:
        html = '<html><body><img class="logo" src="https://example.com/logo.svg"></body></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="logo")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_url == "https://example.com/logo.svg"

    @pytest.mark.asyncio
    async def test_no_logo_found(self) -> None:
        ctx = _make_ctx("<html><body></body></html>")
        brand = BrandResponse()
        result = StageResult(stage_name="logo")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_url == ""
        assert brand.has_logo is False
