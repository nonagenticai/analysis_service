"""Infer context from crawl and analysis data."""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlparse

from src.domains.analyze.schemas import PageType
from src.domains.analyze.services.context.types import (
    FeaturePriority,
    InferredContext,
    NarrationGuidance,
)

logger = logging.getLogger(__name__)

_HIGH_VALUE_TYPES = {
    PageType.HOMEPAGE,
    PageType.LANDING,
    PageType.PRICING,
    PageType.PRODUCT,
    PageType.DASHBOARD,
}
_LOW_VALUE_TYPES = {PageType.LOGIN, PageType.UNKNOWN}


class ContextInferrer:
    """Infer context from page analysis data."""

    @staticmethod
    def from_pages(pages: list[dict]) -> InferredContext:
        """Infer context from analyzed pages.

        - site_type from PageType distribution
        - focus_pages from highest-value pages
        - exclude_pages from login, error pages
        """
        page_types: list[str] = []
        focus_candidates: list[str] = []
        exclude_candidates: list[str] = []

        for page in pages:
            page_type = page.get("page_type", "unknown")
            page_types.append(page_type)
            url = page.get("url", "")

            if page_type in {t.value for t in _HIGH_VALUE_TYPES}:
                focus_candidates.append(url)
            if page_type in {t.value for t in _LOW_VALUE_TYPES}:
                exclude_candidates.append(url)

            lower_url = url.lower()
            if (
                any(
                    pat in lower_url
                    for pat in ["/login", "/signin", "/admin", "/error", "/404", "/500"]
                )
                and url not in exclude_candidates
            ):
                exclude_candidates.append(url)

        site_type = _infer_site_type(page_types)

        return InferredContext(
            site_type=site_type,
            focus_pages=focus_candidates,
            exclude_pages=exclude_candidates,
        )

    @staticmethod
    def from_site_analysis(
        site_analysis: dict,
        prior: InferredContext | None = None,
    ) -> InferredContext:
        """Enrich context from site-level analysis results."""
        ctx = InferredContext()

        description = site_analysis.get("description", "")
        if description:
            ctx.description = description

        audience = site_analysis.get("target_audience", [])
        if audience:
            if isinstance(audience, list):
                ctx.target_audience = ", ".join(str(a) for a in audience)
            else:
                ctx.target_audience = str(audience)

        use_cases = site_analysis.get("use_cases", [])
        if use_cases:
            ctx.goals = list(use_cases)

        selling_points = site_analysis.get("selling_points", [])
        if selling_points:
            ctx.feature_priorities = [
                FeaturePriority(feature=sp, priority="medium") for sp in selling_points
            ]

        tone = site_analysis.get("tone", "")
        if tone:
            ctx.narration_guidance = NarrationGuidance(tone=tone)

        if prior and prior.site_type and not ctx.site_type:
            ctx.site_type = prior.site_type

        return ctx

    @staticmethod
    def merge(
        user_provided: dict | None,
        auto_generated: InferredContext,
    ) -> dict:
        """Merge user-provided and auto-generated contexts.

        User values always win over auto-generated values.

        Args:
            user_provided: User-supplied context dict, or None.
            auto_generated: Auto-inferred context from analysis.

        Returns:
            Merged context dict.
        """
        auto = auto_generated.model_dump()
        if user_provided is None:
            return auto

        merged = {}
        for key, auto_val in auto.items():
            if key == "auto_generated":
                continue
            user_val = user_provided.get(key)
            if user_val:
                merged[key] = user_val
            else:
                merged[key] = auto_val

        return merged


# URL substring -> PageType mapping, in priority order.
_URL_PAGE_TYPE_RULES: tuple[tuple[tuple[str, ...], PageType], ...] = (
    (("/pricing",), PageType.PRICING),
    (("/product",), PageType.PRODUCT),
    (("/doc", "/docs"), PageType.DOCUMENTATION),
    (("/about",), PageType.ABOUT),
    (("/contact",), PageType.CONTACT),
    (("/faq",), PageType.FAQ),
    (("/login", "/signin"), PageType.LOGIN),
    (("/dashboard",), PageType.DASHBOARD),
    (("/gallery",), PageType.GALLERY),
)


def _infer_page_type_from_url(url: str) -> str:
    """Guess page type from URL path patterns."""
    lower = url.lower()
    parsed = urlparse(lower)
    if parsed.path in ("", "/"):
        return PageType.HOMEPAGE.value

    path_tail = lower.split("?")[0].rstrip("/").split("/")[-1]
    if "/blog/" in lower or path_tail == "blog":
        # /blog/ is special-cased before generic substring rules so the index page
        # wins over /blog as a misclassified product or doc tail.
        return PageType.BLOG_INDEX.value

    for substrings, page_type in _URL_PAGE_TYPE_RULES:
        if any(s in lower for s in substrings):
            return page_type.value
    return PageType.LANDING.value


def _infer_site_type(page_types: list[str]) -> str:
    """Infer site type from distribution of page types."""
    if not page_types:
        return ""

    counter = Counter(page_types)

    if counter.get(PageType.PRICING.value, 0) > 0 and counter.get(PageType.PRODUCT.value, 0) > 0:
        return "saas"
    if counter.get(PageType.PRODUCT.value, 0) >= 3:
        return "ecommerce"
    if (
        counter.get(PageType.BLOG_INDEX.value, 0) > 0
        or counter.get(PageType.BLOG_POST.value, 0) >= 3
    ):
        return "blog"
    if counter.get(PageType.DOCUMENTATION.value, 0) >= 2:
        return "docs"
    if counter.get(PageType.DASHBOARD.value, 0) > 0:
        return "webapp"
    if counter.get(PageType.PRICING.value, 0) > 0:
        return "saas"

    return "website"
