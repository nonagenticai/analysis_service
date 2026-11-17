"""SocialStage: extract social media profiles."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.social_extractor import SocialExtractor


class SocialStage(BaseStage):
    """Extract social media profile URLs."""

    name = "social"

    def __init__(self) -> None:
        super().__init__()
        self._social_extractor = SocialExtractor()

    def should_run(self, ctx: PipelineContext) -> bool:
        return (
            ctx.soup is not None
            and ctx.config.should_run("social_extractor")
            and ctx.config.is_feature_enabled("social")
        )

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("SocialStage.run() requires ctx.soup (should_run guards this)")

        try:
            social = self._social_extractor.extract_social_profiles(soup, ctx.schema_org_dict)
            brand.social_profiles = social.to_dict()
            if social.has_any:
                ctx.tracker.record("social_extractor", "social_profiles", str(social.count))
        except (ValueError, TypeError, KeyError) as e:
            result.add_warning(f"Social extraction failed: {e}")
