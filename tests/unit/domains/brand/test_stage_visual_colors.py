"""Unit tests for VisualColorStage."""

import io

import pytest
from PIL import Image

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.visual_colors import VisualColorStage


def _make_screenshot(color: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    """Create a simple test screenshot."""
    img = Image.new("RGB", (100, 100), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestVisualColorStage:
    def setup_method(self) -> None:
        self.stage = VisualColorStage()

    def test_name(self) -> None:
        assert self.stage.name == "visual_colors"

    def test_should_run_with_screenshot(self) -> None:
        ctx = PipelineContext(screenshot_bytes=b"fake_png")
        assert self.stage.should_run(ctx) is True

    def test_should_not_run_without_screenshot(self) -> None:
        ctx = PipelineContext()
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_extracts_colors(self) -> None:
        ctx = PipelineContext(screenshot_bytes=_make_screenshot())
        brand = BrandResponse()
        result = StageResult(stage_name="visual_colors")

        await self.stage.run(ctx, brand, result)

        assert len(ctx.visual_dominant_colors) > 0
        assert len(brand.visual_dominant_colors) > 0
        for c in ctx.visual_dominant_colors:
            assert c.startswith("#")

    @pytest.mark.asyncio
    async def test_sets_primary_when_default(self) -> None:
        ctx = PipelineContext(screenshot_bytes=_make_screenshot((200, 50, 50)))
        brand = BrandResponse()  # primary_color defaults to #2563EB
        result = StageResult(stage_name="visual_colors")

        await self.stage.run(ctx, brand, result)

        # Should have overridden the default primary color
        assert brand.primary_color != "#2563EB"
        assert brand.primary_color.startswith("#")

    @pytest.mark.asyncio
    async def test_keeps_existing_primary(self) -> None:
        ctx = PipelineContext(screenshot_bytes=_make_screenshot())
        brand = BrandResponse(primary_color="#ff0000")
        result = StageResult(stage_name="visual_colors")

        await self.stage.run(ctx, brand, result)

        # Should NOT override non-default primary
        assert brand.primary_color == "#ff0000"
