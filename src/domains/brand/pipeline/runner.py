"""Pipeline runner: thin orchestrator that executes stages in sequence."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from urllib.parse import urlparse

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import (
    AnalysisData,
    BrandResponse,
    ColorInfo,
    ColorPalette,
    ExtractionMetadata,
    ExtractionReport,
    StageIssueSchema,
)
from src.domains.brand.services.color_service import darken_hex, generate_palette

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Executes pipeline stages in sequence."""

    def __init__(self, stages: list[BaseStage]) -> None:
        self._stages = stages

    @staticmethod
    def _derive_font_families(brand: BrandResponse) -> None:
        """Populate ``font_family_primary`` / ``_secondary`` from ``brand.fonts``."""
        for font in brand.fonts:
            if font.role == "heading" and not brand.font_family_primary:
                brand.font_family_primary = font.family
            elif font.role == "body" and not brand.font_family_secondary:
                brand.font_family_secondary = font.family
        if not brand.font_family_primary and brand.fonts:
            brand.font_family_primary = brand.fonts[0].family
        if not brand.font_family_secondary:
            brand.font_family_secondary = brand.font_family_primary

    async def _execute_stages(
        self, ctx: PipelineContext, brand: BrandResponse
    ) -> list[StageResult]:
        """Run each stage in order; abort early if the fetch stage fails."""
        results: list[StageResult] = []
        for stage in self._stages:
            if not stage.should_run(ctx):
                continue
            result = await stage.execute(ctx, brand)
            results.append(result)
            if stage.name == "fetch" and not result.success:
                brand.name = brand.name or brand.domain
                break
        return results

    async def run(self, ctx: PipelineContext) -> tuple[BrandResponse, list[StageResult]]:
        """Run all stages, building a BrandResponse."""
        start_time = time.monotonic()
        brand = BrandResponse()

        if ctx.url:
            parsed = urlparse(ctx.url)
            brand.domain = parsed.netloc.removeprefix("www.")

        results = await self._execute_stages(ctx, brand)
        self._derive_font_families(brand)

        brand.extraction_report = _build_report(results)
        brand.extraction_methods = ctx.tracker.get_methods_used()

        if ctx.include_metadata:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            brand.metadata = ExtractionMetadata(
                extracted_at=datetime.now(UTC),
                source="url",
                confidence=(
                    ctx.tracker.overall_confidence()
                    if ctx.tracker.result_count() > 0
                    else _calculate_confidence(brand)
                ),
                extraction_duration_ms=duration_ms,
                extractor_version="2.0.0",
            )

        total_ms = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "Pipeline completed, domain=%s, methods=%s, confidence=%.2f, duration_ms=%d",
            brand.domain,
            brand.extraction_methods,
            brand.metadata.confidence if brand.metadata else 0.0,
            total_ms,
        )
        return brand, results


def _build_report(results: list[StageResult]) -> ExtractionReport:
    """Build an ExtractionReport from stage results."""
    report = ExtractionReport()
    for r in results:
        report.stages_run.append(r.stage_name)
        if r.success:
            report.stages_succeeded.append(r.stage_name)
        else:
            report.stages_failed.append(r.stage_name)
        for w in r.warnings:
            report.warnings.append(
                StageIssueSchema(stage=w.stage, severity=w.severity, message=w.message)
            )
        for e in r.errors:
            report.errors.append(
                StageIssueSchema(stage=e.stage, severity=e.severity, message=e.message)
            )
        report.total_duration_ms += r.duration_ms
    return report


def _calculate_confidence(brand: BrandResponse) -> float:
    """Fallback confidence based on how many signals were found."""
    score = 0.0
    if brand.name and brand.name != brand.domain:
        score += 0.2
    if brand.has_logo:
        score += 0.2
    if brand.primary_color != "#2563EB":
        score += 0.2
    if brand.fonts:
        score += 0.15
    if brand.schema_org:
        score += 0.15
    if brand.favicon_urls:
        score += 0.1
    return min(score, 1.0)


async def extract_from_analysis(analysis: AnalysisData) -> BrandResponse:
    """Extract brand from pre-analyzed AnalysisData (no pipeline needed)."""
    brand = BrandResponse()

    # Extract domain
    if analysis.pages:
        first_url = analysis.pages[0].url
        if first_url:
            parsed = urlparse(first_url)
            brand.domain = parsed.netloc.removeprefix("www.")

    # Extract name
    brand.name = _extract_name_from_analysis(analysis) or brand.domain

    # Extract logo URL
    brand.logo_url = _extract_logo_url_from_analysis(analysis)
    brand.has_logo = bool(brand.logo_url)

    # Extract colors
    primary, secondary = _extract_colors_from_analysis(analysis)
    if primary:
        brand.primary_color = primary
    if secondary:
        brand.secondary_color = secondary

    # Generate color palette
    if brand.primary_color and brand.primary_color != "#2563EB":
        palette_data = generate_palette(brand.primary_color)
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

    return brand


def _extract_name_from_analysis(analysis: AnalysisData) -> str:
    if analysis.site:
        desc = analysis.site.description
        if desc and len(desc) < 60:
            return desc.strip()
    if analysis.pages:
        title = analysis.pages[0].title
        if title:
            cleaned = re.split(
                r"\s*[|-—-]\s*(?:Home|Welcome|Homepage).*$",
                title,
                flags=re.IGNORECASE,
            )
            return cleaned[0].strip() if cleaned else title.strip()
    return ""


def _extract_logo_url_from_analysis(analysis: AnalysisData) -> str:
    if analysis.site:
        if analysis.site.logo_url:
            return analysis.site.logo_url
        if analysis.site.metadata:
            og_image = analysis.site.metadata.get("og:image", "")
            if og_image:
                return str(og_image)
        if analysis.site.favicon:
            return analysis.site.favicon
    if analysis.pages:
        meta = analysis.pages[0].metadata or {}
        og_image = meta.get("og:image", "")
        if og_image:
            return str(og_image)
    return ""


def _extract_colors_from_analysis(analysis: AnalysisData) -> tuple[str, str]:
    if analysis.site and analysis.site.visual_identity:
        vi = analysis.site.visual_identity
        for key in ("primary_color", "primary", "brand_color", "color"):
            if val := vi.get(key):
                color = str(val).strip()
                if re.match(r"^#[0-9a-fA-F]{3,8}$", color):
                    return color, darken_hex(color)
    return "", ""
