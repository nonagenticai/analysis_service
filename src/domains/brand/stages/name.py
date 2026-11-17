"""NameStage: extract brand name from title/og:site_name."""

from __future__ import annotations

import re

from bs4 import Tag

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse


class NameStage(BaseStage):
    """Extract brand name from HTML title and og:site_name."""

    name = "name"

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("NameStage.run() requires ctx.soup (should_run guards this)")

        # Try <title>
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            title = title_tag.string.strip()
            cleaned = re.split(
                r"\s*[|-—-]\s*(?:Home|Welcome|Homepage).*$",
                title,
                flags=re.IGNORECASE,
            )
            brand.name = cleaned[0].strip() if cleaned else title
            ctx.tracker.record("html_meta", "name", brand.name)

        # Fallback: og:site_name
        if not brand.name:
            og_name = soup.find("meta", attrs={"property": "og:site_name"})
            if og_name and isinstance(og_name, Tag):
                content = og_name.get("content", "")
                if content:
                    brand.name = (
                        str(content).strip()
                        if isinstance(content, str)
                        else str(content[0]).strip()
                    )
                    ctx.tracker.record("html_meta", "name", brand.name, 0.8)

        # Final fallback: domain
        if not brand.name:
            brand.name = brand.domain
