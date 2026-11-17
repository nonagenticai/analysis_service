"""Summary generation for review results.

Aggregates top strengths, critical issues, and recommendations
across all review types into an executive summary.
"""

from __future__ import annotations

import logging

from src.domains.review.services.findings import extract_findings
from src.domains.review.services.scoring import (
    compute_aggregate_score,
    compute_penalty_from_findings,
    normalize_to_100,
)
from src.domains.review.services.text_utils import extract_item_text

logger = logging.getLogger(__name__)


def generate_summary(
    job_id: str,
    results: dict[str, dict],
    aggregate_weights: dict[str, float],
) -> dict:
    """Generate an executive summary across all review types.

    Args:
        job_id: The review job ID.
        results: Dict of review_type -> review_result_dict.
        aggregate_weights: Dict of review_type -> weight for scoring.

    Returns:
        Summary dict with aggregate scores, top findings, and recommendations.
    """
    scoring = compute_aggregate_score(results, aggregate_weights)

    # Compute normalized and penalty scores
    score_100 = None
    if scoring["aggregate_score"] is not None:
        score_100 = normalize_to_100(scoring["aggregate_score"])

    findings = extract_findings(results)
    pen_score = compute_penalty_from_findings(findings) if findings else None

    top_strengths, critical_issues, top_recommendations = _collect_summary_buckets(results)
    top_strengths = _deduplicate(top_strengths, max_items=10)
    critical_issues = _deduplicate(critical_issues, max_items=10)
    top_recommendations = _deduplicate(top_recommendations, max_items=10)

    return {
        "job_id": job_id,
        "aggregate_score": scoring["aggregate_score"],
        "grade": scoring["grade"],
        "per_review_scores": scoring["per_review_scores"],
        # Coverage travels with the aggregate so a partial job is never read as
        # a complete one. See scoring.compute_aggregate_score.
        "coverage": scoring["coverage"],
        "scored_review_types": scoring["scored_review_types"],
        "unscored_review_types": scoring["unscored_review_types"],
        "partial": scoring["partial"],
        "top_strengths": top_strengths,
        "critical_issues": critical_issues,
        "top_recommendations": top_recommendations,
        "score_100": score_100,
        "penalty_score": pen_score,
    }


def _collect_into(bucket: list[str], items: list, limit: int) -> None:
    for item in items[:limit]:
        text = extract_item_text(item).strip()
        if text:
            bucket.append(text)


def _collect_summary_buckets(
    results: dict[str, dict],
) -> tuple[list[str], list[str], list[str]]:
    """Collect (strengths, issues, recommendations) across non-error reviews."""
    top_strengths: list[str] = []
    critical_issues: list[str] = []
    top_recommendations: list[str] = []
    for result in results.values():
        if "error" in result:
            continue
        _collect_into(top_strengths, result.get("strengths", []), 3)
        for key in ("critical_issues", "weaknesses", "issues"):
            _collect_into(critical_issues, result.get(key, []), 3)
        _collect_into(top_recommendations, result.get("recommendations", []), 3)
        for key in ("quick_wins", "long_term_improvements"):
            _collect_into(top_recommendations, result.get(key, []), 2)
    return top_strengths, critical_issues, top_recommendations


def _deduplicate(items: list[str], max_items: int = 10) -> list[str]:
    """Remove duplicates while preserving order, capped at max_items."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if normalized not in seen:
            seen.add(normalized)
            result.append(item)
            if len(result) >= max_items:
                break
    return result
