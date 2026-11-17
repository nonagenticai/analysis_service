"""Consolidated constants for the review service.

All magic values from across the service in one place -- thresholds,
grade maps, severity mappings, key aliases, and review type metadata.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Review service orchestration
# ---------------------------------------------------------------------------

DEPENDENT_REVIEW_TYPES: set[str] = {"feature_suggestions"}

NA_VALUES: set[str] = {"n/a", "na", "none", "null", ""}

KEY_ALIASES: dict[str, str] = {
    "suggestions": "proposals",
    "features": "proposals",
    "count": "total_proposals",
    "total": "total_proposals",
}

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (9.0, "A+"),
    (8.5, "A"),
    (8.0, "A-"),
    (7.5, "B+"),
    (7.0, "B"),
    (6.5, "B-"),
    (6.0, "C+"),
    (5.5, "C"),
    (5.0, "C-"),
    (4.5, "D+"),
    (4.0, "D"),
    (3.5, "D-"),
    (0.0, "F"),
]

SEVERITY_PENALTIES: dict[str, float] = {
    "critical": -20.0,
    "high": -10.0,
    "medium": -4.0,
    "low": -1.0,
    "info": 0.0,
}

PENALTY_K: float = 35.0
PENALTY_FLOOR: float = 50.0

# Minimum share of the configured aggregate weight that must actually have been
# measured before an aggregate score is reported at all. Excluding failed review
# types and renormalising over the survivors is fine while most of the weight is
# still there; below half, the number no longer describes the site and is
# refused (``None``) rather than published as a complete-looking grade.
MIN_AGGREGATE_COVERAGE: float = 0.5

# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}

REMEDIATION_EFFORT_MAP: dict[str, str] = {
    "critical": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "info": "low",
}

SEVERITY_SCORE_THRESHOLDS: list[tuple[float, str]] = [
    (4.0, "critical"),
    (6.0, "high"),
    (7.0, "medium"),
    (8.0, "low"),
]
