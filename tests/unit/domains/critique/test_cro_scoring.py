"""Unit tests for compute_cro_score + the shared score_to_grade helper."""

from __future__ import annotations

from src.domains.critique.services.report_generator import (
    GRADE_THRESHOLDS,
    compute_category_score,
    score_to_grade,
)
from src.domains.critique.services.scanners.cro import SECTION_WEIGHTS, compute_cro_score


def _finding(check_id: str, severity: str) -> dict:
    return {"category": "cro", "severity": severity, "check_id": check_id}


class TestSectionWeights:
    def test_weights_sum_to_one(self):
        assert round(sum(SECTION_WEIGHTS.values()), 6) == 1.0


class TestComputeCroScore:
    def test_no_findings_is_perfect(self):
        result = compute_cro_score([])
        assert result["overall_score"] == 100.0
        assert result["grade"] == "A+"
        assert all(v == 100.0 for v in result["subsection_scores"].values())

    def test_only_cro_findings_contribute(self):
        # A non-cro finding must be ignored entirely.
        findings = [{"category": "seo", "severity": "critical", "check_id": "seo-x"}]
        assert compute_cro_score(findings)["overall_score"] == 100.0

    def test_section_score_matches_category_decay(self):
        # One high finding in the form section: that section's score must equal
        # the production decay for the same single finding.
        findings = [_finding("cro-form-field-count", "high")]
        result = compute_cro_score(findings)
        expected = compute_category_score(findings, "cro")
        assert result["subsection_scores"]["form"] == expected
        # Untouched sections stay perfect.
        assert result["subsection_scores"]["hero"] == 100.0

    def test_overall_is_weighted_mean_of_sections(self):
        findings = [_finding("cro-form-field-count", "critical")]
        result = compute_cro_score(findings)
        sub = result["subsection_scores"]
        expected = round(
            sum(sub[s] * w for s, w in SECTION_WEIGHTS.items()) / sum(SECTION_WEIGHTS.values()), 1
        )
        assert result["overall_score"] == expected

    def test_worse_findings_lower_the_score(self):
        light = compute_cro_score([_finding("cro-hero-headline-length", "low")])["overall_score"]
        heavy = compute_cro_score([_finding("cro-hero-no-h1", "high")])["overall_score"]
        assert heavy < light < 100.0


class TestScoreToGrade:
    def test_thresholds(self):
        assert score_to_grade(100) == "A+"
        assert score_to_grade(95) == "A+"
        assert score_to_grade(94.9) == "A"
        assert score_to_grade(90) == "A"
        assert score_to_grade(85) == "B"
        assert score_to_grade(75) == "C"
        assert score_to_grade(65) == "D"
        assert score_to_grade(59) == "F"
        assert score_to_grade(0) == "F"

    def test_thresholds_are_ordered_high_to_low(self):
        scores = [t for t, _ in GRADE_THRESHOLDS]
        assert scores == sorted(scores, reverse=True)
