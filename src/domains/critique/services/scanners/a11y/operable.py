"""Operable WCAG checks (skip links, tabindex, keyboard, touch targets, focus trap)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.a11y.base import (
    _a11y_finding,
    _collect_clickable_elements,
    _extract_inline_style_value,
    _has_small_inline_dimension,
)


def check_skip_links(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    skip_patterns = re.compile(r"skip|jump|main.content", re.IGNORECASE)
    first_links = soup.find_all("a", limit=5)
    has_skip = any(
        skip_patterns.search(str(link.get("href", ""))) or skip_patterns.search(link.get_text())
        for link in first_links
    )
    if not has_skip:
        findings.append(
            _a11y_finding(
                severity="medium",
                title="No skip navigation link",
                description="No skip-to-content link found (WCAG 2.4.1).",
                url=url,
                recommendation="Add a skip link as the first focusable element on the page.",
                check_id="a11y-no-skip-link",
                wcag_criterion="2.4.1",
            )
        )
    return findings


def check_tabindex(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    positive_tabindex = soup.find_all(attrs={"tabindex": True})
    bad_tabs = []
    for elem in positive_tabindex:
        if not isinstance(elem, Tag):
            continue
        try:
            val = int(str(elem.get("tabindex", "0")))
            if val > 0:
                bad_tabs.append(elem)
        except ValueError, TypeError:
            pass

    if bad_tabs:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{len(bad_tabs)} elements with positive tabindex",
                description=("Positive tabindex values create confusing tab order (WCAG 2.4.3)."),
                url=url,
                recommendation=(
                    'Use tabindex="0" for focusable elements or '
                    'tabindex="-1" for programmatic focus.'
                ),
                selector="[tabindex]",
                check_id="a11y-positive-tabindex",
                wcag_criterion="2.4.3",
            )
        )

    return findings


def check_touch_targets(soup: BeautifulSoup, url: str) -> list[dict]:
    """Heuristic check for small touch targets (WCAG 2.5.8)."""
    findings: list[dict] = []
    for elem in _collect_clickable_elements(soup):
        if not isinstance(elem, Tag):
            continue
        style = str(elem.get("style", ""))
        if not style or not _has_small_inline_dimension(style):
            continue
        findings.append(
            _a11y_finding(
                severity="medium",
                title="Small touch target",
                description=(
                    f"A clickable <{elem.name}> element has inline dimensions "
                    "smaller than 44px, which may be difficult for touch users "
                    "(WCAG 2.5.8). Note: this is a heuristic check based on "
                    "inline styles only."
                ),
                url=url,
                recommendation="Ensure touch targets are at least 44x44 CSS pixels.",
                selector=elem.name,
                check_id="a11y-small-touch-target",
                wcag_criterion="2.5.8",
                remediation_effort="medium",
            )
        )
    return findings


def _outline_removed_inline(soup: BeautifulSoup, url: str) -> list[dict]:
    out: list[dict] = []
    for elem in soup.find_all(attrs={"style": True}):
        if not isinstance(elem, Tag):
            continue
        style = str(elem.get("style", ""))
        outline_val = _extract_inline_style_value(style, "outline")
        if outline_val and re.match(r"^(none|0(px)?)$", outline_val.strip()):
            out.append(
                _a11y_finding(
                    severity="high",
                    title="Focus outline removed via inline style",
                    description=(
                        f"A <{elem.name}> element has outline:none or outline:0 "
                        "in its inline style, which removes the keyboard focus "
                        "indicator (WCAG 2.4.7)."
                    ),
                    url=url,
                    recommendation=(
                        "Do not remove focus outlines. Provide a visible focus "
                        "indicator, either the default or a custom one."
                    ),
                    selector=f"{elem.name}[style]",
                    check_id="a11y-outline-removed",
                    wcag_criterion="2.4.7",
                    remediation_effort="medium",
                )
            )
    return out


def _outline_removed_in_styles(soup: BeautifulSoup, url: str) -> list[dict]:
    focus_outline_pattern = re.compile(
        r":focus[^{]*\{[^}]*outline\s*:\s*(none|0(px)?)\s*;?[^}]*\}",
        re.IGNORECASE | re.DOTALL,
    )
    out: list[dict] = []
    for style_tag in soup.find_all("style"):
        if focus_outline_pattern.search(style_tag.get_text()):
            out.append(
                _a11y_finding(
                    severity="high",
                    title="Focus outline removed in embedded CSS",
                    description=(
                        "A :focus rule in a <style> block sets outline to none or 0, "
                        "removing the keyboard focus indicator (WCAG 2.4.7)."
                    ),
                    url=url,
                    recommendation=(
                        "Do not remove focus outlines. Provide a visible focus "
                        "indicator, either the default or a custom one."
                    ),
                    check_id="a11y-outline-removed",
                    wcag_criterion="2.4.7",
                    remediation_effort="medium",
                )
            )
    return out


def _missing_keyboard_handlers(soup: BeautifulSoup, url: str) -> list[dict]:
    out: list[dict] = []
    for elem in soup.find_all(["a", "button", "input"]):
        if not isinstance(elem, Tag):
            continue
        if not (elem.get("onclick") and not elem.get("tabindex")):
            continue
        has_keyboard = bool(elem.get("onkeydown") or elem.get("onkeyup") or elem.get("onkeypress"))
        if has_keyboard:
            continue
        # Only flag <a> without href: native interactives don't need extra handlers.
        if elem.name == "a" and not elem.get("href"):
            out.append(
                _a11y_finding(
                    severity="medium",
                    title="Interactive element lacks keyboard handler",
                    description=(
                        f"A <{elem.name}> element has an onclick handler but "
                        "no keyboard event handler and no href (WCAG 2.1.1)."
                    ),
                    url=url,
                    recommendation=(
                        "Add onkeydown/onkeypress handlers or use a native interactive element."
                    ),
                    selector=f"{elem.name}[onclick]",
                    check_id="a11y-no-keyboard-handler",
                    wcag_criterion="2.1.1",
                    remediation_effort="medium",
                )
            )
    return out


def check_keyboard_navigation(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for keyboard navigation issues (WCAG 2.4.7, 2.1.1)."""
    findings: list[dict] = []
    findings.extend(_outline_removed_inline(soup, url))
    findings.extend(_outline_removed_in_styles(soup, url))
    findings.extend(_missing_keyboard_handlers(soup, url))
    return findings


