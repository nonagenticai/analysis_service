"""Shared finding factory + regexes for the marketing scanner package.

The marketing scorecard blends three *reused* engines — the CRO conversion
scanner, the SEO audit scanner, and the brand/consistency signals — into a
go-to-market composite. Three go-to-market dimensions have no existing scanner,
so this package adds the light deterministic checks for them:

* ``competitive`` — differentiation, comparison surfaces, third-party reputation.
* ``brand``       — identity + authority markers (logo, favicon, OG image,
  structured data, about/team transparency).
* ``growth``      — pricing visibility, growth loops, retention/nurture hooks.

Every check emits a finding through :func:`_marketing_finding`, which pins
``category="marketing"`` and ``source_tool="marketing_scanner"``. Findings carry
a stable ``check_id`` in the ``mkt-<dimension>-<check>`` namespace — the
``<dimension>`` token is what :func:`marketing.scoring` groups by.

Like CRO, marketing is modelled as *absence detection*: a check emits a penalty
finding when a go-to-market signal is missing or weak. A page that presents
itself well emits no findings for that dimension and scores 100.
"""

from __future__ import annotations

import re

from src.domains.critique.services.scanners.base import make_finding

# Reuse the CRO reputation regex so "featured in Forbes/TechCrunch" is detected
# identically across both scanners.
from src.domains.critique.services.scanners.cro.base import MEDIA_MENTION_RE

__all__ = [
    "MEDIA_MENTION_RE",
    "_marketing_finding",
]


def _marketing_finding(
    *,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    evidence: str | None = None,
    false_positive_risk: str = "medium",
    remediation_effort: str = "low",
) -> dict:
    """Marketing scanner finding with default metadata."""
    return make_finding(
        category="marketing",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        evidence=evidence,
        source_tool="marketing_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )


# --------------------------------------------------------------------------
# Dimension token regexes (compiled once). Deterministic signal detectors — the
# "intelligence" is which token maps to which go-to-market signal at what severity.
# --------------------------------------------------------------------------

# Competitive: comparison / "vs" surfaces.
COMPARISON_RE = re.compile(
    r"\b(vs\.?|versus|compare|comparison|alternative to|alternatives?|"
    r"why choose|switch from|migrate from)\b",
    re.IGNORECASE,
)
# Competitive: differentiation / category-defining claims in the fold copy.
DIFFERENTIATION_RE = re.compile(
    r"\b(only|unlike|first|leading|#\s*1|number one|best|most|fastest|"
    r"unique|patented|proprietary|purpose-built|world[- ]class)\b",
    re.IGNORECASE,
)
# Competitive: third-party review platforms (authority / reputation).
REVIEW_PLATFORM_RE = re.compile(
    r"\bg2\b|capterra|trustpilot|trustradius|gartner|forrester|producthunt|"
    r"product hunt|clutch\.co|glassdoor",
    re.IGNORECASE,
)

# Growth: money / pricing signals in copy.
PRICE_RE = re.compile(
    r"[$£€]\s?\d|\bfree\b|\/\s?(mo|month|yr|year)\b|per (month|year|user|seat)|"
    r"\bpricing\b|\bplans?\b|\btiers?\b",
    re.IGNORECASE,
)
# Growth: viral / referral / advocacy loops.
GROWTH_LOOP_RE = re.compile(
    r"refer(ral)?|affiliate|invite (a )?friend|share and|earn credit|"
    r"reward(s)?|ambassador|partner program",
    re.IGNORECASE,
)
# Growth: retention / nurture surfaces.
RETENTION_RE = re.compile(
    r"newsletter|subscribe|sign up for|community|slack|discord|forum|"
    r"\bblog\b|updates|changelog|webinar|academy|resources",
    re.IGNORECASE,
)

# Brand: about / team / company transparency links.
ABOUT_RE = re.compile(
    r"about|our team|company|mission|our story|who we are|meet the team",
    re.IGNORECASE,
)
# Brand: logo identification (class / alt / id / src).
LOGO_RE = re.compile(r"logo|brand[- ]?mark|wordmark", re.IGNORECASE)
# Brand: contact-information signals (mailto/tel links or address copy).
CONTACT_RE = re.compile(
    r"mailto:|tel:|\bcontact us\b|\bget in touch\b|"
    r"\b\d{1,4}\s+\w+\s+(street|st\.?|avenue|ave\.?|road|rd\.?|blvd)\b",
    re.IGNORECASE,
)

# Growth: analytics / tag-manager / pixel / product-analytics platforms. Matched
# against inline snippet code and against <script src> hosts. Mirrors the
# reference `analyze_page.py` platform list (16+ platforms + inline signals).
# One row per vendor so the facts block can NAME what it saw; ``ANALYTICS_RE`` is
# the alternation of every row's patterns, so the check and the facts can never
# disagree on whether analytics is present.
ANALYTICS_VENDORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Google Analytics", (r"gtag\(", r"google-analytics\.com", r"ga\('create'")),
    ("Google Tag Manager", (r"googletagmanager\.com", r"dataLayer")),
    ("Meta Pixel", (r"fbq\(", r"connect\.facebook\.net")),
    ("LinkedIn Insight", (r"_linkedin_partner_id", r"snap\.licdn\.com")),
    ("Hotjar", (r"static\.hotjar\.com", r"\bhj\(")),
    ("FullStory", (r"fullstory\.com", r"\bFS\.identify")),
    ("Microsoft Clarity", (r"clarity\.ms", r"clarity\(")),
    ("Segment", (r"segment\.com/analytics\.js", r"analytics\.track\(")),
    ("Mixpanel", (r"mixpanel",)),
    ("PostHog", (r"posthog",)),
    ("Plausible", (r"plausible\.io",)),
    ("Matomo", (r"matomo",)),
    ("Amplitude", (r"amplitude",)),
    ("Heap", (r"heap\.io", r"\bheap\.")),
)
ANALYTICS_VENDOR_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile("|".join(alts), re.IGNORECASE)) for name, alts in ANALYTICS_VENDORS
)
ANALYTICS_RE = re.compile(
    "|".join(alt for _, alts in ANALYTICS_VENDORS for alt in alts),
    re.IGNORECASE,
)
# Growth: cookie / consent-management signals (privacy hygiene when tracking runs).
CONSENT_RE = re.compile(
    r"cookieconsent|cookie[- ]?consent|onetrust|cookiebot|osano|termly|iubenda"
    r"|usercentrics|gdpr|\bconsent\b|cookie (policy|preferences|settings)"
    r"|\baccept (all )?cookies\b",
    re.IGNORECASE,
)
# Growth: social-profile presence (a brand's owned channels). One row per
# platform for the facts block; ``SOCIAL_PROFILE_RE`` is their alternation.
SOCIAL_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("x", r"(twitter|x)\.com/"),
    ("linkedin", r"linkedin\.com/(company|in)/"),
    ("facebook", r"facebook\.com/"),
    ("instagram", r"instagram\.com/"),
    ("youtube", r"youtube\.com/(c/|channel/|@)"),
    ("github", r"github\.com/"),
    ("tiktok", r"tiktok\.com/@"),
    ("mastodon", r"mastodon"),
)
SOCIAL_PLATFORM_RES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in SOCIAL_PLATFORMS
)
SOCIAL_PROFILE_RE = re.compile(
    "|".join(pattern for _, pattern in SOCIAL_PLATFORMS),
    re.IGNORECASE,
)
