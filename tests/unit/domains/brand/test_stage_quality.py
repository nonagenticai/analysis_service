"""Unit tests for QualityStage."""

import pytest

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.quality import QualityStage


class TestQualityStage:
    def setup_method(self) -> None:
        self.stage = QualityStage()

    def test_name(self) -> None:
        assert self.stage.name == "quality"

    def test_always_should_run(self) -> None:
        ctx = PipelineContext()
        assert self.stage.should_run(ctx) is True

    @pytest.mark.asyncio
    async def test_scores_brand(self) -> None:
        ctx = PipelineContext()
        brand = BrandResponse(
            name="Acme Corp",
            domain="acme.com",
            has_logo=True,
            logo_url="https://acme.com/logo.png",
            primary_color="#e74c3c",
        )
        result = StageResult(stage_name="quality")
        await self.stage.run(ctx, brand, result)
        assert brand.quality_report is not None
        assert "overall_score" in brand.quality_report

    @pytest.mark.asyncio
    async def test_scores_minimal_brand(self) -> None:
        ctx = PipelineContext()
        brand = BrandResponse()
        result = StageResult(stage_name="quality")
        await self.stage.run(ctx, brand, result)
        assert brand.quality_report is not None
