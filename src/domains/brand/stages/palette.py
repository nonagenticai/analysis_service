"""PaletteStage: WCAG color palette generation (runs after ColorStage)."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse, ColorInfo, ColorPalette
from src.domains.brand.services.color_service import generate_palette


class PaletteStage(BaseStage):
    """Generate WCAG-compliant color palette from primary color."""

    name = "palette"

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.include_colors and ctx.config.is_feature_enabled("colors")

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        if not brand.primary_color:
            return

        try:
            palette_data = generate_palette(brand.primary_color, ctx.background_color)
            brand.color_palette = ColorPalette(
                primary=ColorInfo(**palette_data["primary"]),
                secondary=ColorInfo(**palette_data["secondary"]),
                accent=ColorInfo(**palette_data["accent"]),
                complementary=ColorInfo(**palette_data["complementary"]),
                analogous=[ColorInfo(**c) for c in palette_data["analogous"]],
                light_variant=ColorInfo(**palette_data["light_variant"]),
                dark_variant=ColorInfo(**palette_data["dark_variant"]),
                recommended_text_color=palette_data["recommended_text_color"],
                colors=[ColorInfo(**c) for c in palette_data["colors"]],
            )
        except (ValueError, TypeError, KeyError) as e:
            result.add_warning(f"Palette generation failed: {e}")
