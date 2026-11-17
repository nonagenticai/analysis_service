"""Analytics / tracking readiness checks (``mkt-growth-*``).

The reference rubric treats measurement infrastructure as a growth-&-strategy
signal: a go-to-market site that cannot measure acquisition, attribute channels,
or run experiments is flying blind. These are detectable deterministically from
the static HTML — analytics/tag-manager/pixel snippets live in ``<script>`` src
hosts and inline code, and consent tooling in the same surface plus visible copy.

Two checks, folded into the **growth** dimension:

* ``mkt-growth-no-analytics`` — no web-analytics / tag-manager / pixel detected.
* ``mkt-growth-no-consent``   — analytics IS present but no cookie-consent
  mechanism (a privacy-hygiene gap that only matters once tracking runs).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.cro.base import page_text
from src.domains.critique.services.scanners.marketing.base import (
    ANALYTICS_RE,
    CONSENT_RE,
    _marketing_finding,
)


def script_haystack(soup: BeautifulSoup) -> str:
    """Concatenate every ``<script>`` src + inline body — where trackers live."""
    parts: list[str] = []
    for tag in soup.find_all("script"):
        src = tag.get("src")
        if src:
            parts.append(str(src))
        body = tag.string or tag.get_text()
        if body:
            parts.append(body)
    return "\n".join(parts)


def check_tracking(soup: BeautifulSoup, url: str) -> list[dict]:
    """Detect missing web analytics and (when tracking runs) missing consent."""
    findings: list[dict] = []
    scripts = script_haystack(soup)
    has_analytics = bool(ANALYTICS_RE.search(scripts))

    if not has_analytics:
        findings.append(
            _marketing_finding(
                severity="low",
                title="No web analytics or tag manager detected",
                description=(
                    "No analytics, tag-manager, or conversion-pixel snippet was found in "
                    "the static HTML (e.g. GA4, Google Tag Manager, Meta Pixel, LinkedIn "
                    "Insight, PostHog, Segment). Without measurement a site cannot attribute "
                    "channels, quantify conversion, or run experiments. NOTE: trackers "
                    "injected client-side by a SPA may not be visible to a static fetch."
                ),
                url=url,
                recommendation=(
                    "Install a web-analytics platform and, if running paid acquisition, "
                    "the relevant conversion pixels — ideally via a tag manager. If already "
                    "present via a JS bundle, expose it in the initial HTML for reliability."
                ),
                check_id="mkt-growth-no-analytics",
                false_positive_risk="high",
            )
        )
    else:
        # Consent only matters once tracking is actually running.
        consent_haystack = f"{scripts}\n{page_text(soup)}"
        if not CONSENT_RE.search(consent_haystack):
            findings.append(
                _marketing_finding(
                    severity="low",
                    title="Tracking present but no cookie-consent mechanism",
                    description=(
                        "Analytics or tracking scripts were detected but no cookie-consent "
                        "or privacy-preference mechanism was found. In many jurisdictions "
                        "this is a compliance risk and it erodes visitor trust."
                    ),
                    url=url,
                    recommendation=(
                        "Add a consent-management banner (e.g. OneTrust, Cookiebot, "
                        "Usercentrics) gating non-essential tracking."
                    ),
                    check_id="mkt-growth-no-consent",
                    false_positive_risk="medium",
                )
            )

    return findings
