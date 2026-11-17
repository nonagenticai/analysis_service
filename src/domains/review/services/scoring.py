"""Weighted scoring system for review results.

Computes deterministic weighted scores from LLM-generated category scores,
providing a reproducible overall_score alongside the LLM's own assessment.
Also provides 0-100 normalization and penalty-based scoring from findings.
"""

from __future__ import annotations

import logging
import math

from src.domains.review.services.constants import (
    GRADE_THRESHOLDS,
    MIN_AGGREGATE_COVERAGE,
    PENALTY_FLOOR,
    PENALTY_K,
    SEVERITY_PENALTIES,
)
from src.domains.review.services.text_utils import extract_overall_score

logger = logging.getLogger(__name__)


def score_to_grade(score: float) -> str:
    """Convert a numeric score (0-10) to a letter grade."""
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"


def normalize_to_100(score_10: float) -> float:
    """Convert a 0-10 score to a 0-100 scale.

    Args:
        score_10: Score on the 0-10 scale.

    Returns:
        Score on the 0-100 scale, rounded to 1 decimal place.
    """
    return round(score_10 * 10, 1)


def penalty_score(penalties: list[dict]) -> float:
    """Compute a penalty-based score using exponential decay.

    Formula: score = 100 * exp(-total_penalty / k) with k=35.0 and floor 50.0.

    Args:
        penalties: List of dicts with {severity: str, count: int}.

    Returns:
        Score on 0-100 scale (floored at 50.0).
    """
    total_penalty = 0.0
    for p in penalties:
        severity = p.get("severity", "info")
        count = p.get("count", 0)
        penalty_per = abs(SEVERITY_PENALTIES.get(severity, 0.0))
        total_penalty += penalty_per * count

    if total_penalty == 0.0:
        return 100.0

    raw = 100.0 * math.exp(-total_penalty / PENALTY_K)
    return round(max(raw, PENALTY_FLOOR), 1)


def compute_penalty_from_findings(findings: list[dict]) -> float:
    """Count findings by severity and compute penalty score.

    Args:
        findings: List of finding dicts with at least a 'severity' key.

    Returns:
        Penalty-based score on 0-100 scale.
    """
    counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1

    penalties = [{"severity": sev, "count": cnt} for sev, cnt in counts.items()]
    return penalty_score(penalties)


def compute_weighted_score(
    categories: dict,
    weights: dict[str, float],
) -> float | None:
    """Compute weighted average score from category scores.

    Args:
        categories: Dict of category_name -> {score: float, ...}
        weights: Dict of category_name -> weight (should sum to ~1.0)

    Returns:
        Weighted average score, or None if no valid scores found.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for category, weight in weights.items():
        cat_data = categories.get(category)
        if cat_data is None:
            continue
        score = cat_data.get("score") if isinstance(cat_data, dict) else None
        if score is not None:
            try:
                score_val = float(score)
                weighted_sum += score_val * weight
                total_weight += weight
            except ValueError, TypeError:
                continue

    if total_weight == 0:
        return None

    return round(weighted_sum / total_weight, 2)


def compute_aggregate_score(
    results: dict[str, dict],
    aggregate_weights: dict[str, float],
) -> dict:
    """Compute aggregate score across multiple review types.

    Review types that errored or produced no score are excluded and the
    remaining weights are renormalised -- but the renormalisation is never
    silent. The result carries ``coverage`` (the share of the configured
    aggregate weight that was actually measured) plus the scored/unscored
    review type lists, so a consumer cannot read a 3-of-4 number as a complete
    assessment. Below ``MIN_AGGREGATE_COVERAGE`` the aggregate is refused
    outright (``None``) rather than reported: under half the weight, the
    renormalised number no longer describes the site.

    Args:
        results: Dict of review_type -> review_result_dict
        aggregate_weights: Dict of review_type -> weight

    Returns:
        Dict with aggregate_score, grade, per_review_scores, coverage,
        scored_review_types, unscored_review_types and partial.
    """
    per_review_scores: dict[str, float | None] = {}
    configured_weight = 0.0
    measured_weight = 0.0
    weighted_sum = 0.0

    # DENOMINATOR = what this job ATTEMPTED, not the configured catalogue.
    # `aggregate_weights` is `settings.scoring.aggregate_weights` at every call
    # site -- every review type the SERVICE knows about. A job that legitimately
    # requested one review type would otherwise score coverage = its weight over
    # the whole catalogue (~0.25), fall under the floor, and return NO score for
    # a completely successful run. `results` carries an entry per attempted
    # review type, including the errored/insufficient-data ones the floor exists
    # to catch, so it is the honest denominator: "of the reviews this job
    # actually ran, how many produced a score".
    for review_type, weight in aggregate_weights.items():
        if review_type not in results:
            continue
        configured_weight += weight
        # ``extract_overall_score`` is the single reader of a review's score:
        # it skips errored/insufficient-data results and checks each candidate
        # key with ``is not None``. The ``or`` chain this replaces treated a
        # legitimate 0.0 as absent -- the exact mirror of treating an absent
        # score as 0.0, and it inflated every mean containing a real zero.
        score = extract_overall_score(results.get(review_type))
        per_review_scores[review_type] = score
        if score is None:
            continue
        weighted_sum += score * weight
        measured_weight += weight

    coverage = round(measured_weight / configured_weight, 4) if configured_weight > 0 else 0.0
    unscored = sorted(k for k, v in per_review_scores.items() if v is None)

    aggregate_score: float | None = None
    grade: str | None = None
    if measured_weight > 0 and coverage >= MIN_AGGREGATE_COVERAGE:
        aggregate_score = round(weighted_sum / measured_weight, 2)
        grade = score_to_grade(aggregate_score)
    elif measured_weight > 0:
        logger.warning(
            "Refusing to aggregate: coverage %.2f < %.2f (unscored=%s)",
            coverage,
            MIN_AGGREGATE_COVERAGE,
            unscored,
        )

    return {
        "aggregate_score": aggregate_score,
        "grade": grade,
        "per_review_scores": per_review_scores,
        "coverage": coverage,
        "scored_review_types": sorted(k for k, v in per_review_scores.items() if v is not None),
        "unscored_review_types": unscored,
        "partial": bool(unscored),
    }
