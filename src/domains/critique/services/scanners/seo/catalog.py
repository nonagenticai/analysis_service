"""Static SEO check catalog (design 2026-10-17 X-G1) for the audit check inventory.

Every deterministic ``seo-*`` check the ``/api/seo/audit`` path can emit, with
its audit group (via :func:`scoring.group_of`), a short title, the capability it
needs beyond the page HTML, and a target where one is obvious. The inventory
(``checks[]``) is built from this list: a catalogued check with no finding is
``pass`` — unless its requirement was not available, in which case it is
``not_run`` and never ``pass``.

``requires`` tokens:

* ``"url"`` — a live fetch on the site origin (robots.txt, sitemap.xml,
  llms.txt). Inline HTML cannot provide it.
* ``"page_url"`` — the page's own address (URL structure, HTTPS, canonical
  protocol). Inline HTML has none, so those checks cannot fire.
* ``"crawl"`` — the opt-in cross-page internal-linking crawl.

A guard test asserts every ``seo-*`` literal in this package is either here or
in :data:`STATUS_MARKER_IDS`, so the catalog cannot rot as checks are added.
"""

from __future__ import annotations

from src.domains.critique.services.scanners.seo.scoring import group_of
from src.domains.scorecards.findings import CheckSpec

# Findings that are "we could not check" markers, not checks of their own. They
# make their parent check ``not_run`` and are kept out of the inventory.
STATUS_MARKER_IDS: dict[str, str] = {
    "seo-robots-txt-errored": "seo-robots-txt-missing",
    "seo-sitemap-errored": "seo-sitemap-missing",
}

