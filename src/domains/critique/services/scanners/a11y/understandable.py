"""Understandable WCAG checks (lang, form labels, link text, headings, autocomplete)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.a11y.base import _a11y_finding


def check_lang(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    html_tag = soup.find("html")
    if html_tag and isinstance(html_tag, Tag):
        lang = html_tag.get("lang", "")
        if not lang:
            findings.append(
                _a11y_finding(
                    severity="high",
                    title="Missing lang attribute",
                    description="The <html> element is missing a lang attribute (WCAG 3.1.1).",
                    url=url,
                    recommendation='Add lang="en" (or appropriate language) to the <html> tag.',
                    selector="html",
                    check_id="a11y-missing-lang",
                    wcag_criterion="3.1.1",
                )
            )
    return findings


def check_lang_validity(soup: BeautifulSoup, url: str) -> list[dict]:
    """Validate the lang attribute value on <html> (WCAG 3.1.1)."""
    findings: list[dict] = []
    html_tag = soup.find("html")
    if not html_tag or not isinstance(html_tag, Tag):
        return findings

    lang = html_tag.get("lang", "")
    if isinstance(lang, list):
        lang = lang[0] if lang else ""
    lang = str(lang).strip()
    if not lang:
        return findings  # Missing lang is caught by check_lang

    # Valid: 2-3 letter primary subtag, optional region/script subtags
    valid_lang_re = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")
    if not valid_lang_re.match(lang):
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"Invalid lang attribute value: '{lang}'",
                description=(
                    f"The <html> lang attribute value '{lang}' does not "
                    "match a valid BCP 47 language tag pattern. An "
                    "invalid language code prevents assistive "
                    "technologies from selecting the correct speech "
                    "synthesizer (WCAG 3.1.1)."
                ),
                url=url,
                recommendation=(
                    "Set the lang attribute to a valid ISO 639-1 "
                    "language code (e.g. 'en', 'fr', 'de', 'en-US')."
                ),
                selector="html",
                check_id="a11y-lang-invalid",
                wcag_criterion="3.1.1",
                evidence=f'lang="{lang}"',
            )
        )
    return findings


def check_form_labels(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    inputs = soup.find_all(["input", "select", "textarea"])
    unlabeled = []

    for inp in inputs:
        if not isinstance(inp, Tag):
            continue
        input_type = inp.get("type", "text")
        if input_type in ("hidden", "submit", "button", "reset", "image"):
            continue

        input_id = inp.get("id", "")
        has_label = bool(input_id and soup.find("label", attrs={"for": input_id}))
        has_aria = bool(inp.get("aria-label") or inp.get("aria-labelledby"))
        has_title = bool(inp.get("title"))

        if not has_label and not has_aria and not has_title:
            parent = inp.parent
            if not (parent and parent.name == "label"):
                unlabeled.append(inp)

    if unlabeled:
        findings.append(
            _a11y_finding(
                severity="high",
                title=f"{len(unlabeled)} form inputs without labels",
                description=(
                    f"{len(unlabeled)} form inputs lack associated labels (WCAG 1.3.1, 4.1.2)."
                ),
                url=url,
                recommendation=(
                    "Associate each input with a <label> element using for/id attributes."
                ),
                selector="input:not([aria-label]):not([title])",
                check_id="a11y-form-no-label",
                wcag_criterion="1.3.1",
            )
        )

    return findings


def check_link_text(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    bad_texts = {"click here", "here", "read more", "more", "link", "this"}
    links = soup.find_all("a")
    bad_links = []

    for link in links:
        if not isinstance(link, Tag):
            continue
        text = link.get_text(strip=True).lower()
        if text in bad_texts and not link.get("aria-label"):
            bad_links.append(text)

    if bad_links:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{len(bad_links)} links with non-descriptive text",
                description=f"Links use generic text like '{bad_links[0]}' (WCAG 2.4.4).",
                url=url,
                recommendation=(
                    "Use descriptive link text that explains the destination or purpose."
                ),
                selector="a",
                check_id="a11y-generic-link-text",
                wcag_criterion="2.4.4",
            )
        )

    return findings


def check_heading_order(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    if not headings:
        return findings

    levels = [int(h.name[1]) for h in headings]
    if levels and levels[0] != 1:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"First heading is H{levels[0]}, not H1",
                description="Page content should begin with an H1 heading (WCAG 1.3.1).",
                url=url,
                recommendation="Start the page content with an H1 heading.",
                check_id="a11y-heading-not-h1",
                wcag_criterion="1.3.1",
            )
        )

    for i in range(1, len(levels)):
        if levels[i] > levels[i - 1] + 1:
            findings.append(
                _a11y_finding(
                    severity="medium",
                    title=f"Heading level skip: H{levels[i - 1]} to H{levels[i]}",
                    description=(
                        f"Heading hierarchy skips from H{levels[i - 1]} to H{levels[i]} "
                        "(WCAG 1.3.1)."
                    ),
                    url=url,
                    recommendation="Use sequential heading levels without skipping.",
                    check_id="a11y-heading-skip",
                    wcag_criterion="1.3.1",
                )
            )
            break

    return findings


def check_autocomplete(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for autocomplete attributes on identity fields (WCAG 1.3.5)."""
    findings: list[dict] = []
    # Patterns that suggest identity/personal fields
    identity_patterns = re.compile(
        r"(name|email|phone|tel|address|postal|zip|city|country|"
        r"cc-?number|card|fname|lname|first.?name|last.?name|"
        r"username|login)",
        re.IGNORECASE,
    )
    identity_types = {"text", "email", "tel", "password", "url"}

    missing_autocomplete: list[Tag] = []
    for inp in soup.find_all("input"):
        if not isinstance(inp, Tag):
            continue
        input_type = str(inp.get("type", "text")).lower()
        if input_type not in identity_types:
            continue
        # Skip if already has autocomplete
        if inp.get("autocomplete"):
            continue
        # Check if name/id suggests identity field
        name = str(inp.get("name", ""))
        input_id = str(inp.get("id", ""))
        if identity_patterns.search(name) or identity_patterns.search(input_id):
            missing_autocomplete.append(inp)

    if missing_autocomplete:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{len(missing_autocomplete)} identity inputs missing autocomplete",
                description=(
                    f"{len(missing_autocomplete)} input field(s) that appear to "
                    "collect personal information lack the autocomplete "
                    "attribute (WCAG 1.3.5). Autocomplete helps users fill "
                    "forms faster and assists assistive technologies."
                ),
                url=url,
                recommendation=(
                    "Add appropriate autocomplete values (e.g. "
                    'autocomplete="email", autocomplete="name") to '
                    "identity-related input fields."
                ),
                selector="input",
                check_id="a11y-autocomplete-missing",
                wcag_criterion="1.3.5",
            )
        )
    return findings
