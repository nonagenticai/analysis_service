"""Unit tests for SocialStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.social import SocialStage


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestSocialStage:
    def setup_method(self) -> None:
        self.stage = SocialStage()

    def test_name(self) -> None:
        assert self.stage.name == "social"

    @pytest.mark.asyncio
    async def test_extract_social_profiles(self) -> None:
        html = """<html><body>
        <footer>
            <a href="https://twitter.com/example">Twitter</a>
            <a href="https://github.com/example">GitHub</a>
        </footer>
        </body></html>"""
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="social")
        await self.stage.run(ctx, brand, result)
        assert brand.social_profiles is not None

    @pytest.mark.asyncio
    async def test_no_social_profiles(self) -> None:
        ctx = _make_ctx("<html><body></body></html>")
        brand = BrandResponse()
        result = StageResult(stage_name="social")
        await self.stage.run(ctx, brand, result)
        # Should complete without error
        assert result.success is True

    def test_should_not_run_when_disabled(self) -> None:
        from src.domains.brand.services.extraction_config import BrandExtractConfig

        ctx = PipelineContext(config=BrandExtractConfig(extract_social=False))
        ctx.soup = BeautifulSoup("<html></html>", "html.parser")
        assert self.stage.should_run(ctx) is False
