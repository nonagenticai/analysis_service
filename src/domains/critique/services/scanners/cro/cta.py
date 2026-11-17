"""Call-to-action quality CRO checks.

Evidence: action-verb CTAs beat generic "Submit/Click here"; first-person copy
("Start my free trial") lifts clicks (ContentVerve); reassurance microcopy near
the button reduces anxiety; long pages need the CTA repeated.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    ACTION_VERB_RE,
    FIRST_PERSON_RE,
    GENERIC_CTA_RE,
    MICROCOPY_RE,
    _cro_finding,
    cta_elements,
    cta_text,
    page_text,
)

_LONG_PAGE_CHARS = 6000


def check_cta(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    ctas = cta_elements(soup)
    if not ctas:
        return findings  # absence handled by hero.check_hero

    texts = [cta_text(el) for el in ctas]
    non_empty = [t for t in texts if t]

    # 1. Generic-only CTA copy (no action verb anywhere).
    has_action_verb = any(ACTION_VERB_RE.search(t) for t in non_empty)
    has_generic = any(GENERIC_CTA_RE.match(t.strip()) for t in non_empty)
    if non_empty and not has_action_verb and has_generic:
        findings.append(
            _cro_finding(
                severity="medium",
                title="CTAs use generic copy",
                description=(
                    "Every CTA uses generic text (e.g. 'Submit', 'Click here', 'Continue'). "
                    "Action-verb, benefit-led copy ('Get my free quote') converts better."
                ),
                url=url,
                recommendation="Rewrite CTAs to start with an action verb tied to the benefit.",
                selector="a, button",
                check_id="cro-cta-generic-text",
                evidence=", ".join(non_empty[:5]),
            )
        )

    # 2. No first-person framing.
    if non_empty and not any(FIRST_PERSON_RE.search(t) for t in non_empty):
        findings.append(
            _cro_finding(
                severity="low",
                title="No first-person CTA copy",
                description=(
                    "None of the CTAs use first-person phrasing. 'Start my free trial' has "
                    "out-clicked 'Start your free trial' in repeated tests (ContentVerve)."
                ),
                url=url,
                recommendation="Test first-person CTA copy ('my' instead of 'your').",
                selector="a, button",
                check_id="cro-cta-no-first-person",
                false_positive_risk="high",
            )
        )

    # 3. No reassurance microcopy near any CTA.
    def _near_text(el: Tag) -> str:
        parent = el.parent
        return parent.get_text(" ", strip=True) if isinstance(parent, Tag) else ""

    if not any(MICROCOPY_RE.search(_near_text(el)) for el in ctas):
        findings.append(
            _cro_finding(
                severity="low",
                title="No reassurance microcopy near CTAs",
                description=(
                    "No risk-reducing microcopy (e.g. 'No credit card required', "
                    "'Cancel anytime') was found beside a CTA."
                ),
                url=url,
                recommendation="Add a short reassurance line directly under the primary CTA.",
                selector="a, button",
                check_id="cro-cta-no-microcopy",
            )
        )

    # 4. Long page with a single CTA.
    if len(ctas) == 1 and len(page_text(soup)) > _LONG_PAGE_CHARS:
        findings.append(
            _cro_finding(
                severity="low",
                title="Only one CTA on a long page",
                description=(
                    "A long page presents the CTA once. Repeating the same primary CTA as "
                    "the visitor scrolls captures intent at multiple points."
                ),
                url=url,
                recommendation="Repeat the primary CTA every ~1-1.5 viewports on long pages.",
                selector="a, button",
                check_id="cro-cta-single-on-long-page",
            )
        )

    return findings
