"""Pydantic models for LLM output validation.

Replaces the duck-type REVIEW_SCHEMAS dict + _validate_review_schema()
with proper Pydantic models that coerce common LLM quirks (e.g. string
scores like ``"7"`` -> ``7.0``) and provide sensible defaults for missing
fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# "Not assessed" vs "assessed badly"
# ---------------------------------------------------------------------------
# Every score field below defaults to ``None``, never ``0.0``. A missing score
# is an ABSENT measurement; ``0.0`` is the worst possible real measurement and
# grades as an F. Defaulting the first to the second is how a review that never
# happened reached a customer as "Score: 0.0/10 (F)".


class CategoryScore(BaseModel):
    model_config = ConfigDict(extra="allow")

    score: float | None = None
    observations: list[str] = []
    notes: str = ""


class DesignReviewOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    overall_score: float | None = None
    categories: dict[str, CategoryScore | float] = {}
    strengths: list[str] = []
    weaknesses: list[str] = []
    recommendations: list[str] = []


class UsabilityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    overall_score: float | None = None
    categories: dict[str, CategoryScore | float] = {}
    critical_issues: list[str | dict] = []
    quick_wins: list[str | dict] = []
    long_term_improvements: list[str | dict] = []


class FeatureReviewOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    vertical: str = ""
    feature_completeness_score: float | None = None
    present_features: dict = {}
    missing_features: list[str | dict] = []


class ContentQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    overall_score: float | None = None
    categories: dict[str, CategoryScore | float] = {}
    strengths: list[str] = []
    issues: list[str | dict] = []
    recommendations: list[str] = []


class Proposal(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int | str = 0
    title: str = ""
    category: str = ""
    impact: str = ""
    complexity: str = ""
    description: str = ""
    rationale: str = ""


class FeatureSuggestionsOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_proposals: int = 0
    proposals: list[Proposal | dict] = []


LLM_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "design_review": DesignReviewOutput,
    "usability_review": UsabilityReviewOutput,
    "feature_review": FeatureReviewOutput,
    "content_quality_review": ContentQualityReviewOutput,
    "quality": ContentQualityReviewOutput,
    "feature_suggestions": FeatureSuggestionsOutput,
}


def validate_llm_output(review_type: str, data: dict) -> dict:
    """Validate and coerce LLM output using the appropriate Pydantic model.

    Returns ``model_dump()`` dict. Falls back to the raw dict for unknown
    review types.
    """
    model_cls = LLM_OUTPUT_MODELS.get(review_type)
    if model_cls is None:
        return data
    validated = model_cls.model_validate(data)
    return validated.model_dump()


def insufficient_data_result(review_type: str, message: str) -> dict:
    """Build the canonical "this review was never assessed" result.

    The marker reuses the ``error`` key that every consumer in this domain
    already reads as "no score here" -- ``text_utils.extract_overall_score``,
    ``report_generator``/``html_report_generator``, ``summary`` and
    ``scoring.compute_aggregate_score`` all skip a result carrying it. That is
    what keeps an unmeasured review from being printed as a grade or subtracted
    as a delta, without every one of those call sites needing a new branch.

    ``insufficient_data`` is what distinguishes it from a genuine review
    failure (a WM timeout, a malformed response) for anything that wants to
    tell the two apart, and from a genuine ``0.0``, which carries no ``error``
    key at all and still grades as an F.
    """
    return {
        "error": message,
        "insufficient_data": True,
        "review_type": review_type,
    }
