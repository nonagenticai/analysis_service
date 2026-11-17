"""LLMBrandAnalysisStage: DeepSeek-powered brand voice, tone, industry, and content analysis."""

from __future__ import annotations

from typing import Any, NamedTuple
from urllib.parse import urlparse

from bs4 import Tag

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.llm_brand_analyzer import analyze_brand_with_llm, build_prompt


class _PageSignals(NamedTuple):
    title: str
    meta_desc: str
    headings: list[str]
    domain: str
    schema_org_type: str
    page_text: str


class LLMBrandAnalysisStage(BaseStage):
    """Replace keyword heuristics with LLM-powered brand analysis."""

    name = "llm_analysis"

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None and ctx.config.should_run("llm_analysis")

    @staticmethod
    def _extract_page_signals(ctx: PipelineContext) -> _PageSignals:
        """Pull title/meta/headings/domain/schema_org/page_text from the context."""
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("LLMAnalysisStage.run() requires ctx.soup (should_run guards this)")

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        meta_desc = ""
        meta_tag = soup.find("meta", attrs={"name": "description"})
        if meta_tag and isinstance(meta_tag, Tag):
            content = meta_tag.get("content", "")
            meta_desc = str(content).strip() if isinstance(content, str) else ""

        headings: list[str] = []
        for tag in soup.find_all(["h1", "h2", "h3"], limit=10):
            text = tag.get_text(strip=True)
            if text:
                headings.append(text)

        return _PageSignals(
            title=title,
            meta_desc=meta_desc,
            headings=headings,
            domain=urlparse(ctx.url).netloc if ctx.url else "",
            schema_org_type=str((ctx.schema_org_dict or {}).get("type", "")),
            page_text=ctx.page_text or soup.get_text(separator=" ", strip=True),
        )

    @staticmethod
    def _record_analysis(ctx: PipelineContext, brand: BrandResponse, analysis: Any) -> None:
        brand.tagline = analysis.tagline
        brand.brand_voice = analysis.brand_voice
        brand.content_analysis = analysis.to_content_dict()
        brand.tone_result = analysis.to_tone_dict()
        brand.industry = analysis.to_industry_dict()
        brand.brand_personality = analysis.brand_personality
        brand.token_usage = analysis.token_usage

        signals: list[tuple[str, str, float]] = [
            ("tagline", analysis.tagline, analysis.tone_confidence),
            ("brand_voice", analysis.brand_voice, analysis.tone_confidence),
            ("tone", analysis.tone, analysis.tone_confidence),
            ("industry", analysis.industry, analysis.industry_confidence),
        ]
        for name, value, conf in signals:
            if value:
                ctx.tracker.record("llm_analysis", name, value, conf)

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        if ctx.wm_client is None:
            raise RuntimeError("WMClient not configured on PipelineContext")

        signals = self._extract_page_signals(ctx)
        prompt = build_prompt(
            page_text=signals.page_text,
            title=signals.title,
            meta_description=signals.meta_desc,
            headings=signals.headings,
            domain=signals.domain,
            schema_org_type=signals.schema_org_type,
        )

        analysis = await analyze_brand_with_llm(
            wm_client=ctx.wm_client,
            prompt=prompt,
            model_override=ctx.llm_model_override,
        )

        self._record_analysis(ctx, brand, analysis)

        self.logger.info(
            "LLM analysis complete: tone=%s, industry=%s, tagline=%s",
            analysis.tone,
            analysis.industry,
            analysis.tagline[:50] if analysis.tagline else "(none)",
        )
