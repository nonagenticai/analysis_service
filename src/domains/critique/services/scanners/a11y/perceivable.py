"""Perceivable WCAG checks (alt text, video captions, SVG, motion, table headers)."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.a11y.base import _a11y_finding


def check_alt_text(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    images = soup.find_all("img")

    missing_alt = []
    empty_alt = []
    for img in images:
        alt = img.get("alt")
        if alt is None:
            missing_alt.append(img)
        elif isinstance(alt, str) and alt.strip() == "":
            role = img.get("role", "")
            if role != "presentation":
                src = str(img.get("src", ""))
                if src and not src.startswith("data:"):
                    empty_alt.append(img)

    if missing_alt:
        findings.append(
            _a11y_finding(
                severity="high",
                title=f"{len(missing_alt)} images missing alt attribute",
                description=(
                    f"{len(missing_alt)} images have no alt attribute at all (WCAG 1.1.1)."
                ),
                url=url,
                recommendation=(
                    "Add descriptive alt text to all images, or alt='' for decorative images."
                ),
                selector="img:not([alt])",
                check_id="a11y-img-missing-alt",
                wcag_criterion="1.1.1",
            )
        )

    if empty_alt:
        findings.append(
            _a11y_finding(
                severity="low",
                title=f"{len(empty_alt)} images with empty alt text",
                description=f"{len(empty_alt)} non-decorative images have empty alt text.",
                url=url,
                recommendation=(
                    "Add descriptive alt text or mark as decorative with role='presentation'."
                ),
                selector='img[alt=""]',
                check_id="a11y-img-empty-alt",
                wcag_criterion="1.1.1",
            )
        )

    return findings


def check_video_captions(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check video elements for captions (WCAG 1.2.2)."""
    findings: list[dict] = []
    videos = soup.find_all("video")

    for video in videos:
        if not isinstance(video, Tag):
            continue
        tracks = video.find_all("track")
        caption_tracks = [
            t for t in tracks if isinstance(t, Tag) and t.get("kind") in ("captions", "subtitles")
        ]

        if not caption_tracks:
            src = video.get("src", "")
            findings.append(
                _a11y_finding(
                    severity="high",
                    title="Video without captions",
                    description=(
                        'A <video> element has no <track kind="captions"> or '
                        '<track kind="subtitles"> child (WCAG 1.2.2).'
                    ),
                    url=url,
                    recommendation=(
                        'Add a <track kind="captions" src="captions.vtt" '
                        'srclang="en"> element to the video.'
                    ),
                    selector="video",
                    evidence=str(src)[:100] if src else None,
                    check_id="a11y-video-no-captions",
                    wcag_criterion="1.2.2",
                    remediation_effort="high",
                )
            )
        else:
            for track in caption_tracks:
                if not track.get("srclang"):
                    findings.append(
                        _a11y_finding(
                            severity="medium",
                            title="Video caption track missing srclang",
                            description=(
                                "A caption/subtitle track is missing the srclang "
                                "attribute (WCAG 1.2.2)."
                            ),
                            url=url,
                            recommendation=(
                                'Add srclang="en" (or appropriate language code) '
                                "to the <track> element."
                            ),
                            selector="track",
                            check_id="a11y-video-caption-no-lang",
                            wcag_criterion="1.2.2",
                            remediation_effort="high",
                        )
                    )

    return findings


