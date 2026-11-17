"""Shared composite-scorecard kernel — per-bucket penalty decay + weighted mean.

Every w2v scorecard (CRO sub-sections, SEO audit groups, marketing dimensions)
reconciles the production exponential-decay penalty model
(:func:`compute_category_score`) with a reference rubric expressed as *weighted
buckets*: apply the same decay to each bucket's finding slice independently, then
take the reference-weighted mean of the bucket scores. A bucket with no findings
scores 100 (nothing wrong), exactly as the category engine treats an empty
category.

Before this module the identical "slice by bucket → decay → weighted mean" loop
was re-implemented in ``cro.scoring``, ``seo.scoring`` and ``marketing.scoring``.
They now all call in here; a new scorecard is a ``{weights, bucket_of}`` config,
not another copy of the loop.

⛔ AN EMPTY BUCKET IS AMBIGUOUS. "No findings" means either "scanned, nothing
wrong" (100 is correct) or "the scanner never ran" (100 is a lie, and it lifts
the weighted mean). The kernel cannot tell those apart from the finding list
alone, so the *caller* — which knows whether its scanner raised — declares the
gap via ``unmeasured``. Such a bucket scores ``None`` (not 0, which would report
a perfect site as broken over a transient network blip) and its weight is dropped
from the mean, which is then renormalised over the buckets that were actually
measured. Default ``unmeasured=()`` ⇒ every existing caller is byte-identical.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from src.domains.critique.services.report_generator import (
    compute_category_score,
    score_to_grade,
)

BucketResolver = Callable[[str | None], str | None]


def score_buckets(
    findings: list[dict],
    *,
    category: str,
    weights: dict[str, float],
    bucket_of: BucketResolver,
) -> dict[str, float]:
    """Decay-score each weighted bucket over just that bucket's finding slice.

    Only ``category``-matching findings contribute. Each bucket in ``weights`` is
    scored with :func:`compute_category_score` over the findings whose
    ``bucket_of(check_id)`` equals that bucket (100.0 when the slice is empty), so
    severity drives the penalty consistently with every other category.
    """
    cat_findings = [f for f in findings if f.get("category") == category]
    return {
        bucket: compute_category_score(
            [f for f in cat_findings if bucket_of(f.get("check_id")) == bucket],
            category,
        )
        for bucket in weights
    }


def apply_unmeasured(
    bucket_scores: dict[str, float], unmeasured: Collection[str]
) -> dict[str, float | None]:
    """Overwrite the score of every ``unmeasured`` bucket with ``None``.

    ``None`` is this kernel's one representation of "not measured" — deliberately
    NOT ``0.0`` (a broken-site verdict the evidence does not support) and NOT
    ``100.0`` (the clean-site verdict that is the defect being fixed). Returns a
    new dict; with the default empty ``unmeasured`` it is a plain copy, so the
    downstream mean is bit-for-bit what it always was.
    """
    return {b: (None if b in unmeasured else s) for b, s in bucket_scores.items()}


def measured_coverage(
    bucket_scores: Mapping[str, float | None], weights: dict[str, float]
) -> float:
    """Fraction of the rubric's total weight that was actually measured (0-1)."""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    measured = sum(w for b, w in weights.items() if bucket_scores.get(b) is not None)
    return round(measured / total, 4)


def weighted_overall(bucket_scores: Mapping[str, float | None], weights: dict[str, float]) -> float:
    """Reference-weighted mean of the MEASURED bucket scores, to one decimal.

    A bucket scoring ``None`` was never measured, so it contributes neither its
    score nor its weight: the mean is renormalised over the remaining weight. An
    unmeasured bucket therefore cannot lift (or sink) the overall the way a
    defaulted ``100.0`` did. With no ``None`` present this is the original
    ``sum(score * w) / sum(w)`` exactly.

    Raises ``ValueError`` when nothing was measured — there is no honest number to
    return for a scorecard whose every bucket failed, and inventing one is the
    defect this module exists to prevent.
    """
    # Indexing (not .get) keeps the original KeyError for a bucket the caller
    # forgot to score: a MISSING bucket is a caller bug, not a measurement gap.
    measured: dict[str, float] = {
        b: score for b in weights if (score := bucket_scores[b]) is not None
    }
    total_weight = sum(weights[b] for b in measured)
    if total_weight <= 0:
        raise ValueError("every weighted bucket is unmeasured — no score can be computed")
    overall = sum(score * weights[b] for b, score in measured.items()) / total_weight
    return round(overall, 1)


def compute_scorecard(
    findings: list[dict],
    *,
    category: str,
    weights: dict[str, float],
    bucket_of: BucketResolver,
    breakdown_key: str,
    unmeasured: Collection[str] = (),
) -> dict[str, Any]:
    """Return ``{overall_score, grade, <breakdown_key>: bucket_scores}``.

    The single-engine scorecard shape (CRO, SEO). Composite scorecards that borrow
    bucket scores from other engines (marketing) call :func:`score_buckets` and
    :func:`weighted_overall` directly and assemble their own breakdown.

    ``unmeasured`` names the buckets whose scanner did NOT run (a crawl timeout, a
    network error). Each scores ``None`` and is dropped from the weighted mean, and
    two extra keys — ``unmeasured`` (sorted names) and ``coverage`` (measured share
    of the total weight) — are added so the gap is visible downstream. Those keys
    appear ONLY when something is actually unmeasured, so the default result dict
    is byte-identical for every existing caller.
    """
    scored = score_buckets(findings, category=category, weights=weights, bucket_of=bucket_of)
    bucket_scores = apply_unmeasured(scored, unmeasured)
    overall = weighted_overall(bucket_scores, weights)
    result: dict[str, Any] = {
        "overall_score": overall,
        "grade": score_to_grade(overall),
        breakdown_key: bucket_scores,
    }
    gaps = sorted(b for b in weights if b in unmeasured)
    if gaps:
        result["unmeasured"] = gaps
        result["coverage"] = measured_coverage(bucket_scores, weights)
    return result
