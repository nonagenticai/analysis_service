"""Unit tests for LogoDownloadStage."""

from unittest.mock import AsyncMock

import pytest

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse, LogoAsset
from src.domains.brand.stages.logo_download import LogoDownloadStage


class TestLogoDownloadStage:
    def setup_method(self) -> None:
        self.stage = LogoDownloadStage()

    def test_name(self) -> None:
        assert self.stage.name == "logo_download"

    @pytest.mark.asyncio
    async def test_downloads_logo(self) -> None:
        mock_logo_service = AsyncMock()
        mock_logo_service.download_and_store = AsyncMock(
            return_value=LogoAsset(
                asset_id="abc123",
                stored_path="t1/abc123.png",
                content_type="image/png",
                size_bytes=1024,
            )
        )
        ctx = PipelineContext(
            download_logo=True,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            logo_service=mock_logo_service,
        )
        brand = BrandResponse(logo_url="https://example.com/logo.png")
        result = StageResult(stage_name="logo_download")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_asset is not None
        assert brand.logo_asset.asset_id == "abc123"
        assert brand.logo_serve_url == "/api/brand/logo/abc123"

    @pytest.mark.asyncio
    async def test_skips_without_logo_url(self) -> None:
        ctx = PipelineContext(download_logo=True, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523")
        brand = BrandResponse(logo_url="")
        result = StageResult(stage_name="logo_download")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_asset is None

    @pytest.mark.asyncio
    async def test_skips_without_tenant_id(self) -> None:
        ctx = PipelineContext(download_logo=True)
        brand = BrandResponse(logo_url="https://example.com/logo.png")
        result = StageResult(stage_name="logo_download")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_asset is None

    @pytest.mark.asyncio
    async def test_handles_download_failure(self) -> None:
        mock_logo_service = AsyncMock()
        mock_logo_service.download_and_store = AsyncMock(side_effect=ValueError("Too large"))
        ctx = PipelineContext(
            download_logo=True,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            logo_service=mock_logo_service,
        )
        brand = BrandResponse(logo_url="https://example.com/logo.png")
        result = StageResult(stage_name="logo_download")
        await self.stage.run(ctx, brand, result)
        assert brand.logo_asset is None
        assert len(result.warnings) == 1
