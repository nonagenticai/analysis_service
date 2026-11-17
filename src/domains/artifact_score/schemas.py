"""Request/response models for synchronous artifact quality scoring.

``POST /api/brand/score-artifact`` scores a rendered artifact (a live URL or a
raw HTML string) and returns a deterministic quality score with no LLM and no
DB write. It is the synchronous, one-shot counterpart to the async, job-based
``/api/critique`` pipeline: it reuses critique's pure scoring core + the cheap
static scanners (SEO / accessibility / performance) while skipping the slow
LLM (ux/summary) and live-network (security/seo_live) scanners.

The response is the **wire contract** consumed by Merqi's Branding Coach as
*supporting evidence* on a rendered ``brand_artifact`` (see
``BRANDING_COACH_SPEC.md`` §5.4/§11). It is authoritative for nothing on the
Merqi side — the Perceived Value Index is computed there.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.domains.scorecards.schemas import SourceXorValidatorMixin

__all__ = [
    "STATIC_CATEGORIES",
    "ArtifactScoreRequest",
    "ArtifactScoreResponse",
    "ArtifactSource",
    "FindingSummaryItem",
    "ScoreDeltaRequest",
    "ScoreDeltaResponse",
]

# The only categories runnable on the synchronous path: they score from an
# in-memory HTML string with no LLM call and no live-network probe. ``ux``
# (LLM), ``security`` and ``seo_live`` (network) are deliberately excluded —
# they are what make the async critique job slow.
STATIC_CATEGORIES: tuple[str, ...] = ("seo", "accessibility", "performance")


class ArtifactScoreRequest(SourceXorValidatorMixin, BaseModel):
    """Score request — exactly one of ``url`` or ``html`` is required.

    ``categories`` defaults to the full static set; any requested category
    outside :data:`STATIC_CATEGORIES` is dropped (ux/security/seo_live are not
    supported synchronously). A request whose categories reduce to nothing is a
    ``422`` at the route.
    """

    url: str | None = Field(default=None, description="Public HTTP(S) URL of the rendered artifact")
    html: str | None = Field(default=None, description="Raw rendered HTML (skips the fetch)")
    categories: list[str] | None = Field(
        default=None,
        description=(
            "Subset of ('seo','accessibility','performance') to score. "
            "Defaults to all three; unsupported categories are ignored."
        ),
    )


class FindingSummaryItem(BaseModel):
    """Per-category rollup of the findings that fed the score (no raw findings)."""

    category: str
    count: int
    top_severity: str


class ArtifactScoreResponse(BaseModel):
    """Flat, deterministic quality score for one rendered artifact."""

    overall_score: float = Field(..., description="Weighted 0-100 composite of category scores")
    category_scores: dict[str, float] = Field(default_factory=dict)
    confidence: dict[str, float] = Field(default_factory=dict)
    findings_summary: list[FindingSummaryItem] = Field(default_factory=list)
    source: str = Field(default="", description="The scored URL, or 'inline-html' for raw HTML")
    categories: list[str] = Field(default_factory=list, description="Categories actually scored")
    scored_at: str = Field(default="", description="UTC ISO-8601 timestamp of the scoring")


class ArtifactSource(SourceXorValidatorMixin, BaseModel):
    """One side of a delta — exactly one of ``url`` or ``html`` is required."""

    url: str | None = Field(default=None, description="Public HTTP(S) URL of the rendered artifact")
    html: str | None = Field(default=None, description="Raw rendered HTML (skips the fetch)")


class ScoreDeltaRequest(BaseModel):
    """Before/after delta request — each side scored with the same categories."""

    before: ArtifactSource = Field(..., description="Baseline artifact (the 'before')")
    after: ArtifactSource = Field(..., description="Revised artifact (the 'after')")
    categories: list[str] | None = Field(
        default=None,
        description=(
            "Subset of ('seo','accessibility','performance') to score both sides with. "
            "Defaults to all three; unsupported categories are ignored."
        ),
    )


class ScoreDeltaResponse(BaseModel):
    """Before/after quality delta for two rendered artifacts.

    ``score_deltas``/``overall_delta`` are ``after - before`` (positive = the
    'after' scored higher). ``new_findings``/``resolved_findings`` are the
    per-category rollups of issues that appeared in / disappeared from the
    'after' (same no-raw-findings shape as :class:`ArtifactScoreResponse`).
    """

    before: ArtifactScoreResponse
    after: ArtifactScoreResponse
    score_deltas: dict[str, float] = Field(default_factory=dict)
    overall_delta: float = Field(..., description="after.overall_score - before.overall_score")
    new_findings: list[FindingSummaryItem] = Field(default_factory=list)
    resolved_findings: list[FindingSummaryItem] = Field(default_factory=list)
    persistent_findings: int = Field(default=0, description="Count of findings present in both")
