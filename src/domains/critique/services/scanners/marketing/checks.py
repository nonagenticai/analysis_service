"""Static check catalog for the marketing scanner (design 2026-10-17 X-G1).

One :class:`CheckSpec` per ``mkt-*`` check the scanner can emit — 17 ids:
brand 7, competitive 3, growth 5 + tracking 2 (both ``mkt-growth-*``). The
scorecard's ``checks[]`` inventory is built from this catalog, so a check that
did not fire reads ``pass`` instead of being invisible.

``mkt-growth-no-consent`` only runs when analytics is present (consent is moot
without tracking), so it ``requires="analytics"`` and reads ``not_run`` on a
page with no tracker — never a silent ``pass``. Likewise
``mkt-brand-incomplete-social-card`` only runs once an og:image is set
(``requires="og_image"``).

A unit test greps every ``mkt-*`` literal out of this package and asserts it is
catalogued here, so a new check cannot ship without an inventory row.
"""

from __future__ import annotations

from src.domains.scorecards.findings import CheckSpec

__all__ = ["MARKETING_CHECKS", "NOT_RUN_REASONS"]

MARKETING_CHECKS: tuple[CheckSpec, ...] = (
    # competitive (competitive.py)
    CheckSpec(
        "mkt-competitive-weak-differentiation",
        "competitive",
        "Differentiation claim in the headline area",
        target="An H1/H2 states a point of difference or category position",
    ),
    CheckSpec(
        "mkt-competitive-no-comparison",
        "competitive",
        "Comparison or alternatives content",
        target="A 'vs' / 'alternative to' / comparison section or link",
    ),
    CheckSpec(
        "mkt-competitive-no-reputation",
        "competitive",
        "Third-party reputation signals",
        target="A review platform (G2, Capterra, Trustpilot…) or media mention",
    ),
    # brand (brand.py)
    CheckSpec(
        "mkt-brand-no-structured-data",
        "brand",
        "Structured data (Schema.org)",
        target="At least one JSON-LD block (Organization / Product)",
    ),
    CheckSpec(
        "mkt-brand-no-og-image",
        "brand",
        "Open Graph image",
        target='<meta property="og:image"> set (1200x630)',
    ),
    CheckSpec("mkt-brand-no-favicon", "brand", "Favicon", target='<link rel="icon"> declared'),
    CheckSpec(
        "mkt-brand-no-logo",
        "brand",
        "Identifiable logo",
        target="A logo image in the header or tagged logo by class/alt",
    ),
    CheckSpec(
        "mkt-brand-no-about",
        "brand",
        "About / team link",
        target="A link to an About / Team / Company page",
    ),
    CheckSpec(
        "mkt-brand-no-contact-info",
        "brand",
        "Contact information",
        target="An email, labelled phone, address or contact link",
    ),
    CheckSpec(
        "mkt-brand-incomplete-social-card",
        "brand",
        "Complete social-share card",
        requires="og_image",
        target="og:title + og:description + twitter:card alongside og:image",
    ),
    # growth (growth.py)
    CheckSpec(
        "mkt-growth-no-pricing-visibility",
        "growth",
        "Visible pricing or packaging",
        target="Price / plans copy or a pricing link",
    ),
    CheckSpec(
        "mkt-growth-no-growth-loop",
        "growth",
        "Growth loop (referral / advocacy)",
        target="A referral, affiliate or invite mechanism",
    ),
    CheckSpec(
        "mkt-growth-no-retention-hook",
        "growth",
        "Retention / nurture hook",
        target="A newsletter, community, blog or content hub",
    ),
    CheckSpec(
        "mkt-growth-no-email-capture",
        "growth",
        "Email-capture affordance",
        target="An email / newsletter input",
    ),
    CheckSpec(
        "mkt-growth-no-social-presence",
        "growth",
        "Owned social channels linked",
        target="At least one link to an owned social profile",
    ),
    # growth — tracking (tracking.py)
    CheckSpec(
        "mkt-growth-no-analytics",
        "growth",
        "Web analytics or tag manager",
        target="An analytics / tag-manager / pixel snippet in the HTML",
    ),
    CheckSpec(
        "mkt-growth-no-consent",
        "growth",
        "Cookie-consent mechanism",
        requires="analytics",
        target="A consent banner / CMP gating the detected trackers",
    ),
)

NOT_RUN_REASONS: dict[str, str] = {
    "og_image": "No og:image — the share-card check only runs once an og:image is set",
    "analytics": "No analytics detected — consent only applies once tracking runs",
}
