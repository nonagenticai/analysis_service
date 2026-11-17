"""Hero / above-the-fold CRO checks.

Note on "above the fold": server-side HTML has no layout geometry, so these are
document-level presence/structure checks (one H1, a CTA exists, headline
length, nav-link distraction, a trust signal is present) rather than true
pixel-fold checks — honest to what is computable without a render.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    TRUST_BADGE_RE,
    _cro_finding,
    cta_elements,
    page_text,
)


def nav_link_elements(soup: BeautifulSoup) -> list[Tag]:
    """Links inside every ``<nav>``/``<header>`` (nested containers count twice,
    exactly as the attention-ratio check always has)."""
    nav_links: list[Tag] = []
    for container in soup.find_all(["nav", "header"]):
        if isinstance(container, Tag):
            nav_links.extend(a for a in container.find_all("a") if isinstance(a, Tag))
    return nav_links


def check_hero(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []

    # 1. Exactly one H1.
    h1s = [h for h in soup.find_all("h1") if isinstance(h, Tag)]
    if not h1s:
        findings.append(
            _cro_finding(
                severity="high",
                title="No H1 headline",
                description=(
                    "The page has no <h1>. Visitors decide within ~5 seconds; a clear "
                    "headline stating the value proposition is the single most important "
                    "above-fold element."
                ),
                url=url,
                recommendation="Add one <h1> that states the core benefit in plain language.",
                selector="h1",
                check_id="cro-hero-no-h1",
                false_positive_risk="low",
            )
        )
    elif len(h1s) > 1:
        findings.append(
            _cro_finding(
                severity="low",
                title=f"{len(h1s)} H1 headlines",
                description=("Multiple <h1>s dilute the single-message focus a hero needs."),
                url=url,
                recommendation="Keep exactly one <h1>; demote the rest to <h2>.",
                selector="h1",
                check_id="cro-hero-multiple-h1",
            )
        )

    # 2. Headline length (scannable in ~5 seconds).
    if h1s:
        text = h1s[0].get_text(" ", strip=True)
        words = len(text.split())
        chars = len(text)
        if words and (words < 3 or words > 12 or chars > 70):
            findings.append(
                _cro_finding(
                    severity="low",
                    title="Headline length is off-target",
                    description=(
                        f"The H1 is {words} words / {chars} chars. Punchy hero headlines "
                        "are ~6-12 words and scannable in one glance."
                    ),
                    url=url,
                    recommendation="Tighten the headline to ~6-12 words focused on the benefit.",
                    selector="h1",
                    check_id="cro-hero-headline-length",
                    evidence=text[:120],
                )
            )

    # 3. A primary CTA exists.
    if not cta_elements(soup):
        findings.append(
            _cro_finding(
                severity="high",
                title="No call-to-action found",
                description=(
                    "No button or action link was detected. A landing page with no clear "
                    "CTA cannot convert."
                ),
                url=url,
                recommendation="Add a prominent primary CTA button above the fold.",
                selector="a, button",
                check_id="cro-hero-no-cta",
                false_positive_risk="low",
            )
        )

    # 4. Nav-link distraction (attention ratio).
    nav_links = nav_link_elements(soup)
    if len(nav_links) > 6:
        findings.append(
            _cro_finding(
                severity="medium",
                title=f"{len(nav_links)} competing nav links",
                description=(
                    f"The header/nav offers {len(nav_links)} links, pulling attention away "
                    "from the conversion goal. Dedicated landing pages convert best near a "
                    "1:1 attention ratio (Unbounce)."
                ),
                url=url,
                recommendation="Trim top navigation on conversion pages to the essentials.",
                selector="nav a, header a",
                check_id="cro-hero-nav-distraction",
            )
        )

    # 5. A trust signal is present somewhere on the page.
    if not TRUST_BADGE_RE.search(page_text(soup)):
        findings.append(
            _cro_finding(
                severity="low",
                title="No trust signal detected",
                description=(
                    "No trust badge, guarantee, or security/authority mark was found. "
                    "Above-fold trust cues reduce first-impression risk."
                ),
                url=url,
                recommendation="Add a trust badge, guarantee, or recognised security mark.",
                check_id="cro-hero-no-trust-signal",
            )
        )

    return findings
