"""Unit tests for NameStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.name import NameStage


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestNameStage:
    def setup_method(self) -> None:
        self.stage = NameStage()

    def test_name(self) -> None:
        assert self.stage.name == "name"

    def test_should_not_run_without_soup(self) -> None:
        ctx = PipelineContext()
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_extract_from_title(self) -> None:
        ctx = _make_ctx("<html><head><title>Acme Corp | Home</title></head></html>")
        brand = BrandResponse(domain="example.com")
        result = StageResult(stage_name="name")
        await self.stage.run(ctx, brand, result)
        assert brand.name == "Acme Corp"

    @pytest.mark.asyncio
    async def test_extract_from_og_site_name(self) -> None:
        ctx = _make_ctx(
            '<html><head><meta property="og:site_name" content="Cool Co"></head></html>'
        )
        brand = BrandResponse(domain="example.com")
        result = StageResult(stage_name="name")
        await self.stage.run(ctx, brand, result)
        assert brand.name == "Cool Co"

    @pytest.mark.asyncio
    async def test_fallback_to_domain(self) -> None:
        ctx = _make_ctx("<html><head></head></html>")
        brand = BrandResponse(domain="example.com")
        result = StageResult(stage_name="name")
        await self.stage.run(ctx, brand, result)
        assert brand.name == "example.com"

    @pytest.mark.asyncio
    async def test_title_cleaning_dash(self) -> None:
        ctx = _make_ctx("<html><head><title>Brand - Welcome Home</title></head></html>")
        brand = BrandResponse(domain="example.com")
        result = StageResult(stage_name="name")
        await self.stage.run(ctx, brand, result)
        assert brand.name == "Brand"

    @pytest.mark.asyncio
    async def test_title_cleaning_pipe(self) -> None:
        ctx = _make_ctx("<html><head><title>MyBrand | Homepage</title></head></html>")
        brand = BrandResponse(domain="example.com")
        result = StageResult(stage_name="name")
        await self.stage.run(ctx, brand, result)
        assert brand.name == "MyBrand"
