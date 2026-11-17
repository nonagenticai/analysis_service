"""Structured-data coverage checks (group: ``schema``).

Goes beyond "is there any JSON-LD" (the base scanner) to "does the page carry
the schema types its business type and content warrant, and are the present
types complete?". Business type comes from the deterministic
:class:`IndustryDetector` (reused, no LLM). Tier 0/Tier 2.

Namespace ``seo-schema-<check>`` → group ``schema``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from src.domains.brand.services.industry_detector import IndustryDetector
from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.signals import (
    extract_schema_types,
    schema_objects_of_type,
)

_DETECTOR = IndustryDetector()

# Schema types worth having per detected industry (beyond the universal base).
_RECOMMENDED: dict[str, set[str]] = {
    "saas": {"Organization", "WebSite", "SoftwareApplication", "FAQPage"},
    "ecommerce": {"Organization", "Product", "Offer", "BreadcrumbList"},
    "media": {"Organization", "WebSite", "Article", "BreadcrumbList"},
    "healthcare": {"Organization", "LocalBusiness", "WebSite"},
    "finance": {"Organization", "WebSite", "FAQPage"},
    "education": {"Organization", "Course", "WebSite"},
    "agency": {"Organization", "LocalBusiness", "WebSite"},
    "nonprofit": {"Organization", "WebSite"},
}
_UNIVERSAL = {"Organization", "WebSite"}
# An Organization is satisfied by any of these concrete types.
_ORG_TYPES = {"Organization", "LocalBusiness", "Corporation", "NGO", "OnlineBusiness"}

# Required properties for the key content types (Google rich-result essentials).
_REQUIRED_PROPS: dict[str, set[str]] = {
    "Article": {"headline", "author", "datePublished", "image"},
    "BlogPosting": {"headline", "author", "datePublished", "image"},
    "NewsArticle": {"headline", "author", "datePublished", "image"},
    "Product": {"name", "offers"},
}


@dataclass(frozen=True)
class SchemaCoverage:
    """The measured structured-data coverage behind the ``seo-schema-*`` checks.

    One computation feeds both the checks and ``facts.schema``, so the report's
    coverage matrix can never disagree with the findings.
    """

    present: set[str]
    industry: str | None
    recommended: set[str]
    missing: list[str]
    # type -> missing required props, for the FIRST incomplete object of that type
    # (insertion order follows ``_REQUIRED_PROPS``).
    incomplete: dict[str, list[str]] = field(default_factory=dict)


def analyze_schema_coverage(soup: BeautifulSoup) -> SchemaCoverage:
    """Compute present / recommended / missing / incomplete schema types for a page."""
    present = extract_schema_types(soup)
    # Feed the page's own JSON-LD @types into the classifier (it scores industries
    # by schema type) instead of keyword-only.
    schema_org = {"@type": sorted(present)} if present else None
    industry = _DETECTOR.classify(soup, schema_org).industry
    recommended = _RECOMMENDED.get(industry, _UNIVERSAL)
    # Treat any Organization-ish type as covering "Organization".
    satisfied = set(present)
    if present & _ORG_TYPES:
        satisfied.add("Organization")
    incomplete: dict[str, list[str]] = {}
    for type_name, required in _REQUIRED_PROPS.items():
        for obj in schema_objects_of_type(soup, type_name):
            missing_props = sorted(p for p in required if not obj.get(p))
            if missing_props:
                incomplete[type_name] = missing_props
                break  # one entry per type is enough
    return SchemaCoverage(
        present=present,
        industry=industry or None,
        recommended=set(recommended),
        missing=sorted(recommended - satisfied),
        incomplete=incomplete,
    )


def check_schema_coverage(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    coverage = analyze_schema_coverage(soup)
    present = coverage.present

    # 1. A fundamental Organization/LocalBusiness entity.
    if not (present & _ORG_TYPES):
        findings.append(
            _seo_finding(
                severity="medium",
                title="No Organization schema",
                description=(
                    "No Organization/LocalBusiness structured data was found. This entity anchors "
                    "your brand for search knowledge panels and AI answer engines."
                ),
                url=url,
                recommendation="Add Organization (or LocalBusiness) JSON-LD with name, url and logo.",
                check_id="seo-schema-no-organization",
                false_positive_risk="medium",
            )
        )

    # 2. Industry-recommended coverage gap.
    industry = coverage.industry
    missing = coverage.missing
    if missing:
        label = f" for a {industry} site" if industry else ""
        findings.append(
            _seo_finding(
                severity="low",
                title=f"Missing recommended schema types: {', '.join(missing)}",
                description=(
                    f"The recommended structured-data types{label} are not all present. "
                    f"Missing: {', '.join(missing)}. Richer schema improves rich-result and "
                    "AI-citation eligibility."
                ),
                url=url,
                recommendation=f"Add JSON-LD for: {', '.join(missing)}.",
                check_id="seo-schema-coverage-gap",
                evidence=f"industry={industry or 'unknown'} present={sorted(present)}",
                false_positive_risk="medium",
            )
        )

    # 3. Completeness of present key types.
    for type_name, missing_props in coverage.incomplete.items():
        findings.append(
            _seo_finding(
                severity="low",
                title=f"{type_name} schema missing {', '.join(missing_props)}",
                description=(
                    f"A {type_name} JSON-LD block is missing recommended properties "
                    f"({', '.join(missing_props)}), reducing rich-result eligibility."
                ),
                url=url,
                recommendation=f"Add {', '.join(missing_props)} to the {type_name} schema.",
                check_id=f"seo-schema-{type_name.lower()}-incomplete",
                false_positive_risk="medium",
            )
        )

    return findings
