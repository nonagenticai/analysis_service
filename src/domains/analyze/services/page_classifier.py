"""Classify pages by type using URL patterns and structural signals."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from src.domains.analyze.schemas import PageType

_SUBDOMAIN_PAGE_TYPES: dict[str, PageType] = {
    "blog": PageType.BLOG_INDEX,
    "docs": PageType.DOCUMENTATION,
    "app": PageType.DASHBOARD,
    "help": PageType.FAQ,
    "support": PageType.CONTACT,
    "shop": PageType.PRODUCT_LISTING,
    "store": PageType.PRODUCT_LISTING,
}

URL_PATTERNS: list[tuple[str, PageType]] = [
    (r"^/$|^/home(?=/|$)|^/index(?:\.html?)?$", PageType.HOMEPAGE),
    (r"^/[a-z]{2}(-[A-Z]{2})?/?$", PageType.HOMEPAGE),
    (r"/login(?=/|$)|/signin(?=/|$)", PageType.LOGIN),
    (r"/pricing(?=/|$)|/plans(?=/|$)", PageType.PRICING),
    (
        r"/blog/?$|/articles/?$|/news/?$|/posts/?$"
        r"|/blog/page/|/blog/category/|/blog/tag/"
        r"|/articles/page/|/articles/category/|/articles/tag/"
        r"|/news/page/|/news/category/|/news/tag/",
        PageType.BLOG_INDEX,
    ),
    (r"/blog/.+|/articles/.+|/posts/.+", PageType.BLOG_POST),
    (r"/products/?$|/shop/?$|/store/?$", PageType.PRODUCT_LISTING),
    (r"/products/.+|/shop/.+|/store/.+|/item/.+", PageType.PRODUCT),
    (r"/docs(?=/|$)|/documentation(?=/|$)|/guide(?=/|$)", PageType.DOCUMENTATION),
    (r"/about(?=/|$)|/team(?=/|$)|/company(?=/|$)", PageType.ABOUT),
    (r"/contact(?=/|$)|/support(?=/|$)", PageType.CONTACT),
    (r"/faq(?=/|$)|/help(?=/|$)", PageType.FAQ),
    (
        r"/gallery(?=/|$)|/portfolio(?=/|$)"
        r"|/photos(?=/|$)|/images(?=/|$)|/media(?=/|$)"
        r"|/showcase(?=/|$)|/work(?=/|$)",
        PageType.GALLERY,
    ),
    (r"/dashboard(?=/|$)|/admin(?=/|$)|/app(?=/|$)", PageType.DASHBOARD),
    (r"/signup(?=/|$)|/register(?=/|$)|/get-started(?=/|$)", PageType.FORM),
    (r"/lp(?=/|$)|/landing(?=/|$)|/campaign(?=/|$)", PageType.LANDING),
    (r"/solutions(?=/|$)|/features(?=/|$)|/capabilities(?=/|$)", PageType.PRODUCT),
    (r"/platform(?=/|$)|/technology(?=/|$)|/architecture(?=/|$)", PageType.PRODUCT),
    (
        r"/case-studies(?=/|$)|/customers(?=/|$)|/success-stories(?=/|$)|/testimonials(?=/|$)",
        PageType.LANDING,
    ),
    (r"/resources(?=/|$)|/whitepapers(?=/|$)|/ebooks(?=/|$)", PageType.LANDING),
    (r"/integrations(?=/|$)|/partners(?=/|$)|/ecosystem(?=/|$)", PageType.PRODUCT),
    (r"/search(?=/|$)", PageType.UNKNOWN),
    (
        r"/privacy(?=/|$)|/terms(?=/|$)|/legal(?=/|$)" r"|/cookie-policy(?=/|$)|/tos(?=/|$)",
        PageType.UNKNOWN,
    ),
    (r"/careers(?=/|$)|/jobs(?=/|$)", PageType.UNKNOWN),
]


class PageClassifier:
    """Classify page types using URL patterns and structural signals."""

    def classify(
        self,
        url: str,
        has_forms: bool = False,
        interactive_elements: list[dict] | None = None,
        content_sections: list[dict] | None = None,
    ) -> PageType:
        """Classify a page type from its URL, forms, and structural signals.

        Args:
            url: The page URL.
            has_forms: Whether the page contains forms.
            interactive_elements: List of interactive element dicts.
            content_sections: List of content section dicts.

        Returns:
            The inferred PageType enum value.
        """
        parsed = urlparse(url)
        path = unquote(parsed.path).rstrip("/") or "/"
        hostname = parsed.hostname or ""

        if path == "/":
            subdomain = hostname.split(".")[0] if "." in hostname else ""
            if subdomain in _SUBDOMAIN_PAGE_TYPES:
                return _SUBDOMAIN_PAGE_TYPES[subdomain]

        fragment = parsed.fragment
        if path == "/" and fragment and fragment.startswith("/"):
            path = fragment.rstrip("/") or "/"

        for pattern, page_type in URL_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                return page_type

        elements = interactive_elements or []
        if has_forms and any(
            "signup" in (e.get("name") or "").lower() or "register" in (e.get("name") or "").lower()
            for e in elements
        ):
            return PageType.FORM

        sections = {s.get("section_type", "") for s in (content_sections or [])}
        if "hero" in sections and "features" in sections:
            return PageType.LANDING
        if "pricing" in sections:
            return PageType.PRICING

        content_signals = {"features", "testimonials", "hero", "cta"}
        if has_forms and not (sections & content_signals):
            return PageType.FORM

        return PageType.UNKNOWN
