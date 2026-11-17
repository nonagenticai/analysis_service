"""Tests for penalty-based scoring and normalization in src.services.scoring."""

import math

import pytest

from src.domains.review.services.scoring import (
    compute_penalty_from_findings,
    normalize_to_100,
    penalty_score,
)


class TestNormalizeTo100:
    def test_zero(self):
        assert normalize_to_100(0.0) == 0.0

    def test_ten(self):
        assert normalize_to_100(10.0) == 100.0

    def test_mid(self):
        assert normalize_to_100(5.0) == 50.0

    def test_fractional(self):
        assert normalize_to_100(7.5) == 75.0

    def test_round_trip(self):
        """normalize_to_100 is simply *10, so score/10 returns the original."""
        original = 6.3
        normalized = normalize_to_100(original)
        assert normalized == 63.0


class TestPenaltyScore:
    def test_no_penalties_returns_100(self):
        assert penalty_score([]) == 100.0

    def test_single_low_penalty(self):
        result = penalty_score([{"severity": "low", "count": 1}])
        expected = round(100.0 * math.exp(-1.0 / 35.0), 1)
        assert result == expected

    def test_single_critical_penalty(self):
        result = penalty_score([{"severity": "critical", "count": 1}])
        expected = round(100.0 * math.exp(-20.0 / 35.0), 1)
        assert result == expected

    def test_many_critical_hits_floor(self):
        """Many critical findings should floor at 50.0."""
        result = penalty_score([{"severity": "critical", "count": 10}])
        assert result == 50.0

    def test_mixed_penalties(self):
        penalties = [
            {"severity": "critical", "count": 1},
            {"severity": "high", "count": 2},
            {"severity": "medium", "count": 3},
        ]
        # total: 20 + 20 + 12 = 52
        expected_raw = 100.0 * math.exp(-52.0 / 35.0)
        expected = round(max(expected_raw, 50.0), 1)
        assert penalty_score(penalties) == expected

    def test_info_penalties_no_effect(self):
        result = penalty_score([{"severity": "info", "count": 100}])
        assert result == 100.0

    def test_floor_is_50(self):
        """Even extreme penalties should not go below 50."""
        result = penalty_score([{"severity": "critical", "count": 100}])
        assert result == 50.0


class TestComputePenaltyFromFindings:
    def test_empty_findings(self):
        assert compute_penalty_from_findings([]) == 100.0

    def test_findings_with_severities(self):
        findings = [
            {"severity": "critical"},
            {"severity": "high"},
            {"severity": "low"},
        ]
        result = compute_penalty_from_findings(findings)
        # critical=1, high=1, low=1 => total_penalty = 20+10+1 = 31
        # raw = 100 * exp(-31/35) = ~41.2, but floor is 50.0
        assert result == 50.0

    def test_missing_severity_treated_as_info(self):
        findings = [{"description": "no severity key"}]
        result = compute_penalty_from_findings(findings)
        assert result == 100.0  # info has 0 penalty

    @pytest.mark.parametrize(
        "severity,expected_penalty",
        [
            ("critical", 20.0),
            ("high", 10.0),
            ("medium", 4.0),
            ("low", 1.0),
            ("info", 0.0),
        ],
    )
    def test_single_finding_each_severity(self, severity, expected_penalty):
        findings = [{"severity": severity}]
        result = compute_penalty_from_findings(findings)
        if expected_penalty == 0.0:
            assert result == 100.0
        else:
            expected = round(100.0 * math.exp(-expected_penalty / 35.0), 1)
            assert result == expected
