"""Shared helpers for the accessibility scanner package."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.base import make_finding


def _extract_inline_style_value(style: str, prop: str) -> str | None:
    """Extract a CSS property value from an inline style string.

    For 'color', we need to match 'color:' but NOT 'background-color:'.
    """
    if prop == "color":
        # Match 'color:' that is NOT preceded by '-'
        pattern = re.compile(r"(?<![a-zA-Z-])color\s*:\s*([^;]+)", re.IGNORECASE)
    else:
        pattern = re.compile(re.escape(prop) + r"\s*:\s*([^;]+)", re.IGNORECASE)
    match = pattern.search(style)
    if match:
        return match.group(1).strip()
    return None


def _a11y_finding(
    *,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    wcag_criterion: str | None = None,
    evidence: str | None = None,
    false_positive_risk: str = "low",
    remediation_effort: str = "low",
) -> dict:
    """Accessibility scanner finding with default metadata."""
    return make_finding(
        category="accessibility",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        wcag_criterion=wcag_criterion,
        evidence=evidence,
        source_tool="a11y_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )


def _collect_clickable_elements(soup: BeautifulSoup) -> list[Tag]:
    """Resolve the list of clickable elements (a, button, submit/button inputs)."""
    out: list[Tag] = []
    for sel in ("a", "button", "input[type=submit]", "input[type=button]"):
        if "[" in sel:
            tag_name, attr_part = sel.split("[", 1)
            attr_name, attr_val = attr_part.rstrip("]").split("=", 1)
            out.extend(soup.find_all(tag_name, attrs={attr_name: attr_val}))
        else:
            out.extend(soup.find_all(sel))
    return out


def _has_small_inline_dimension(style: str) -> bool:
    for prop in ("width", "height"):
        dim_str = _extract_inline_style_value(style, prop)
        if not dim_str:
            continue
        px_match = re.match(r"(\d+(?:\.\d+)?)\s*px", dim_str)
        if px_match and float(px_match.group(1)) < 44:
            return True
    return False


def _has_focusable_child(elem: Tag) -> bool:
    focusable_tags = {"a", "button", "input", "select", "textarea"}
    for child in elem.descendants:
        if not isinstance(child, Tag):
            continue
        if child.name in focusable_tags:
            return True
        if child.get("tabindex") is not None:
            try:
                if int(str(child.get("tabindex", "-1"))) >= 0:
                    return True
            except ValueError, TypeError:
                pass
    return False
