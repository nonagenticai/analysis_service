"""Unit tests for MetadataStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.metadata import MetadataStage


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestMetadataStage:
    def setup_method(self) -> None:
        self.stage = MetadataStage()

    def test_name(self) -> None:
        assert self.stage.name == "metadata"

    @pytest.mark.asyncio
    async def test_extract_favicons(self) -> None:
        html = '<html><head><link rel="icon" href="/favicon.ico" sizes="32x32"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="metadata")
        await self.stage.run(ctx, brand, result)
        assert len(brand.favicon_urls) == 1
        assert "favicon.ico" in brand.favicon_urls[0]

    @pytest.mark.asyncio
    async def test_extract_social_images(self) -> None:
        html = '<html><head><meta property="og:image" content="https://example.com/og.png"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="metadata")
        await self.stage.run(ctx, brand, result)
        assert brand.social_images is not None
        assert brand.social_images.og_image == "https://example.com/og.png"

    @pytest.mark.asyncio
    async def test_extract_schema_org(self) -> None:
        html = """<html><head>
        <script type="application/ld+json">
        {"@type": "Organization", "name": "Acme Inc", "url": "https://acme.com"}
        </script>
        </head></html>"""
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="metadata")
        await self.stage.run(ctx, brand, result)
        assert brand.schema_org is not None
        assert brand.schema_org.name == "Acme Inc"
        assert ctx.schema_org_dict is not None

    @pytest.mark.asyncio
    async def test_no_metadata(self) -> None:
        ctx = _make_ctx("<html><body></body></html>")
        brand = BrandResponse()
        result = StageResult(stage_name="metadata")
        await self.stage.run(ctx, brand, result)
        assert brand.favicon_urls == []
        assert brand.schema_org is None