def check_focus_trap(soup: BeautifulSoup, url: str) -> list[dict]:
    """Heuristic check for potential focus traps (WCAG 2.1.2)."""
    findings: list[dict] = []

    # Check for modal dialogs without proper escape mechanisms
    dialogs = soup.find_all(attrs={"role": "dialog"})
    for dialog in dialogs:
        if not isinstance(dialog, Tag):
            continue
        close_pattern = re.compile(r"close|dismiss|cancel|x", re.IGNORECASE)
        close_button = next(
            (
                btn
                for btn in dialog.find_all("button")
                if isinstance(btn, Tag) and btn.string and close_pattern.search(str(btn.string))
            ),
            None,
        )
        has_close = bool(
            close_button
            or dialog.find(attrs={"aria-label": re.compile(r"close|dismiss", re.IGNORECASE)})
        )
        has_aria_modal = dialog.get("aria-modal") == "true"
        if not has_close and not has_aria_modal:
            findings.append(
                _a11y_finding(
                    severity="high",
                    title="Potential focus trap in dialog",
                    description=(
                        "A dialog element (role='dialog') lacks both a "
                        "visible close button and aria-modal='true'. "
                        "Users may become trapped in this dialog without "
                        "a way to escape (WCAG 2.1.2)."
                    ),
                    url=url,
                    recommendation=(
                        "Add a visible close button and/or aria-modal='true' "
                        "to dialog elements. Ensure Escape key closes the dialog."
                    ),
                    selector='[role="dialog"]',
                    check_id="a11y-potential-focus-trap",
                    wcag_criterion="2.1.2",
                    false_positive_risk="high",
                    remediation_effort="medium",
                )
            )

    # Check for tabindex="-1" combined with keyboard handlers (trap pattern)
    for elem in soup.find_all(attrs={"tabindex": "-1"}):
        if not isinstance(elem, Tag):
            continue
        has_key_handler = bool(elem.get("onkeydown") or elem.get("onkeypress"))
        if has_key_handler:
            findings.append(
                _a11y_finding(
                    severity="medium",
                    title="Potential focus trap pattern detected",
                    description=(
                        f"A <{elem.name}> element has tabindex='-1' "
                        "combined with a keyboard event handler, which "
                        "may indicate a focus trap (WCAG 2.1.2). "
                        "This is a heuristic check."
                    ),
                    url=url,
                    recommendation=(
                        "Ensure users can navigate away from this element "
                        "using standard keyboard navigation (Tab, Escape)."
                    ),
                    selector=f'{elem.name}[tabindex="-1"]',
                    check_id="a11y-potential-focus-trap",
                    wcag_criterion="2.1.2",
                    false_positive_risk="high",
                    remediation_effort="medium",
                )
            )
            break  # Only report once

    return findings
