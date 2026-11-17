"""CLS-related performance checks (image dimensions, viewport, content-visibility)."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.perf.base import _perf_finding


def check_viewport(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if not viewport:
        findings.append(
            _perf_finding(
                severity="high",
                title="Missing viewport meta tag",
                description=(
                    "No viewport meta tag found. The page won't render properly on mobile."
                ),
                url=url,
                recommendation=(
                    'Add <meta name="viewport" content="width=device-width, initial-scale=1">.'
                ),
                selector='meta[name="viewport"]',
                check_id="perf-missing-viewport",
            )
        )
    return findings


def check_image_dimensions(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    images = soup.find_all("img")
    missing_dims = []

    for img in images:
        if not isinstance(img, Tag):
            continue
        has_width = img.get("width") is not None
        has_height = img.get("height") is not None
        if not has_width or not has_height:
            src = img.get("src", "")
            if src and not str(src).startswith("data:"):
                missing_dims.append(img)

    if missing_dims:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(missing_dims)} images without explicit dimensions",
                description=(
                    f"{len(missing_dims)} images lack width/height attributes, "
                    "causing layout shifts (CLS)."
                ),
                url=url,
                recommendation="Add explicit width and height attributes to all images.",
                selector="img:not([width]):not([height])",
                check_id="perf-img-no-dimensions",
            )
        )

    return findings


def iframe_dimension_findings(soup: BeautifulSoup, url: str) -> list[dict]:
    iframes_no_dims = [
        iframe
        for iframe in soup.find_all("iframe")
        if isinstance(iframe, Tag) and (iframe.get("width") is None or iframe.get("height") is None)
    ]
    if not iframes_no_dims:
        return []
    return [
        _perf_finding(
            severity="medium",
            title=f"{len(iframes_no_dims)} iframes without width/height dimensions",
            description=(
                f"{len(iframes_no_dims)} iframe elements lack explicit "
                "width/height attributes, which causes layout shifts (CLS)."
            ),
            url=url,
            recommendation=(
                "Add explicit width and height attributes to all "
                "iframe elements to prevent layout shifts."
            ),
            selector="iframe:not([width]):not([height])",
            check_id="perf-iframe-no-dimensions",
            remediation_effort="medium",
        )
    ]


def check_content_visibility(soup: BeautifulSoup, url: str) -> list[dict]:
    """Suggest content-visibility for large DOMs without it."""
    findings: list[dict] = []
    all_elements = soup.find_all(True)
    dom_count = len(all_elements)

    if dom_count <= 500:
        return findings

    # Check inline styles and <style> blocks for content-visibility
    has_content_visibility = False

    for elem in soup.find_all(attrs={"style": True}):
        style = str(elem.get("style", ""))
        if "content-visibility" in style.lower():
            has_content_visibility = True
            break

    if not has_content_visibility:
        for style_tag in soup.find_all("style"):
            css_text = (style_tag.string or "").lower()
            if "content-visibility" in css_text:
                has_content_visibility = True
                break

    if not has_content_visibility:
        findings.append(
            _perf_finding(
                severity="info",
                title=(f"Large DOM ({dom_count} nodes) without content-visibility"),
                description=(
                    f"The page has {dom_count} DOM nodes but does not "
                    "use the CSS content-visibility property. "
                    "content-visibility: auto allows the browser to "
                    "skip rendering off-screen content, significantly "
                    "improving initial render time on large pages."
                ),
                url=url,
                recommendation=(
                    "Apply content-visibility: auto with "
                    "contain-intrinsic-size to off-screen sections to "
                    "reduce rendering work."
                ),
                check_id="perf-no-content-visibility",
                false_positive_risk="medium",
            )
        )
    return findings
