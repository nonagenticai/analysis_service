"""Request/response models for the synchronous marketing scorecard.

``POST /api/marketing/scorecard`` scores a rendered page (a live URL or a raw
HTML string) as a go-to-market asset and returns a deterministic 0-100 composite,
an A-F grade, and a per-dimension breakdown across the six reference marketing
dimensions (content / conversion / seo / competitive / brand / growth) — no LLM
and no DB write on the default path. It is the synchronous, one-shot marketing
lens next to the technical ``/api/critique`` composite, and mirrors the
``/api/cro/score`` and ``/api/seo/audit`` contract shape.

The composite reuses the CRO and SEO engines in-process (conversion / content /
seo dimensions) plus light deterministic checks for competitive / brand / growth.
The ``include_llm`` flag adds advisory, score-invariant copy assessments; the
deterministic score always returns regardless.

This is the w2v go-to-market composite Merqi can consume as *supporting
evidence* — it is not the authoritative PVI (that is computed on the Merqi side).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from src.domains.scorecards.schemas import (
    AiStatus,
    AuditCheckBase,
    AuditFindingBase,
    CritiqueResult,
    ExecSummary,
    GroundingResult,
    Recommendation,
    ScorecardRequest,
    ScoreProjection,
)

__all__ = [
    "MarketingCheck",
    "MarketingDimensionSummary",
    "MarketingFacts",
    "MarketingFinding",
    "MarketingFindingRef",
    "MarketingLlmAssessment",
    "MarketingScorecardRequest",
    "MarketingScorecardResponse",
]


class MarketingScorecardRequest(ScorecardRequest):
    """Marketing scorecard request — exactly one of ``url`` or ``html`` is required."""


class MarketingDimensionSummary(BaseModel):
    """One scorecard dimension's score, weight, and finding rollup."""

    dimension: str
    score: float = Field(..., description="0-100 score for this dimension")
    weight: float = Field(..., description="Dimension weight in the composite")
    count: int = Field(default=0, description="Number of findings attributed to this dimension")
    top_severity: str = Field(default="info", description="Most severe finding in this dimension")


class MarketingFindingRef(BaseModel):
    """Lightweight reference to a finding (no full body) for action buckets.

    ``priority`` / ``rank`` / ``evidence`` / ``selector`` are additive and appear
    only when the request opts into the matching enrichment; absent otherwise so
    the default response is byte-identical.
    """

    check_id: str
    dimension: str
    title: str
    severity: str
    priority: float | None = Field(default=None, description="Deterministic ROI priority score")
    rank: int | None = Field(default=None, description="1-based rank among actionable findings")
    evidence: str | None = Field(default=None, description="Raw evidence snippet, if captured")
    selector: str | None = Field(default=None, description="CSS selector for the finding, if known")
    recommendation: Recommendation | None = Field(
        default=None,
        description="Evidence-grounded fix prose (only when include_recommendations and a fix was written)",
    )


class MarketingLlmAssessment(BaseModel):
    """One Tier-3 LLM copy assessment (advisory only — never moves the score)."""

    element: str = Field(..., description="Snippet element judged: headline/value_prop/testimonial")
    dimension: str = Field(default="content", description="Scorecard dimension this informs")
    score: float = Field(..., description="0-100 score on that element's own axis")
    assessment: str = Field(default="", description="One-sentence assessment")
    suggestion: str = Field(default="", description="One concrete rewrite/action")


class MarketingFinding(AuditFindingBase):
    """A full finding row (all severities, with description + deterministic fix)."""

    dimension: str


class MarketingCheck(AuditCheckBase):
    """One catalogued check with its pass / fail / not_run outcome."""

    dimension: str


class PresenceFact(BaseModel):
    """Whether an element exists, and its URL when the markup carries one."""

    present: bool
    url: str | None = None


class PositioningFacts(BaseModel):
    """Fold copy + competitive signals (``competitive.py`` detectors)."""

    fold_copy_excerpt: str | None = Field(default=None, description="H1/H2 copy, truncated")
    differentiation_claim: str | None = Field(
        default=None, description="The fold copy around the first differentiation claim, if any"
    )
    comparison_page: bool = Field(..., description="Comparison / 'vs' / alternatives content found")
    reputation_signals: list[str] = Field(
        default_factory=list, description="Review platforms / media mentions named on the page"
    )


