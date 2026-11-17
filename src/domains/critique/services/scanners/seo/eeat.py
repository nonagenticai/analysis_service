"""E-E-A-T signal checks (group: ``eeat``).

Deterministic presence checks for the Trust and Authority signals Google's
quality raters and AI answer engines reward: HTTPS, privacy/contact info, author
attribution, outbound citations, and a publish/modify date. Tier 0.

The one genuinely fuzzy dimension — Experience/Expertise *depth* — is left to an
opt-in Tier-3 micro-LLM call in a later phase; everything here is computable.
Namespace ``seo-eeat-<check>`` → group ``eeat``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import main_text, words
from src.domains.critique.services.scanners.seo.signals import (
    external_link_hrefs,
    extract_content_date,
    has_author_signal,
)
from src.domains.critique.services.scanners.ux_claims import has_labelled_phone

# Authority/citation signals only make sense on real content pages.
_MIN_WORDS = 200
_PRIVACY_RE = re.compile(r"privacy", re.IGNORECASE)
_CONTACT_RE = re.compile(r"contact|get in touch|support", re.IGNORECASE)


def _has_link_like(soup: BeautifulSoup, pattern: re.Pattern[str]) -> bool:
    for a in soup.find_all("a"):
        if not isinstance(a, Tag):
            continue
        if pattern.search(a.get_text(" ", strip=True)) or pattern.search(str(a.get("href", ""))):
            return True
    return False


def check_eeat(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []

    if url:
        scheme = urlparse(url).scheme
        if scheme and scheme != "https":
            findings.append(
                _seo_finding(
                    severity="medium",
                    title="Page is not served over HTTPS",
                    description=(
                        "The page is served over plain HTTP. HTTPS is a baseline trust and "
                        "ranking signal; browsers and search engines penalise insecure pages."
                    ),
                    url=url,
                    recommendation="Serve the site over HTTPS with a valid TLS certificate.",
                    check_id="seo-eeat-no-https",
                    false_positive_risk="low",
                )
            )

    if not _has_link_like(soup, _PRIVACY_RE):
        findings.append(
            _seo_finding(
                severity="low",
                title="No privacy policy link",
                description=(
                    "No link to a privacy policy was found. A privacy policy is a core "
                    "trustworthiness signal, especially for YMYL and lead-gen pages."
                ),
                url=url,
                recommendation="Add a footer link to a privacy policy.",
                check_id="seo-eeat-no-privacy-link",
                false_positive_risk="medium",
            )
        )

    # A phone number written in the visible text is contact information even with no
    # `tel:` link and no "Contact" nav item, so asserting the absence without a phone
    # probe produced a customer-facing false positive on sites whose footer simply
    # prints the number. It must be `has_labelled_phone`, NOT `has_phone`: the latter
    # accepts any ten-digit run starting with 0, so an SKU, order number, tracking code
    # or bank account would silently suppress this finding on a page with no contact
    # details at all -- a false negative nobody can see, which is worse.
    has_contact = (
        _has_link_like(soup, _CONTACT_RE)
        or bool(soup.select_one('a[href^="mailto:"], a[href^="tel:"]'))
        or has_labelled_phone(soup)
    )
    if not has_contact:
        findings.append(
            _seo_finding(
                severity="low",
                title="No contact information",
                description=(
                    "No contact link, email, or phone was found. Reachable contact details "
                    "signal a real, accountable organisation behind the content."
                ),
                url=url,
                recommendation="Add a contact page link or a visible email/phone.",
                check_id="seo-eeat-no-contact",
                false_positive_risk="medium",
            )
        )

    if len(words(main_text(soup))) >= _MIN_WORDS:
        if not has_author_signal(soup):
            findings.append(
                _seo_finding(
                    severity="low",
                    title="No author attribution",
                    description=(
                        "The content has no author byline or author metadata. Named, credible "
                        "authorship is central to Expertise and Authoritativeness."
                    ),
                    url=url,
                    recommendation="Add a visible author byline and author metadata (schema Person).",
                    check_id="seo-eeat-no-author",
                    false_positive_risk="medium",
                )
            )

        if not external_link_hrefs(soup, url):
            findings.append(
                _seo_finding(
                    severity="low",
                    title="No outbound citations",
                    description=(
                        "The content cites no external sources. Linking to authoritative "
                        "references strengthens trust and measurably improves AI-answer citation."
                    ),
                    url=url,
                    recommendation="Cite authoritative external sources where you make factual claims.",
                    check_id="seo-eeat-no-citations",
                    false_positive_risk="medium",
                )
            )

        if extract_content_date(soup) is None:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="No publish or update date",
                    description=(
                        "No publish/modify date was found in metadata or markup. Dates signal "
                        "freshness and let search and AI engines judge currency."
                    ),
                    url=url,
                    recommendation="Expose datePublished/dateModified via Article schema or a <time> element.",
                    check_id="seo-eeat-no-date",
                    false_positive_risk="medium",
                )
            )
    else:
        findings.append(
            _seo_finding(
                severity="low",
                title="Not enough content to assess expertise & authority",
                description=(
                    "The page has too little body text to evaluate authorship, outbound "
                    "citations, or freshness. E-E-A-T cannot be established on thin content."
                ),
                url=url,
                recommendation="Add substantive, authored content with credible sources and dates.",
                check_id="seo-eeat-insufficient-content",
                false_positive_risk="low",
            )
        )

    return findings
