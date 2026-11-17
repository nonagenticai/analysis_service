"""QualityStage: consistency scoring (runs last)."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.consistency_scorer import BrandConsistencyScorer


class QualityStage(BaseStage):
    """Score brand quality and completeness."""

    name = "quality"

    def __init__(self) -> None:
        super().__init__()
        self._scorer = BrandConsistencyScorer()

    def should_run(self, ctx: PipelineContext) -> bool:
        return True  # Always run quality scoring

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        try:
            report = self._scorer.score(brand)
            brand.quality_report = report.to_dict()
        except (ValueError, TypeError, KeyError) as e:
            result.add_warning(f"Quality scoring failed: {e}")