# (check_id, title, requires, target)
_ROWS: tuple[tuple[str, str, str | None, str | None], ...] = (
    # --- on_page -----------------------------------------------------------
    ("seo-missing-title", "Title tag present", None, "a <title> tag"),
    ("seo-title-too-short", "Title not too short", None, "50-60 characters"),
    ("seo-title-too-long", "Title not too long", None, "50-60 characters"),
    ("seo-meta-description-missing", "Meta description present", None, "a meta description"),
    (
        "seo-meta-description-too-short",
        "Meta description not too short",
        None,
        "150-160 characters",
    ),
    ("seo-meta-description-too-long", "Meta description not too long", None, "150-160 characters"),
    ("seo-missing-h1", "H1 heading present", None, "exactly one H1"),
    ("seo-multiple-h1", "Single H1 heading", None, "exactly one H1"),
    ("seo-heading-hierarchy-skip", "Heading levels not skipped", None, "no skipped levels"),
    ("seo-images-missing-alt", "Images have alt text", None, "0 images without alt"),
    ("seo-keyword-alignment", "Title and H1 share keywords", None, None),
    ("seo-duplicate-meta", "No duplicate meta tags", None, "each meta name once"),
    # --- technical ---------------------------------------------------------
    ("seo-canonical-missing", "Canonical link present", None, '<link rel="canonical">'),
    ("seo-canonical-protocol-mismatch", "Canonical uses the page protocol", "page_url", None),
    ("seo-internal-nofollow", "Internal links are followable", None, "no rel=nofollow internally"),
    ("seo-url-underscores", "URL path has no underscores", "page_url", "hyphens, not underscores"),
    ("seo-url-query-params", "URL has few query parameters", "page_url", None),
    ("seo-url-depth", "URL path is shallow", "page_url", None),
    ("seo-meta-robots-noindex", "Page is not noindex", None, "indexable"),
    ("seo-meta-robots-nofollow", "Page is not nofollow", None, "links followable"),
    ("seo-favicon-missing", "Favicon present", None, None),
    ("seo-hreflang-no-default", "hreflang has x-default", None, "x-default when hreflang used"),
    ("seo-hreflang-invalid", "hreflang codes are valid", None, "ISO 639-1 codes"),
    ("seo-hreflang-duplicate", "hreflang has no duplicates", None, None),
    ("seo-robots-txt-missing", "robots.txt present", "url", "/robots.txt returns non-404"),
    ("seo-sitemap-missing", "sitemap.xml present", "url", "/sitemap.xml returns non-404"),
    ("seo-viewport-missing", "Viewport meta present", None, "width=device-width"),
    ("seo-mobile-font-size", "Mobile font sizes readable", None, None),
    ("seo-og-tags-missing", "Open Graph tags present", None, "og:title/description/image"),
    ("seo-twitter-card-missing", "Twitter card present", None, None),
    ("seo-twitter-meta-incomplete", "Twitter card complete", None, None),
    # --- schema ------------------------------------------------------------
    ("seo-structured-data-missing", "Structured data present", None, "JSON-LD"),
    ("seo-structured-data-no-type", "Structured data has @type", None, None),
    ("seo-structured-data-no-name", "Structured data has a name", None, None),
    ("seo-structured-data-invalid", "Structured data parses", None, "valid JSON-LD"),
    ("seo-breadcrumb-missing", "Breadcrumb schema present", None, "BreadcrumbList"),
    ("seo-schema-no-organization", "Organization schema present", None, "Organization"),
    ("seo-schema-coverage-gap", "Recommended schema types present", None, None),
    # --- content -----------------------------------------------------------
    ("seo-content-very-thin", "Content is not very thin", None, ">= 150 words"),
    ("seo-content-thin", "Content is not thin", None, ">= 300 words"),
    ("seo-content-hard-to-read", "Content is readable", None, "Flesch reading ease >= 50"),
    ("seo-content-grade-too-high", "Reading grade not too high", None, "Gunning fog <= 16"),
    # --- keyword -----------------------------------------------------------
    ("seo-keyword-missing-in-title", "Primary keyword in title", None, None),
    ("seo-keyword-missing-in-h1", "Primary keyword in H1", None, None),
    (
        "seo-keyword-not-in-intro",
        "Primary keyword in the intro",
        None,
        "within the first 100 words",
    ),
    ("seo-keyword-stuffing", "No keyword stuffing", None, "density <= 3%"),
    # --- eeat --------------------------------------------------------------
    ("seo-eeat-no-https", "Served over HTTPS", "page_url", "https"),
    ("seo-eeat-no-privacy-link", "Privacy policy linked", None, None),
    ("seo-eeat-no-contact", "Contact information linked", None, None),
    ("seo-eeat-no-author", "Author shown", None, None),
    ("seo-eeat-no-citations", "Cites external sources", None, None),
    ("seo-eeat-no-date", "Publish/update date shown", None, None),
    ("seo-eeat-insufficient-content", "Enough content to assess E-E-A-T", None, ">= 200 words"),
    # --- internal_linking ----------------------------------------------------
    ("seo-linking-generic-anchors", "Descriptive anchor text", None, "< 3 generic anchors"),
    ("seo-linking-no-internal", "Links to other pages on the site", None, ">= 1 internal link"),
    (
        "seo-linking-insufficient-content",
        "Enough content to assess internal linking",
        None,
        ">= 200 words",
    ),
    ("seo-linking-broken", "No broken internal links", "crawl", "0 links returning 4xx/5xx"),
    ("seo-linking-orphan", "No orphaned sitemap pages", "crawl", "every sitemap page linked"),
    ("seo-linking-hub", "Internal-link hubs identified", "crawl", None),
    # --- snippet -----------------------------------------------------------
    ("seo-snippet-no-question-heading", "Question-style heading present", None, None),
    ("seo-snippet-no-concise-answer", "Concise answer under a question", None, "40-60 words"),
    ("seo-snippet-no-faq-schema", "FAQ schema present", None, "FAQPage"),
    # --- geo ---------------------------------------------------------------
    ("seo-geo-insufficient-content", "Enough content to assess AI discoverability", None, None),
    ("seo-geo-no-author-entity", "Author is a structured entity", None, "Person in JSON-LD"),
    ("seo-geo-stale-content", "Content recently updated", None, "<= 180 days old"),
    ("seo-geo-ai-crawler-blocked", "AI crawlers allowed in robots.txt", "url", "no AI bot blocked"),
    ("seo-geo-no-llms-txt", "llms.txt published", "url", "/llms.txt present"),
)

SEO_CHECK_CATALOG: tuple[CheckSpec, ...] = tuple(
    CheckSpec(check_id=cid, bucket=group_of(cid), title=title, requires=req, target=target)
    for cid, title, req, target in _ROWS
)

CATALOG_IDS: frozenset[str] = frozenset(spec.check_id for spec in SEO_CHECK_CATALOG)

# Which live probe each live check depends on: (probe, part). The audit marks
# the check ``not_run`` when that probe got no HTTP response.
LIVE_CHECK_PROBES: dict[str, tuple[str, str]] = {
    "seo-robots-txt-missing": ("technical", "robots"),
    "seo-sitemap-missing": ("technical", "sitemap"),
    "seo-geo-ai-crawler-blocked": ("geo", "robots"),
    "seo-geo-no-llms-txt": ("geo", "llms_txt"),
}
