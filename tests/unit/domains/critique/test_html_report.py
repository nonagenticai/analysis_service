"""Unit tests for HTML report generator."""

import uuid

from src.domains.critique.services.html_report_generator import (
    _generate_radar_svg,
    generate_html_report,
)


def _make_finding(
    category: str = "seo",
    severity: str = "medium",
    title: str = "Test finding",
) -> dict:
    return {
        "finding_id": str(uuid.uuid7()),
        "category": category,
        "severity": severity,
        "title": title,
        "description": "Test description",
        "url": "https://example.com",
        "recommendation": "Fix it",
    }


class TestHTMLReport:
    def test_contains_url(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 85.0},
            summary="Test summary",
        )
        assert "https://example.com" in report

    def test_contains_scores(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 75.0, "security": 90.0},
            summary="Mixed results",
        )
        assert "75.0" in report
        assert "90.0" in report

    def test_contains_findings(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[_make_finding(title="Missing title tag")],
            scores={"seo": 85.0},
            summary="One issue found",
        )
        assert "Missing title tag" in report

    def test_contains_svg_chart(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[
                _make_finding(category="seo"),
                _make_finding(category="security"),
                _make_finding(category="accessibility"),
            ],
            scores={
                "seo": 85.0,
                "security": 90.0,
                "accessibility": 70.0,
            },
            summary="Test",
        )
        assert "<svg" in report

    def test_severity_badges(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[
                _make_finding(severity="critical"),
                _make_finding(severity="high"),
                _make_finding(severity="low"),
            ],
            scores={"seo": 60.0},
            summary="Issues found",
        )
        assert "critical" in report.lower()
        assert "high" in report.lower()

    def test_overall_score_displayed(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[],
            scores={"seo": 90.0},
            summary="Good",
            overall_score=90.0,
        )
        assert "90.0" in report

    def test_html_is_valid_structure(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[],
            scores={},
            summary="Empty",
        )
        assert report.startswith("<!DOCTYPE html>")
        assert "</html>" in report

    def test_radar_svg_generation(self) -> None:
        svg = _generate_radar_svg({"seo": 80, "security": 90, "performance": 70})
        assert "<svg" in svg
        assert "polygon" in svg

    def test_radar_svg_empty(self) -> None:
        svg = _generate_radar_svg({})
        assert svg == ""

    def test_collapsible_findings_html(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 85.0},
            summary="Test summary",
        )
        assert "<details>" in report
        assert "<summary>" in report

    def test_confidence_scores_rendered(self) -> None:
        report = generate_html_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 85.0},
            summary="Test summary",
            confidence_scores={"seo": 0.9},
        )
        assert "confidence-bar" in report
        assert "90%" in report


def _css_rule(html: str, selector: str) -> dict[str, str]:
    """Return the declarations of ``selector`` from the report's inline <style>."""
    import re

    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", html)
    assert match, f"no CSS rule for {selector}"
    decls: dict[str, str] = {}
    for part in match.group(1).split(";"):
        if ":" in part:
            key, value = part.split(":", 1)
            decls[key.strip()] = value.strip()
    return decls


class TestPdfSafeLayout:
    """The report is rendered to PDF by WeasyPrint, which mis-lays-out some CSS."""

    def _report(self) -> str:
        return generate_html_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 80.0, "security": 60.0, "performance": 70.0},
            summary="s",
            confidence_scores={"seo": 0.5},
        )

    def test_grade_badge_centres_letter_without_flex(self) -> None:
        # WeasyPrint does not centre flex content inside a floated box: the
        # grade letter rendered top-left of the circle.
        badge = _css_rule(self._report(), ".grade-badge")
        assert "flex" not in badge["display"]
        assert badge["float"] == "right"
        assert badge["text-align"] == "center"
        assert badge["line-height"] == badge["height"]
        assert badge["width"] == badge["height"]

    def test_score_grid_avoids_auto_fit_tracks(self) -> None:
        # repeat(auto-fit, ...) collapses to a single full-width column in WeasyPrint.
        grid = _css_rule(self._report(), ".score-grid")
        assert "auto-fit" not in grid["grid-template-columns"]
        assert "auto-fill" not in grid["grid-template-columns"]

    def test_confidence_fill_is_block_so_its_width_applies(self) -> None:
        fill = _css_rule(self._report(), ".confidence-fill")
        assert fill["display"] == "block"

    def test_radar_canvas_is_wider_than_chart_so_side_labels_fit(self) -> None:
        import re

        size = 300
        svg = _generate_radar_svg({"seo": 80, "security": 90, "performance": 70}, size=size)
        vb = re.search(r'viewBox="(-?\d+) 0 (\d+) (\d+)"', svg)
        assert vb
        min_x, width = int(vb.group(1)), int(vb.group(2))
        # "Performance" is anchored start at cx + radius + 20 = 280 with 11px text.
        assert min_x + width >= size + 50
        assert min_x <= -50
