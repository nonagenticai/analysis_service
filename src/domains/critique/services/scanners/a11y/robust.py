"""Robust WCAG checks (ARIA roles, landmarks, button names, iframe, aria-live)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.a11y.base import (
    _a11y_finding,
    _has_focusable_child,
)


def check_aria_roles(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []

    clickable_divs = soup.find_all(
        ["div", "span"],
        attrs={"onclick": True},
    )
    for elem in clickable_divs:
        if not isinstance(elem, Tag):
            continue
        if not elem.get("role") and not elem.get("tabindex"):
            findings.append(
                _a11y_finding(
                    severity="high",
                    title=f"Clickable <{elem.name}> without ARIA role",
                    description=(
                        f"A <{elem.name}> with onclick handler lacks role and tabindex "
                        "(WCAG 4.1.2)."
                    ),
                    url=url,
                    recommendation=(
                        f'Add role="button" and tabindex="0" to clickable <{elem.name}> elements.'
                    ),
                    selector=f"{elem.name}[onclick]",
                    check_id="a11y-clickable-no-role",
                    wcag_criterion="4.1.2",
                    remediation_effort="medium",
                )
            )
            break

    return findings


def check_landmarks(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    landmarks = soup.find_all(["main", "nav", "header", "footer", "aside"])
    aria_landmarks = soup.find_all(
        attrs={"role": re.compile(r"^(main|navigation|banner|contentinfo|complementary)$")}
    )

    if not landmarks and not aria_landmarks:
        findings.append(
            _a11y_finding(
                severity="medium",
                title="No landmark regions found",
                description=(
                    "No semantic landmarks (<main>, <nav>, etc.) or ARIA landmarks "
                    "found (WCAG 1.3.1)."
                ),
                url=url,
                recommendation=("Use semantic HTML5 elements: <main>, <nav>, <header>, <footer>."),
                check_id="a11y-no-landmarks",
                wcag_criterion="1.3.1",
                remediation_effort="medium",
            )
        )

    if not soup.find("main") and not soup.find(attrs={"role": "main"}):
        findings.append(
            _a11y_finding(
                severity="medium",
                title="No main landmark",
                description="No <main> element or role='main' found (WCAG 1.3.1).",
                url=url,
                recommendation="Wrap the primary content in a <main> element.",
                selector="main",
                check_id="a11y-no-main",
                wcag_criterion="1.3.1",
                remediation_effort="medium",
            )
        )

    return findings


def check_button_names(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check button elements for accessible names (WCAG 4.1.2)."""
    findings: list[dict] = []
    buttons = soup.find_all("button")

    for button in buttons:
        if not isinstance(button, Tag):
            continue

        text_content = button.get_text(strip=True)
        aria_label = button.get("aria-label")
        aria_labelledby = button.get("aria-labelledby")
        title = button.get("title")

        if not text_content and not aria_label and not aria_labelledby and not title:
            findings.append(
                _a11y_finding(
                    severity="high",
                    title="Button without accessible name",
                    description=(
                        "A <button> element has no text content, aria-label, "
                        "aria-labelledby, or title attribute (WCAG 4.1.2)."
                    ),
                    url=url,
                    recommendation=(
                        "Add text content, aria-label, aria-labelledby, or "
                        "title to the button for screen reader accessibility."
                    ),
                    selector="button",
                    check_id="a11y-button-no-name",
                    wcag_criterion="4.1.2",
                )
            )

    return findings


def check_empty_th(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag <th> elements with no text content (WCAG 1.3.1)."""
    findings: list[dict] = []
    empty_ths: list[Tag] = []

    for th in soup.find_all("th"):
        if not isinstance(th, Tag):
            continue
        if not th.get_text(strip=True):
            empty_ths.append(th)

    if empty_ths:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{len(empty_ths)} empty table header cell(s)",
                description=(
                    f"{len(empty_ths)} <th> element(s) contain no text. "
                    "Empty header cells provide no information to screen "
                    "readers, making table data harder to understand "
                    "(WCAG 1.3.1)."
                ),
                url=url,
                recommendation=(
                    "Add descriptive text to all <th> elements, or "
                    "convert empty headers to <td> if they are not "
                    "actual headers."
                ),
                selector="th:empty",
                check_id="a11y-empty-th",
                wcag_criterion="1.3.1",
            )
        )
    return findings


def check_iframe_title(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag <iframe> elements without a title attribute (WCAG 2.4.1)."""
    findings: list[dict] = []
    iframes_no_title: list[Tag] = []

    for iframe in soup.find_all("iframe"):
        if not isinstance(iframe, Tag):
            continue
        if not iframe.get("title"):
            iframes_no_title.append(iframe)

    if iframes_no_title:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{len(iframes_no_title)} iframe(s) without title attribute",
                description=(
                    f"{len(iframes_no_title)} <iframe> element(s) lack a "
                    "title attribute. Screen reader users need the title "
                    "to understand the purpose of each iframe "
                    "(WCAG 2.4.1)."
                ),
                url=url,
                recommendation=(
                    "Add a descriptive title attribute to each <iframe> "
                    "that explains its content or purpose."
                ),
                selector="iframe:not([title])",
                check_id="a11y-iframe-no-title",
                wcag_criterion="2.4.1",
            )
        )
    return findings


def check_aria_hidden_focusable(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag aria-hidden='true' elements containing focusable children (WCAG 4.1.2)."""
    findings: list[dict] = []
    violations = sum(
        1
        for elem in soup.find_all(attrs={"aria-hidden": "true"})
        if isinstance(elem, Tag) and _has_focusable_child(elem)
    )

    if violations:
        findings.append(
            _a11y_finding(
                severity="high",
                title=(f"{violations} aria-hidden region(s) with focusable children"),
                description=(
                    f"{violations} element(s) with aria-hidden='true' "
                    "contain focusable children (links, buttons, inputs, "
                    "or elements with tabindex). This traps focus inside "
                    "hidden content, confusing keyboard and screen reader "
                    "users (WCAG 4.1.2)."
                ),
                url=url,
                recommendation=(
                    "Remove focusable elements from aria-hidden regions, "
                    "or remove the aria-hidden attribute. Use "
                    "tabindex='-1' on children that must remain hidden."
                ),
                selector='[aria-hidden="true"]',
                check_id="a11y-aria-hidden-focusable",
                wcag_criterion="4.1.2",
                remediation_effort="medium",
            )
        )
    return findings


def check_aria_live(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check if the page has any aria-live regions (WCAG 4.1.3)."""
    findings: list[dict] = []
    live_regions = soup.find_all(attrs={"aria-live": True})
    # Also check role="alert", role="status", role="log" which are
    # implicit live regions.
    implicit_live_roles = soup.find_all(
        attrs={"role": re.compile(r"^(alert|status|log|timer|marquee)$")}
    )

    if not live_regions and not implicit_live_roles:
        findings.append(
            _a11y_finding(
                severity="low",
                title="No aria-live regions found",
                description=(
                    "The page contains no aria-live regions or elements "
                    "with implicit live roles (alert, status, log). "
                    "Live regions announce dynamic content changes to "
                    "screen reader users (WCAG 4.1.3)."
                ),
                url=url,
                recommendation=(
                    "Add aria-live='polite' or aria-live='assertive' "
                    "regions for dynamic content that updates without "
                    "page reload (e.g. notifications, form validation "
                    "messages, chat)."
                ),
                check_id="a11y-no-aria-live",
                wcag_criterion="4.1.3",
                false_positive_risk="high",
            )
        )
    return findings