def check_table_headers(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check data tables for proper headers (WCAG 1.3.1)."""
    findings: list[dict] = []
    tables = soup.find_all("table")

    for table in tables:
        if not isinstance(table, Tag):
            continue

        # Skip presentation tables
        if table.get("role") == "presentation":
            continue

        rows = table.find_all("tr")
        if len(rows) <= 1:
            continue

        # This appears to be a data table
        th_elements = table.find_all("th")

        if not th_elements:
            findings.append(
                _a11y_finding(
                    severity="high",
                    title="Data table without header cells",
                    description=(
                        "A data table has no <th> elements to identify column or "
                        "row headers (WCAG 1.3.1)."
                    ),
                    url=url,
                    recommendation=("Add <th> elements to identify column and/or row headers."),
                    selector="table",
                    check_id="a11y-table-no-headers",
                    wcag_criterion="1.3.1",
                )
            )
        else:
            # Check for scope attribute on th elements
            th_without_scope = [th for th in th_elements if not th.get("scope")]
            if th_without_scope:
                findings.append(
                    _a11y_finding(
                        severity="medium",
                        title="Table header cells missing scope attribute",
                        description=(
                            f"{len(th_without_scope)} <th> elements lack a scope "
                            "attribute (WCAG 1.3.1)."
                        ),
                        url=url,
                        recommendation=(
                            'Add scope="col" or scope="row" to <th> elements '
                            "to clarify header relationships."
                        ),
                        selector="th:not([scope])",
                        check_id="a11y-table-header-no-scope",
                        wcag_criterion="1.3.1",
                    )
                )

        # Check for caption or aria-label
        has_caption = bool(table.find("caption"))
        has_aria_label = bool(table.get("aria-label") or table.get("aria-labelledby"))
        if not has_caption and not has_aria_label:
            findings.append(
                _a11y_finding(
                    severity="low",
                    title="Data table without caption",
                    description=(
                        "A data table has no <caption> element or aria-label "
                        "to describe its purpose (WCAG 1.3.1)."
                    ),
                    url=url,
                    recommendation=(
                        "Add a <caption> element or aria-label to describe the table's content."
                    ),
                    selector="table",
                    check_id="a11y-table-no-caption",
                    wcag_criterion="1.3.1",
                )
            )

    return findings


def check_deprecated_motion(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag deprecated <marquee> and <blink> elements (WCAG 2.3.1)."""
    findings: list[dict] = []
    marquees = soup.find_all("marquee")
    blinks = soup.find_all("blink")
    deprecated = len(marquees) + len(blinks)

    if deprecated:
        elements = []
        if marquees:
            elements.append(f"{len(marquees)} <marquee>")
        if blinks:
            elements.append(f"{len(blinks)} <blink>")
        findings.append(
            _a11y_finding(
                severity="high",
                title="Deprecated motion element(s) found",
                description=(
                    f"Found {', '.join(elements)} element(s). These "
                    "deprecated elements create uncontrollable motion "
                    "that can cause seizures or vestibular disorders "
                    "(WCAG 2.3.1)."
                ),
                url=url,
                recommendation=(
                    "Remove <marquee> and <blink> elements entirely. "
                    "If animation is needed, use CSS with "
                    "prefers-reduced-motion support."
                ),
                selector="marquee, blink",
                check_id="a11y-deprecated-motion",
                wcag_criterion="2.3.1",
            )
        )
    return findings


def check_svg_accessibility(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag <svg> elements without accessible names (WCAG 1.1.1)."""
    findings: list[dict] = []
    inaccessible_svgs = 0

    for svg in soup.find_all("svg"):
        if not isinstance(svg, Tag):
            continue
        has_title_child = bool(svg.find("title"))
        has_aria_label = bool(svg.get("aria-label"))
        has_aria_labelledby = bool(svg.get("aria-labelledby"))
        has_role_presentation = svg.get("role") in (
            "presentation",
            "none",
        )

        if has_role_presentation:
            continue  # Decorative SVG, no label needed
        if not has_title_child and not has_aria_label and not has_aria_labelledby:
            inaccessible_svgs += 1

    if inaccessible_svgs:
        findings.append(
            _a11y_finding(
                severity="medium",
                title=f"{inaccessible_svgs} SVG(s) without accessible name",
                description=(
                    f"{inaccessible_svgs} <svg> element(s) lack a <title> "
                    "child, aria-label, or aria-labelledby attribute. "
                    "Screen readers cannot describe these images to "
                    "users (WCAG 1.1.1)."
                ),
                url=url,
                recommendation=(
                    "Add a <title> child element inside each meaningful "
                    "<svg>, or use aria-label / aria-labelledby. For "
                    "decorative SVGs, add role='presentation'."
                ),
                selector="svg",
                check_id="a11y-svg-no-title",
                wcag_criterion="1.1.1",
                false_positive_risk="medium",
            )
        )
    return findings
