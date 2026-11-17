"""Unit tests for PaletteStage."""

import pytest

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.palette import PaletteStage


class TestPaletteStage:
    def setup_method(self) -> None:
        self.stage = PaletteStage()

    def test_name(self) -> None:
        assert self.stage.name == "palette"

    def test_should_not_run_when_colors_disabled(self) -> None:
        ctx = PipelineContext(include_colors=False)
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_generate_palette(self) -> None:
        ctx = PipelineContext()
        brand = BrandResponse(primary_color="#e74c3c")
        result = StageResult(stage_name="palette")
        await self.stage.run(ctx, brand, result)
        assert brand.color_palette is not None
        assert brand.color_palette.primary is not None
        assert brand.color_palette.primary.hex == "#e74c3c"
        assert brand.color_palette.secondary is not None
        assert brand.color_palette.accent is not None

    @pytest.mark.asyncio
    async def test_no_palette_without_primary(self) -> None:
        ctx = PipelineContext()
        brand = BrandResponse(primary_color="")
        result = StageResult(stage_name="palette")
        await self.stage.run(ctx, brand, result)
        assert brand.color_palette is None

    @pytest.mark.asyncio
    async def test_uses_background_color(self) -> None:
        ctx = PipelineContext()
        ctx.background_color = "#1a1a1a"
        brand = BrandResponse(primary_color="#3498db")
        result = StageResult(stage_name="palette")
        await self.stage.run(ctx, brand, result)
        assert brand.color_palette is not None
