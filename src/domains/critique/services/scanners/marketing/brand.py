"""Brand & trust marketing checks (``mkt-brand-*``).

The reference rubric weights brand & trust at 10%: identity strength (logo,
favicon), authority/rich-result eligibility (structured data, social image), and
transparency (about/team). These are deliberately the *fast, HTML-only* identity
signals — the deep brand pipeline (colour/typography extraction, LLM voice
analysis) is a separate, heavier surface and is intentionally not invoked here so
the scorecard stays a single-fetch deterministic call.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import page_text
from src.domains.critique.services.scanners.marketing.base import (
    ABOUT_RE,
    CONTACT_RE,
    LOGO_RE,
    _marketing_finding,
)
from src.domains.critique.services.scanners.ux_claims import has_labelled_phone


def logo_img(soup: BeautifulSoup) -> Tag | None:
    """The logo <img> (by class/alt/id/src), else the first <img> inside the header."""
    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        haystack = " ".join(str(img.get(attr, "")) for attr in ("class", "alt", "id", "src"))
        if isinstance(img.get("class"), list):
            haystack += " " + " ".join(img.get("class") or [])
        if LOGO_RE.search(haystack):
            return img
    header = soup.find("header")
    if not isinstance(header, Tag):
        return None
    img = header.find("img")
    return img if isinstance(img, Tag) else None


def favicon_link(soup: BeautifulSoup) -> Tag | None:
    """The first ``<link>`` whose ``rel`` declares an icon."""
    for link in soup.find_all("link"):
        if isinstance(link, Tag) and "icon" in " ".join(link.get("rel") or []).lower():
            return link
    return None


def about_link(soup: BeautifulSoup) -> Tag | None:
    """The first about / team / company link (matched on label or href)."""
    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        if ABOUT_RE.search(a.get_text(" ", strip=True)) or ABOUT_RE.search(str(a.get("href", ""))):
            return a
    return None


def has_contact(soup: BeautifulSoup) -> bool:
    """Email / phone / address / contact-link signals (see the check for why labelled)."""
    contact_haystack = (
        page_text(soup)
        + " "
        + " ".join(str(a.get("href", "")) for a in soup.find_all("a") if isinstance(a, Tag))
    )
    return bool(CONTACT_RE.search(contact_haystack)) or has_labelled_phone(soup)


def check_brand_trust(soup: BeautifulSoup, url: str) -> list[dict]:
    """Detect missing brand-identity, authority, and transparency signals."""
    findings: list[dict] = []

    # 1. Structured data (Schema.org JSON-LD) — rich-result + entity authority.
    if not soup.find("script", attrs={"type": "application/ld+json"}):
        findings.append(
            _marketing_finding(
                severity="medium",
                title="No structured data (Schema.org)",
                description=(
                    "No JSON-LD structured data is present. Organization/Product schema "
                    "unlocks rich results and helps search engines model the brand as an "
                    "entity."
                ),
                url=url,
                recommendation="Add Organization (and Product/Article) JSON-LD structured data.",
                check_id="mkt-brand-no-structured-data",
            )
        )

    # 2. Open Graph image — controls how shared links present the brand.
    if not soup.find("meta", attrs={"property": "og:image"}):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No Open Graph image",
                description=(
                    "No og:image is set, so links shared on social/chat render without "
                    "branded imagery — a missed impression on every share."
                ),
                url=url,
                recommendation="Set an og:image (1200x630) for branded link previews.",
                check_id="mkt-brand-no-og-image",
            )
        )

    # 3. Favicon — browser-tab brand presence.
    if favicon_link(soup) is None:
        findings.append(
            _marketing_finding(
                severity="low",
                title="No favicon",
                description=(
                    "No favicon link is declared. The browser tab and bookmarks show a "
                    "generic icon instead of the brand mark."
                ),
                url=url,
                recommendation='Declare a favicon via <link rel="icon">.',
                check_id="mkt-brand-no-favicon",
            )
        )

    # 4. Logo presence.
    if logo_img(soup) is None:
        findings.append(
            _marketing_finding(
                severity="low",
                title="No identifiable logo",
                description=(
                    "No logo image was detected in the header or by class/alt. A visible "
                    "logo anchors brand identity and trust."
                ),
                url=url,
                recommendation="Place a logo image in the header with a descriptive alt.",
                check_id="mkt-brand-no-logo",
                false_positive_risk="high",
            )
        )

    # 5. About / team transparency link.
    if about_link(soup) is None:
        findings.append(
            _marketing_finding(
                severity="info",
                title="No about / team link",
                description=(
                    "No about, team, or company link was found. Transparency about who is "
                    "behind the offer builds credibility, especially for new visitors."
                ),
                url=url,
                recommendation="Add an About / Team link exposing the company and people.",
                check_id="mkt-brand-no-about",
                false_positive_risk="high",
            )
        )

    # 6. Contact information — accessibility of a real business behind the brand.
    # `CONTACT_RE` only sees mailto:/tel:/"contact us"/street-address copy, so a page
    # that just prints its phone number read as "No contact information" -- a false
    # accusation in a delivered report. It must be `has_labelled_phone`, NOT the raw
    # `has_phone`: that one accepts any ten-digit run starting with 0, so an SKU, order
    # number, tracking code or bank account in the footer would silently suppress this
    # finding on a page with no contact details at all.
    if not has_contact(soup):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No contact information",
                description=(
                    "No email, phone, address, or contact link was found. Accessible "
                    "contact details signal a legitimate business and lower the trust "
                    "barrier for high-consideration buyers."
                ),
                url=url,
                recommendation="Expose a contact email/phone or a clear 'Contact' link.",
                check_id="mkt-brand-no-contact-info",
                false_positive_risk="high",
            )
        )

    # 7. Social-card completeness — an og:image alone renders a poor share card.
    if soup.find("meta", attrs={"property": "og:image"}):
        has_og_title = bool(soup.find("meta", attrs={"property": "og:title"}))
        has_og_desc = bool(soup.find("meta", attrs={"property": "og:description"}))
        has_twitter_card = bool(soup.find("meta", attrs={"name": "twitter:card"}))
        if not (has_og_title and has_og_desc and has_twitter_card):
            findings.append(
                _marketing_finding(
                    severity="low",
                    title="Incomplete social-share card",
                    description=(
                        "An og:image is set but the share card is missing one of "
                        "og:title, og:description, or twitter:card — so shared links "
                        "render inconsistently across platforms."
                    ),
                    url=url,
                    recommendation="Add og:title, og:description, and twitter:card meta tags.",
                    check_id="mkt-brand-incomplete-social-card",
                )
            )

    return findings
