"""Unit tests for pipeline runner."""

import pytest

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.runner import (
    PipelineRunner,
    _build_report,
    _calculate_confidence,
    extract_from_analysis,
)
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import AnalysisData, BrandResponse, PageData, SiteData


class DummyStage(BaseStage):
    name = "dummy"

    def should_run(self, ctx):
        return True

    async def run(self, ctx, brand, result):
        brand.name = "DummyBrand"


class FailStage(BaseStage):
    name = "fetch"

    def should_run(self, ctx):
        return True

    async def run(self, ctx, brand, result):
        raise ConnectionError("refused")


class SkipStage(BaseStage):
    name = "nonexistent_method"

    def should_run(self, ctx):
        return False

    async def run(self, ctx, brand, result):
        brand.name = "should_not_run"


class TestPipelineRunner:
    @pytest.mark.asyncio
    async def test_run_single_stage(self) -> None:
        runner = PipelineRunner([DummyStage()])
        ctx = PipelineContext(url="https://example.com")
        brand, results = await runner.run(ctx)
        assert brand.name == "DummyBrand"
        assert brand.domain == "example.com"
        assert len(results) == 1
        assert results[0].success is True

    @pytest.mark.asyncio
    async def test_run_aborts_on_fetch_failure(self) -> None:
        runner = PipelineRunner([FailStage(), DummyStage()])
        ctx = PipelineContext(url="https://example.com")
        brand, results = await runner.run(ctx)
        assert len(results) == 1
        assert results[0].success is False
        assert brand.name == "example.com"  # Falls back to domain

    @pytest.mark.asyncio
    async def test_skips_disabled_stages(self) -> None:
        runner = PipelineRunner([SkipStage(), DummyStage()])
        ctx = PipelineContext(url="https://example.com")
        brand, results = await runner.run(ctx)
        assert brand.name == "DummyBrand"
        assert len(results) == 1  # SkipStage was skipped

    @pytest.mark.asyncio
    async def test_extraction_report_built(self) -> None:
        runner = PipelineRunner([DummyStage()])
        ctx = PipelineContext(url="https://example.com")
        brand, _ = await runner.run(ctx)
        assert brand.extraction_report is not None
        assert "dummy" in brand.extraction_report.stages_run
        assert "dummy" in brand.extraction_report.stages_succeeded

    @pytest.mark.asyncio
    async def test_domain_extraction(self) -> None:
        runner = PipelineRunner([])
        ctx = PipelineContext(url="https://www.example.com/page")
        brand, _ = await runner.run(ctx)
        assert brand.domain == "example.com"


class TestBuildReport:
    def test_empty_results(self) -> None:
        report = _build_report([])
        assert report.stages_run == []
        assert report.total_duration_ms == 0

    def test_mixed_results(self) -> None:
        ok = StageResult(stage_name="name", success=True, duration_ms=10)
        fail = StageResult(stage_name="fetch", success=False, duration_ms=5)
        fail.add_error("connection refused")
        report = _build_report([ok, fail])
        assert report.stages_run == ["name", "fetch"]
        assert report.stages_succeeded == ["name"]
        assert report.stages_failed == ["fetch"]
        assert report.total_duration_ms == 15
        assert len(report.errors) == 1


class TestCalculateConfidence:
    def test_minimal_brand(self) -> None:
        brand = BrandResponse()
        assert _calculate_confidence(brand) == 0.0

    def test_full_brand(self) -> None:
        brand = BrandResponse(
            name="Acme",
            domain="acme.com",
            has_logo=True,
            primary_color="#ff0000",
            fonts=[],
        )
        conf = _calculate_confidence(brand)
        assert conf >= 0.4  # name + logo + color


class TestExtractFromAnalysis:
    @pytest.mark.asyncio
    async def test_domain_extraction(self) -> None:
        analysis = AnalysisData(
            pages=[PageData(url="https://www.example.com/page", title="Example")]
        )
        result = await extract_from_analysis(analysis)
        assert result.domain == "example.com"

    @pytest.mark.asyncio
    async def test_name_from_title(self) -> None:
        analysis = AnalysisData(
            pages=[PageData(url="https://example.com", title="Acme Corp | Home")]
        )
        result = await extract_from_analysis(analysis)
        assert result.name == "Acme Corp"

    @pytest.mark.asyncio
    async def test_name_from_site_description(self) -> None:
        analysis = AnalysisData(
            pages=[PageData(url="https://example.com")],
            site=SiteData(description="My Cool Site"),
        )
        result = await extract_from_analysis(analysis)
        assert result.name == "My Cool Site"

    @pytest.mark.asyncio
    async def test_logo_extraction(self) -> None:
        analysis = AnalysisData(
            pages=[PageData(url="https://example.com")],
            site=SiteData(logo_url="https://example.com/logo.png"),
        )
        result = await extract_from_analysis(analysis)
        assert result.logo_url == "https://example.com/logo.png"
        assert result.has_logo is True

    @pytest.mark.asyncio
    async def test_color_extraction(self) -> None:
        analysis = AnalysisData(
            pages=[PageData(url="https://example.com")],
            site=SiteData(visual_identity={"primary_color": "#ff0000"}),
        )
        result = await extract_from_analysis(analysis)
        assert result.primary_color == "#ff0000"
        assert result.color_palette is not None

    @pytest.mark.asyncio
    async def test_empty_analysis(self) -> None:
        result = await extract_from_analysis(AnalysisData())
        assert result.domain == ""
        assert result.name == ""
