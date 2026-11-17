"""LogoDownloadStage: download and store logo (runs after LogoStage)."""

from __future__ import annotations

import httpx

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse


class LogoDownloadStage(BaseStage):
    """Download and store the logo image."""

    name = "logo_download"

    def should_run(self, ctx: PipelineContext) -> bool:
        return True  # Conditional logic is in run()

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        """Fetch the logo bytes and attach the stored asset to *brand*.

        A logo is a nice-to-have on a brand record, so every failure here is a
        warning and never a raise: the extraction still returns colors, fonts
        and metadata. ``httpx.HTTPError`` is in the tuple because
        ``LogoService.download_and_store`` documents raising it (see
        ``services/logo_service.py``) and it derives from ``Exception``, not
        ``OSError`` — without it a connect error or a read timeout on the logo
        URL escaped this stage and failed the whole extraction.
        """
        dl = ctx.download_logo or ctx.config.is_feature_enabled("logo")
        if not (dl and brand.logo_url and ctx.logo_service and ctx.tenant_id):
            return

        try:
            logo_asset = await ctx.logo_service.download_and_store(brand.logo_url, ctx.tenant_id)
            brand.logo_asset = logo_asset
            brand.logo_serve_url = f"/api/brand/logo/{logo_asset.asset_id}"
        except (OSError, TimeoutError, ValueError, httpx.HTTPError) as e:
            result.add_warning(f"Logo download failed for {brand.logo_url}: {e}")
