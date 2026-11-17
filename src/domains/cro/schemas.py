"""Request/response models for synchronous CRO (conversion) scoring.

``POST /api/cro/score`` scores a rendered page (a live URL or a raw HTML string)
for conversion-rate-optimization and returns a deterministic 0-100 score, an
A-F grade, and a per-sub-section breakdown — no LLM and no DB write on the
default path. It is the synchronous, one-shot counterpart to the async,
job-based ``/api/critique`` pipeline (where ``cro`` is an opt-in category), and
mirrors the ``/api/brand/score-artifact`` contract shape.

The ``include_llm`` flag is reserved for the opt-in Tier-3 micro-judgment
enrichment; the deterministic score always returns regardless.
"""

from __future__ import annotations

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
    "CroCheck",
    "CroExperiment",
    "CroFacts",
    "CroFinding",
    "CroFindingRef",
    "CroLlmJudgment",
    "CroScoreRequest",
    "CroScoreResponse",
    "CroSubsectionSummary",
]


class CroScoreRequest(ScorecardRequest):
    """CRO score request — exactly one of ``url`` or ``html`` is required."""


class CroSubsectionSummary(BaseModel):
    """One CRO sub-section's score, weight, and finding rollup.

    ``score`` is ``None`` (with ``measured=False`` and a ``note``) when the
    section's checks could not run on this page — e.g. ``form`` on a page with no
    conversion form — so it is never dressed up as a clean 100.
    """

    section: str
    score: float | None = Field(
        ..., description="0-100 penalty-decay score for this sub-section (null = not measured)"
    )
    weight: float = Field(..., description="Sub-section weight in the composite")
    count: int = Field(default=0, description="Number of findings in this sub-section")
    top_severity: str = Field(default="info", description="Most severe finding in this sub-section")
    measured: bool = Field(default=True, description="False when the section could not be scored")
    note: str | None = Field(default=None, description="Why the section was not measured")


class CroFindingRef(BaseModel):
    """Lightweight reference to a finding (no full body) for action buckets.

    ``priority`` / ``rank`` / ``evidence`` / ``selector`` are additive and appear
    only when the request opts into the matching enrichment; absent otherwise so
    the default response is byte-identical.
    """

    check_id: str
    section: str
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


class CroLlmJudgment(BaseModel):
    """One Tier-3 LLM micro-judgment on an extracted copy snippet (advisory only)."""

    element: str = Field(..., description="Snippet element judged: headline/value_prop/testimonial")
    score: float = Field(..., description="0-100 score on that element's own conversion axis")
    assessment: str = Field(default="", description="One-sentence assessment")
    suggestion: str = Field(default="", description="One concrete rewrite/action")


class CroExperiment(BaseModel):
    """An authored A/B experiment for a check (static catalog, design CRO-G4)."""

    hypothesis: str
    primary_metric: str
    variant: str


class CroFinding(AuditFindingBase):
    """A full finding row (all severities, with description + deterministic fix)."""

    section: str
    experiment: CroExperiment | None = Field(
        default=None,
        description="Authored experiment for this check; absent = hypothesis not authored",
    )


class CroHeroFacts(BaseModel):
    h1_text: str | None = None
    h1_length: int | None = Field(default=None, description="H1 length in characters")
    h1_word_count: int | None = None
    h1_count: int = 0
    has_cta_above_fold: bool | None = Field(
        default=None, description="Always null: a server-side fetch has no layout geometry"
    )
    cta_before_first_h2: bool = Field(
        default=False, description="Document-order proxy: the first CTA precedes the first <h2>"
    )
    nav_link_count: int = 0
    trust_signal: bool = False
    trust_signal_match: str | None = None


class CroValuePropFacts(BaseModel):
    lead_text: str | None = None
    bullets: int = Field(default=0, description="List items on the page (as the check counts)")
    quantified_benefit: bool | None = Field(
        default=None, description="null when there is no headline/lead copy to inspect"
    )
    quantified_benefit_match: str | None = None
    reading_grade: float | None = Field(
        default=None, description="Flesch-Kincaid grade; null below 120 words"
    )


class CroCtaFact(BaseModel):
    text: str
    tag: str
    href_kind: str = Field(
        ...,
        description="none|anchor|mailto|tel|javascript|internal|external|absolute|"
        "form_submit|button",
    )
    position_index: int = Field(..., description="0-based position among CTAs in page order")
    action_verb: bool = False
    first_person: bool = False
    generic: bool = False
    has_microcopy: bool = False


