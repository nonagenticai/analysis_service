"""Unit tests for consolidated constants."""

import pytest

from src.domains.review.services.constants import (
    DEPENDENT_REVIEW_TYPES,
    GRADE_THRESHOLDS,
    KEY_ALIASES,
    NA_VALUES,
    PENALTY_FLOOR,
    PENALTY_K,
    REMEDIATION_EFFORT_MAP,
    SEVERITY_ORDER,
    SEVERITY_PENALTIES,
    SEVERITY_SCORE_THRESHOLDS,
)


@pytest.mark.unit
class TestConstants:
    def test_dependent_review_types_is_set(self) -> None:
        assert isinstance(DEPENDENT_REVIEW_TYPES, set)
        assert "feature_suggestions" in DEPENDENT_REVIEW_TYPES

    def test_na_values_is_set(self) -> None:
        assert isinstance(NA_VALUES, set)
        assert "n/a" in NA_VALUES

    def test_key_aliases_maps_to_canonical(self) -> None:
        assert KEY_ALIASES["suggestions"] == "proposals"
        assert KEY_ALIASES["total"] == "total_proposals"

    def test_grade_thresholds_descending(self) -> None:
        scores = [t[0] for t in GRADE_THRESHOLDS]
        assert scores == sorted(scores, reverse=True)

    def test_severity_penalties_all_non_positive(self) -> None:
        for sev, val in SEVERITY_PENALTIES.items():
            assert val <= 0, f"{sev} has positive penalty {val}"

    def test_penalty_constants(self) -> None:
        assert PENALTY_K > 0
        assert PENALTY_FLOOR >= 0

    def test_severity_order_covers_all_levels(self) -> None:
        expected = {"critical", "high", "medium", "low", "info"}
        assert set(SEVERITY_ORDER.keys()) == expected

    def test_remediation_effort_map_covers_all_severities(self) -> None:
        expected = {"critical", "high", "medium", "low", "info"}
        assert set(REMEDIATION_EFFORT_MAP.keys()) == expected

    def test_severity_score_thresholds_ascending(self) -> None:
        scores = [t[0] for t in SEVERITY_SCORE_THRESHOLDS]
        assert scores == sorted(scores)