class ContactFact(BaseModel):
    """Contact details exposed by the page."""

    present: bool
    email: str | None = None
    phone: str | None = None


class SocialCardFields(BaseModel):
    """Which share-card meta tags are declared."""

    og_title: bool
    og_description: bool
    og_image: bool
    twitter_card: bool


class BrandFacts(BaseModel):
    """Brand-identity assets (``brand.py`` detectors)."""

    logo: PresenceFact
    og_image_url: str | None = None
    favicon: PresenceFact
    about_link: PresenceFact
    contact: ContactFact
    social_card_fields: SocialCardFields


class SocialProfile(BaseModel):
    """One owned social channel linked from the page."""

    platform: str
    url: str


class ChannelFacts(BaseModel):
    """Owned channels + measurement (``growth.py`` / ``tracking.py`` detectors)."""

    social_profiles: list[SocialProfile] = Field(default_factory=list)
    email_capture: bool
    analytics_vendors: list[str] = Field(default_factory=list)
    consent: bool
    pricing_visible: bool
    referral: bool
    retention_hook: bool


class NotMeasuredFact(BaseModel):
    """A fact a page scan cannot produce — rendered as "Not measured — <reason>"."""

    measured: Literal[False] = False
    reason: str


class DimensionSource(BaseModel):
    """One row of the weights table: where a dimension's score comes from."""

    dimension: str
    weight: float
    score: float | None = None
    weighted_points: float | None = None
    source_engine: Literal["cro", "seo", "marketing"]
    reference_only: bool = Field(
        ..., description="Borrowed from the CRO/SEO engine — details live in that report"
    )
    report_pointer: Literal["cro_audit", "seo_audit"] | None = None


class MarketingFacts(BaseModel):
    """Measured page facts behind the marketing checks (design 2026-10-17 MKT-G1).

    A section that could not be extracted is ``None`` — never a default that
    reads as a measurement.
    """

    positioning: PositioningFacts | None = None
    brand: BrandFacts | None = None
    channels: ChannelFacts | None = None
    competitive_benchmark: NotMeasuredFact
    channel_performance: NotMeasuredFact
    dimension_sources: list[DimensionSource] = Field(default_factory=list)


class MarketingScorecardResponse(BaseModel):
    """Deterministic marketing-presence scorecard for one rendered page."""

    overall_score: float = Field(..., description="Weighted 0-100 marketing composite")
    grade: str = Field(..., description="Letter grade A+ through F")
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    dimensions: list[MarketingDimensionSummary] = Field(default_factory=list)
    quick_wins: list[MarketingFindingRef] = Field(
        default_factory=list, description="High-impact, low-effort fixes"
    )
    strategic: list[MarketingFindingRef] = Field(
        default_factory=list, description="Higher-effort or critical fixes"
    )
    source: str = Field(default="", description="The scored URL, or 'inline-html' for raw HTML")
    llm_enriched: bool = Field(
        default=False, description="Whether Tier-3 LLM assessments were applied"
    )
    llm_assessments: list[MarketingLlmAssessment] = Field(
        default_factory=list, description="Tier-3 LLM copy assessments (empty unless include_llm)"
    )
    projection: ScoreProjection | None = Field(
        default=None,
        description="Score lift from fixing top findings (only when include_projection)",
    )
    grounding: list[GroundingResult] | None = Field(
        default=None,
        description="Per-recommendation claim-grounding verdicts (only when include_grounding)",
    )
    exec_summary: ExecSummary | None = Field(
        default=None,
        description="Executive summary + id-locked roadmap (only when include_exec_summary)",
    )
    critique: CritiqueResult | None = Field(
        default=None,
        description="Holistic report-critic verdict (only when include_critic)",
    )
    findings: list[MarketingFinding] = Field(
        default_factory=list,
        description="Every finding (all severities), ranked, with description and fix",
    )
    checks: list[MarketingCheck] = Field(
        default_factory=list,
        description="Check inventory: every catalogued check with pass/fail/not_run",
    )
    facts: MarketingFacts | None = Field(
        default=None,
        description="Measured page facts behind the checks; always present from this version",
    )
    ai: AiStatus | None = Field(
        default=None,
        description="AI-analysis status; always present on responses from this version",
    )
    scored_at: str = Field(default="", description="UTC ISO-8601 timestamp of the scoring")