class CroCtaSummary(BaseModel):
    count: int = 0
    generic_count: int = 0
    generic_ratio: float | None = None
    first_person_count: int = 0
    with_microcopy_count: int = 0
    truncated: bool = False


class CroFormField(BaseModel):
    type: str
    name: str | None = None
    label: str | None = None
    required: bool = False
    autocomplete: str | None = None


class CroFormFacts(BaseModel):
    present: bool = False
    field_count: int | None = None
    required_count: int | None = None
    textareas: int | None = None
    selects: int | None = None
    placeholder_only_labels: int | None = None
    email_type_ok: bool | None = Field(
        default=None, description="null when there is no email field"
    )
    autocomplete: bool | None = None
    multistep: bool | None = None
    fields: list[CroFormField] = Field(default_factory=list)


class CroTrustFacts(BaseModel):
    testimonials: bool = False
    named_testimonials: bool | None = Field(
        default=None, description="null when there are no testimonials to attribute"
    )
    logos: bool = False
    ratings: bool = False
    metrics: bool = False
    faq: bool = False
    risk_reversal: bool = False
    security_assurance: bool = False
    contact: bool = False


class CroJourneyStage(BaseModel):
    stage: str
    label: str
    sections: list[str]
    section_scores: dict[str, float | None] = Field(default_factory=dict)
    score: float | None = None
    measured: bool = True


class CroJourneyFriction(BaseModel):
    section: str = "tech"
    score: float | None = None


class CroJourney(BaseModel):
    stages: list[CroJourneyStage] = Field(default_factory=list)
    friction: CroJourneyFriction = Field(default_factory=CroJourneyFriction)
    weakest_stage: str | None = None


class CroFacts(BaseModel):
    """Measured CRO page facts. A block is null when facts could not be built."""

    hero: CroHeroFacts | None = None
    value_prop: CroValuePropFacts | None = None
    ctas: list[CroCtaFact] = Field(default_factory=list)
    cta_summary: CroCtaSummary | None = None
    form: CroFormFacts | None = None
    trust: CroTrustFacts | None = None
    journey: CroJourney | None = None


class CroCheck(AuditCheckBase):
    """One catalogued check with its pass / fail / not_run outcome."""

    section: str


class CroScoreResponse(BaseModel):
    """Deterministic CRO conversion score for one rendered page."""

    overall_score: float = Field(..., description="Weighted 0-100 CRO composite")
    grade: str = Field(..., description="Letter grade A+ through F")
    subsection_scores: dict[str, float | None] = Field(
        default_factory=dict, description="Per-section score; null = not measured on this page"
    )
    subsections: list[CroSubsectionSummary] = Field(default_factory=list)
    unmeasured_sections: list[str] = Field(
        default_factory=list, description="Sections not measured (excluded from overall_score)"
    )
    coverage: float = Field(
        default=1.0, description="Share of the section weight that was measured (0-1)"
    )
    quick_wins: list[CroFindingRef] = Field(
        default_factory=list, description="High-impact, low-effort fixes"
    )
    strategic: list[CroFindingRef] = Field(
        default_factory=list, description="Higher-effort or critical fixes"
    )
    source: str = Field(default="", description="The scored URL, or 'inline-html' for raw HTML")
    llm_enriched: bool = Field(
        default=False, description="Whether Tier-3 LLM judgments were applied"
    )
    llm_judgments: list[CroLlmJudgment] = Field(
        default_factory=list, description="Tier-3 LLM micro-judgments (empty unless include_llm)"
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
    findings: list[CroFinding] = Field(
        default_factory=list,
        description="Every finding (all severities), ranked, with description and fix",
    )
    checks: list[CroCheck] = Field(
        default_factory=list,
        description="Check inventory: every catalogued check with pass/fail/not_run",
    )
    facts: CroFacts = Field(
        default_factory=CroFacts,
        description="Measured page facts behind the checks (hero, CTAs, form, trust, journey)",
    )
    ai: AiStatus | None = Field(
        default=None,
        description="AI-analysis status; always present on responses from this version",
    )
    scored_at: str = Field(default="", description="UTC ISO-8601 timestamp of the scoring")
