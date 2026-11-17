"""VisualColorStage: extract dominant colors from page screenshot."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.visual_color_extractor import extract_dominant_colors

DEFAULT_PRIMARY = "#2563EB"


class VisualColorStage(BaseStage):
    """Extract dominant colors from page screenshot using k-means clustering."""

    name = "visual_colors"

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.screenshot_bytes) and ctx.config.should_run("visual_colors")

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        colors = extract_dominant_colors(ctx.screenshot_bytes, n_colors=5)
        ctx.visual_dominant_colors = colors
        brand.visual_dominant_colors = colors
        self.logger.info("Extracted %d visual dominant colors", len(colors))

        if colors:
            ctx.tracker.record("visual_colors", "visual_dominant_colors", ",".join(colors))

        # If CSS extraction found no brand colors (still default), use visual dominant
        if colors and brand.primary_color == DEFAULT_PRIMARY:
            brand.primary_color = colors[0]
            ctx.tracker.record("visual_colors", "primary_color", colors[0])
            result.add_warning(
                "Primary color set from visual extraction (no CSS brand color found)"
            )
