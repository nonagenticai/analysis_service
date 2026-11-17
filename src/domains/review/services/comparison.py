"""Compare two review jobs and produce a structured delta report.

Given the JSONB results from two completed review jobs this module
computes per-review-type score deltas, overall delta, grade change,
newly-appeared strengths, resolved weaknesses, and persistent issue count.
"""

from __future__ import annotations

import logging

from src.domains.review.services.scoring import score_to_grade
from src.domains.review.services.text_utils import collect_text_set, extract_overall_score

logger = logging.getLogger(__name__)


def compare_reviews(
    job1_results: dict[str, dict],
    job2_results: dict[str, dict],
) -> dict:
    """Compare two sets of review results.

    Args:
        job1_results: Results dict from the *baseline* (older) job.
        job2_results: Results dict from the *current* (newer) job.

    Returns:
        Dict with keys:
            score_deltas        - per review type float delta (job2 - job1)
            overall_delta       - aggregate score difference
            grade_change        - e.g. "B+ -> A-" or None when unchanged
            new_strengths       - strengths in job2 not in job1 (max 10)
            resolved_weaknesses - weaknesses in job1 not in job2 (max 10)
            persistent_issues   - count of weaknesses present in both
    """
    score_deltas: dict[str, float | None] = {}
    job1_scores: dict[str, float] = {}
    job2_scores: dict[str, float] = {}

    all_review_types = set(job1_results.keys()) | set(job2_results.keys())

    for review_type in sorted(all_review_types):
        s1 = extract_overall_score(job1_results.get(review_type, {}))
        s2 = extract_overall_score(job2_results.get(review_type, {}))

        if s1 is not None:
            job1_scores[review_type] = s1
        if s2 is not None:
            job2_scores[review_type] = s2

        if s1 is not None and s2 is not None:
            score_deltas[review_type] = round(s2 - s1, 2)
        else:
            score_deltas[review_type] = None

    # Overall scores
    overall1 = _mean(list(job1_scores.values()))
    overall2 = _mean(list(job2_scores.values()))
    overall_delta = (
        round(overall2 - overall1, 2) if overall1 is not None and overall2 is not None else None
    )

    # Grade change
    grade1 = score_to_grade(overall1) if overall1 is not None else None
    grade2 = score_to_grade(overall2) if overall2 is not None else None
    grade_change: str | None = None
    if grade1 is not None and grade2 is not None and grade1 != grade2:
        grade_change = f"{grade1} -> {grade2}"

    # Strengths diff
    job1_strengths = collect_text_set(job1_results, "strengths")
    job2_strengths = collect_text_set(job2_results, "strengths")
    new_strengths = sorted(job2_strengths - job1_strengths)[:10]

    # Weaknesses diff
    job1_weaknesses = collect_text_set(
        job1_results,
        "weaknesses",
        "critical_issues",
        "issues",
    )
    job2_weaknesses = collect_text_set(
        job2_results,
        "weaknesses",
        "critical_issues",
        "issues",
    )
    resolved_weaknesses = sorted(job1_weaknesses - job2_weaknesses)[:10]
    persistent_issues = len(job1_weaknesses & job2_weaknesses)

    return {
        "score_deltas": score_deltas,
        "overall_delta": overall_delta,
        "grade_change": grade_change,
        "new_strengths": new_strengths,
        "resolved_weaknesses": resolved_weaknesses,
        "persistent_issues": persistent_issues,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    """Return the arithmetic mean or None for an empty list."""
    if not values:
        return None
    return round(sum(values) / len(values), 2)
