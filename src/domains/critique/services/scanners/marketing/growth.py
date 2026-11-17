"""Growth & strategy marketing checks (``mkt-growth-*``).

The reference rubric weights growth & strategy at 10%: is pricing/packaging
visible (monetisation clarity), is there a growth loop (referral/advocacy), and
is there a retention/nurture hook (newsletter/community/content). All three are
detectable as keyword/structural signals in the static HTML.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import page_text
from src.domains.critique.services.scanners.marketing.base import (
    GROWTH_LOOP_RE,
    PRICE_RE,
    RETENTION_RE,
    SOCIAL_PROFILE_RE,
    _marketing_finding,
)


def has_email_capture(soup: BeautifulSoup) -> bool:
    """True if the page exposes an email-capture affordance (input or form)."""
    for inp in soup.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        itype = str(inp.get("type", "")).lower()
        name = f"{inp.get('name', '')} {inp.get('id', '')} {inp.get('placeholder', '')}".lower()
        if itype == "email" or "email" in name or "newsletter" in name:
            return True
    return False


def social_links(soup: BeautifulSoup) -> list[str]:
    """Every href that links out to an owned social channel, in page order."""
    return [
        str(a.get("href", ""))
        for a in soup.find_all("a")
        if isinstance(a, Tag) and SOCIAL_PROFILE_RE.search(str(a.get("href", "")))
    ]


def has_pricing(soup: BeautifulSoup, text: str) -> bool:
    """Price / plan copy, or a link whose href points at pricing or plans."""
    return bool(PRICE_RE.search(text)) or any(
        "pric" in str(a.get("href", "")).lower() or "plan" in str(a.get("href", "")).lower()
        for a in soup.find_all("a")
        if isinstance(a, Tag)
    )


def check_growth(soup: BeautifulSoup, url: str) -> list[dict]:
    """Detect missing pricing visibility, growth loops, and retention hooks."""
    findings: list[dict] = []
    text = page_text(soup)

    # 1. Pricing / packaging visibility.
    if not has_pricing(soup, text):
        findings.append(
            _marketing_finding(
                severity="medium",
                title="No visible pricing or packaging",
                description=(
                    "No pricing, plans, or price signals were found. Hidden pricing adds "
                    "friction and filters out self-serve buyers who want to qualify "
                    "themselves before talking to sales."
                ),
                url=url,
                recommendation=(
                    "Expose pricing or a plans/packaging section (or a clear 'contact for "
                    "pricing' path)."
                ),
                check_id="mkt-growth-no-pricing-visibility",
                false_positive_risk="high",
            )
        )

    # 2. Growth loop (referral / advocacy).
    if not GROWTH_LOOP_RE.search(text):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No growth loop (referral / advocacy)",
                description=(
                    "No referral, affiliate, or invite mechanism is present. Growth loops "
                    "turn existing users into an acquisition channel and lower CAC over time."
                ),
                url=url,
                recommendation="Add a referral, affiliate, or invite-a-colleague loop.",
                check_id="mkt-growth-no-growth-loop",
                false_positive_risk="high",
            )
        )

    # 3. Retention / nurture hook.
    if not RETENTION_RE.search(text):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No retention / nurture hook",
                description=(
                    "No newsletter, community, blog, or content hook was found. Visitors "
                    "who are not ready to buy leave with no way to stay in the funnel."
                ),
                url=url,
                recommendation=(
                    "Add a newsletter signup, community link, or content hub to nurture "
                    "not-yet-ready visitors."
                ),
                check_id="mkt-growth-no-retention-hook",
                false_positive_risk="high",
            )
        )

    # 4. Email capture (lead nurture entry point).
    if not has_email_capture(soup):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No email-capture affordance",
                description=(
                    "No email input or newsletter form was found. Email is the highest-ROI "
                    "owned channel; without a capture point, visitors who aren't ready to "
                    "buy leave with no way to be nurtured back."
                ),
                url=url,
                recommendation="Add an email/newsletter capture form to a prominent surface.",
                check_id="mkt-growth-no-email-capture",
                false_positive_risk="high",
            )
        )

    # 5. Owned social-channel presence (multi-channel acquisition + credibility).
    if not social_links(soup):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No owned social channels linked",
                description=(
                    "No links to owned social profiles (LinkedIn, X, YouTube, etc.) were "
                    "found. Social channels diversify acquisition and provide social proof "
                    "of an active, credible brand."
                ),
                url=url,
                recommendation="Link the brand's active social profiles (typically in the footer).",
                check_id="mkt-growth-no-social-presence",
                false_positive_risk="medium",
            )
        )

    return findings
