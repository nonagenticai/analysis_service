"""MetadataStage: favicons, social images, schema.org, manifest."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.color_service import darken_hex
from src.domains.brand.services.metadata_extractor import MetadataExtractor


class MetadataStage(BaseStage):
    """Extract metadata: favicons, social images, schema.org, manifest."""

    name = "metadata"

    def __init__(self) -> None:
        super().__init__()
        self._metadata_extractor = MetadataExtractor()

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("MetadataStage.run() requires ctx.soup (should_run guards this)")

        # Favicons
        brand.favicon_urls = self._metadata_extractor.extract_favicons(soup, ctx.url)
        if brand.favicon_urls:
            ctx.tracker.record("html_meta", "favicon_urls", str(len(brand.favicon_urls)))

        # Social images
        brand.social_images = self._metadata_extractor.extract_social_images(soup)

        # Schema.org
        brand.schema_org = self._metadata_extractor.extract_schema_org(soup)
        if brand.schema_org:
            ctx.tracker.record("schema_org", "schema_org", brand.schema_org.type)
            # Store for downstream stages
            ctx.schema_org_dict = brand.schema_org.model_dump()

        # Web manifest
        try:
            manifest_color, _ = await self._metadata_extractor.extract_manifest(soup, ctx.url)
            if manifest_color:
                brand.manifest_theme_color = manifest_color
                ctx.tracker.record("manifest", "manifest_theme_color", manifest_color)
                # Use manifest theme color as primary if we don't have one
                if brand.primary_color == "#2563EB" and manifest_color.startswith("#"):
                    brand.primary_color = manifest_color
                    brand.secondary_color = darken_hex(manifest_color)
        except (ValueError, TypeError, KeyError) as e:
            result.add_warning(f"Manifest extraction failed: {e}")
