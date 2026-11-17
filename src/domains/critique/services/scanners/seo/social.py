"""Social SEO checks (Open Graph, Twitter Cards)."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo.base import _seo_finding


def check_og_tags(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    required_og = ["og:title", "og:description", "og:image", "og:url"]
    missing = []
    for prop in required_og:
        tag = soup.find("meta", attrs={"property": prop})
        if not tag:
            missing.append(prop)
    if missing:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Missing Open Graph tags",
                description=(
                    f"Missing OG tags: {', '.join(missing)}. "
                    "Social sharing will lack rich previews."
                ),
                url=url,
                recommendation=f"Add the following Open Graph meta tags: {', '.join(missing)}.",
                check_id="seo-og-tags-missing",
            )
        )
    return findings


def check_twitter_cards(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for Twitter Card meta tags."""
    findings: list[dict] = []

    twitter_card = soup.find("meta", attrs={"name": "twitter:card"})
    if not twitter_card:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Missing Twitter Card tag",
                description=(
                    "No twitter:card meta tag found. Twitter shares will lack rich card previews."
                ),
                url=url,
                recommendation=(
                    'Add <meta name="twitter:card" content="summary_large_image"> '
                    "to enable Twitter Card previews."
                ),
                selector='meta[name="twitter:card"]',
                check_id="seo-twitter-card-missing",
            )
        )

    twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
    twitter_desc = soup.find("meta", attrs={"name": "twitter:description"})
    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    missing_tags = []
    if not twitter_title:
        missing_tags.append("twitter:title")
    if not twitter_desc:
        missing_tags.append("twitter:description")
    if not twitter_image:
        missing_tags.append("twitter:image")

    if missing_tags:
        findings.append(
            _seo_finding(
                severity="low",
                title="Incomplete Twitter Card meta tags",
                description=(
                    f"Missing Twitter meta tags: {', '.join(missing_tags)}. "
                    "Complete tags improve social sharing appearance."
                ),
                url=url,
                recommendation=(f"Add the following Twitter meta tags: {', '.join(missing_tags)}."),
                check_id="seo-twitter-meta-incomplete",
            )
        )

    return findings
