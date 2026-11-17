"""Footer / secondary-CTA CRO checks.

By the time a visitor reaches the end of the page they are warm — a closing CTA
and easy contact capture that intent. Approximates "bottom of page" as the
<footer> element, falling back to the last top-level section of the body.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import _cro_finding, cta_elements

_CONTACT_RE = re.compile(r"contact|support|help|email|phone", re.IGNORECASE)


def _bottom_region(soup: BeautifulSoup) -> Tag | None:
    footer = soup.find("footer")
    if isinstance(footer, Tag):
        return footer
    body = soup.find("body")
    if isinstance(body, Tag):
        sections = [c for c in body.find_all(["section", "div"], recursive=False)]
        if sections:
            return sections[-1]
    return None


def has_bottom_region(soup: BeautifulSoup) -> bool:
    """True when the page has a footer (or a last top-level body section) to check.

    Without one, :func:`check_footer` runs no check at all, so the ``footer``
    section is NOT measured — the caller must report it unmeasured, never a
    clean 100 (design 2026-10-17 §6: no silent passes).
    """
    return _bottom_region(soup) is not None


def check_footer(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    region = _bottom_region(soup)
    if region is None:
        return findings

    # 1. Closing CTA in the bottom region.
    bottom_ctas = cta_elements(region)
    if not bottom_ctas:
        findings.append(
            _cro_finding(
                severity="low",
                title="No closing CTA",
                description=(
                    "The bottom of the page has no call-to-action. Visitors who read to the "
                    "end are the warmest — give them a final way to convert."
                ),
                url=url,
                recommendation="Repeat the primary CTA in a closing section or the footer.",
                selector="footer",
                check_id="cro-footer-no-final-cta",
            )
        )

    # 2. Contact / support reachability at the bottom.
    region_text = region.get_text(" ", strip=True)
    has_contact_link = any(
        str(a.get("href", "")).startswith(("mailto:", "tel:"))
        or _CONTACT_RE.search(a.get_text(" ", strip=True))
        for a in region.find_all("a")
        if isinstance(a, Tag)
    )
    if not has_contact_link and not _CONTACT_RE.search(region_text):
        findings.append(
            _cro_finding(
                severity="low",
                title="No contact/support link in footer",
                description=(
                    "The footer offers no contact or support link. Easy reachability builds "
                    "trust and captures late-stage questions."
                ),
                url=url,
                recommendation="Add contact/support links (and an email/phone) to the footer.",
                selector="footer",
                check_id="cro-footer-no-contact",
            )
        )

    return findings
