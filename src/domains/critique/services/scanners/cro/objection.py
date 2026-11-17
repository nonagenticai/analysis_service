"""Objection-handling CRO checks.

Detects whether the page proactively answers the questions and risks that stall
a conversion: FAQ, risk reversal (guarantee / free trial), security & privacy
assurance, and basic business legitimacy (contact/phone/address).
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    _FAQ_CLASS_RE,
    FREE_TRIAL_RE,
    GUARANTEE_RE,
    SECURITY_RE,
    _cro_finding,
    page_text,
)

_CONTACT_LINK_RE = re.compile(r"contact|about|support|help", re.IGNORECASE)


def objection_signals(soup: BeautifulSoup, text: str | None = None) -> dict[str, bool]:
    """Presence of each objection-handling signal — shared by the checks and facts."""
    text = page_text(soup) if text is None else text
    anchors = [a for a in soup.find_all("a") if isinstance(a, Tag)]
    hrefs = [str(a.get("href", "")) for a in anchors]
    has_faq = (
        soup.find(attrs={"class": _FAQ_CLASS_RE}) is not None
        or soup.find("details") is not None
        or "frequently asked" in text.lower()
    )
    has_privacy_link = any("privacy" in h.lower() or "security" in h.lower() for h in hrefs)
    has_contact = any(h.startswith(("mailto:", "tel:")) for h in hrefs) or any(
        _CONTACT_LINK_RE.search(a.get_text(" ", strip=True)) for a in anchors
    )
    return {
        "faq": has_faq,
        "risk_reversal": bool(GUARANTEE_RE.search(text) or FREE_TRIAL_RE.search(text)),
        "security_assurance": bool(SECURITY_RE.search(text)) or has_privacy_link,
        "contact": has_contact,
    }


def check_objection(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    signals = objection_signals(soup)

    # 1. FAQ.
    if not signals["faq"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No FAQ section",
                description=(
                    "No FAQ or accordion was found. An FAQ pre-empts the objections that "
                    "otherwise stall a decision."
                ),
                url=url,
                recommendation="Add an FAQ answering the top 4-6 buyer objections.",
                check_id="cro-objection-no-faq",
            )
        )

    # 2. Risk reversal.
    if not signals["risk_reversal"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No risk reversal",
                description=(
                    "No guarantee, refund, or free-trial language was found. Risk reversal "
                    "shifts perceived risk off the visitor."
                ),
                url=url,
                recommendation="Offer a money-back guarantee or free trial and state it clearly.",
                check_id="cro-objection-no-risk-reversal",
            )
        )

    # 3. Security / privacy assurance.
    if not signals["security_assurance"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No security or privacy assurance",
                description=(
                    "No security/privacy language or privacy link was found. Visitors "
                    "handing over data look for reassurance it is safe."
                ),
                url=url,
                recommendation="Add a privacy link and a short security/data-handling statement.",
                check_id="cro-objection-no-trust-links",
            )
        )

    # 4. Business legitimacy.
    if not signals["contact"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No contact or legitimacy signals",
                description=(
                    "No contact link, email/phone link, or About/Support link was found. "
                    "Reachability signals a real, accountable business."
                ),
                url=url,
                recommendation="Add visible contact details and an About/Contact link.",
                check_id="cro-objection-no-legitimacy",
            )
        )

    return findings
