"""Request/response models for the synchronous SEO discoverability audit.

``POST /api/seo/audit`` scores a rendered page (a live URL or a raw HTML string)
for search + AI-answer discoverability and returns a deterministic 0-100 SEO
Health Score, an A-F grade, and a per-group breakdown (on-page / technical /
schema / content / keyword / snippet …) — no LLM and no DB write on the default
path. It is the synchronous, one-shot counterpart to the async ``/api/critique``
pipeline (where ``seo`` is a category), and mirrors the ``/api/cro/score`` and
``/api/brand/score-artifact`` contract shape.

The ``include_llm`` flag is reserved for the opt-in Tier-3 micro-judgment
enrichment (search-intent, E-E-A-T depth); the deterministic score always
returns regardless.
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
    "SeoAuditCheck",
    "SeoAuditFinding",
    "SeoAuditFindingRef",
    "SeoAuditGroupSummary",
    "SeoAuditRequest",
    "SeoAuditResponse",
]


class SeoAuditRequest(ScorecardRequest):
    """SEO audit request — exactly one of ``url`` or ``html`` is required.

    Adds the opt-in competitor content-gap + cross-page crawl fields on top of the
    shared ``url``/``html``/``include_llm`` scorecard base.
    """

    competitor_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Optional competitor page URLs for the opt-in content-gap analyzer. "
            "Runs only when include_llm=true. Capped at 3."
        ),
    )
    cross_page_linking: bool = Field(
        default=False,
        description=(
            "Opt in to cross-page internal-linking analysis (sitemap crawl for "
            "orphan / broken-link / hub signals). Network + page-capped; off by "
            "default so the fast deterministic sync contract is unchanged."
        ),
    )
    max_pages: int = Field(
        default=20,
        le=50,
        description="Page cap for the cross-page internal-linking crawl (max 50).",
    )


class SeoAuditGroupSummary(BaseModel):
    """One SEO audit group's score, weight, and finding rollup.

    ⛔ ``score`` is ``None`` exactly when ``measured`` is ``False``. An empty
    finding list is ambiguous — "scanned, nothing wrong" or "never scanned" — and
    reporting the second as ``score=100, count=0`` made a failed crawl
    byte-identical to a genuinely clean site. The gap now has its own shape.
    """

    group: str
    score: float | None = Field(
        ...,
        description=(
            "0-100 penalty-decay score for this group, or null when the group was "
            "not measured (see `measured` / `note`)"
        ),
    )
    weight: float = Field(..., description="Group weight in the composite")
    count: int = Field(default=0, description="Number of findings in this group")
    top_severity: str = Field(default="info", description="Most severe finding in this group")
    measured: bool = Field(
        default=True,
        description=(
            "False when this group's scanner did not run. An unmeasured group is "
            "excluded from overall_score — it is neither clean nor broken, it is unknown."
        ),
    )
    note: str | None = Field(
        default=None, description="Why this group is unmeasured (only when measured=false)"
    )


class SeoAuditFindingRef(BaseModel):
    """Lightweight reference to a finding (no full body) for action buckets.

    ``priority`` / ``rank`` / ``evidence`` / ``selector`` are additive and appear
    only when the request opts into the matching enrichment; absent otherwise so
    the default response is byte-identical.
    """

    check_id: str
    group: str
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


class SeoAuditFinding(AuditFindingBase):
    """A full finding row (all severities, with description + deterministic fix)."""

    group: str


class SeoAuditCheck(AuditCheckBase):
    """One catalogued check with its pass / fail / not_run outcome."""

    group: str


class SeoAuditResponse(BaseModel):
    """Deterministic SEO Health Score for one rendered page."""

    overall_score: float = Field(..., description="Weighted 0-100 SEO Health Score")
    grade: str = Field(..., description="Letter grade A+ through F")
    group_scores: dict[str, float | None] = Field(
        default_factory=dict,
        description="Per-group 0-100 score; null for a group that was not measured",
    )
    groups: list[SeoAuditGroupSummary] = Field(default_factory=list)
    quick_wins: list[SeoAuditFindingRef] = Field(
        default_factory=list, description="High-impact, low-effort fixes"
    )
    strategic: list[SeoAuditFindingRef] = Field(
        default_factory=list, description="Higher-effort or critical fixes"
    )
    source: str = Field(default="", description="The audited URL, or 'inline-html' for raw HTML")
    llm_enriched: bool = Field(
        default=False, description="Whether Tier-3 LLM judgments were applied"
    )
    unmeasured_groups: list[str] = Field(
        default_factory=list,
        description=(
            "Groups whose scanner did not run (e.g. the cross-page linking crawl "
            "timed out). Always includes `performance`, which this path never "
            "measures (weight 0, outside the rubric). These groups are excluded "
            "from overall_score rather than being credited a perfect 100."
        ),
    )
    coverage: float = Field(
        default=1.0,
        description=(
            "Share of the total group weight that was actually measured (0-1). "
            "1.0 means overall_score covers the full rubric."
        ),
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
    findings: list[SeoAuditFinding] = Field(
        default_factory=list,
        description="Every finding (all severities), ranked, with description and fix",
    )
    checks: list[SeoAuditCheck] = Field(
        default_factory=list,
        description="Check inventory: every catalogued check with pass/fail/not_run",
    )
    facts: dict[str, object] = Field(
        default_factory=dict,
        description="Measured page facts behind the checks (per-type shape)",
    )
    ai: AiStatus | None = Field(
        default=None,
        description="AI-analysis status; always present on responses from this version",
    )
    scored_at: str = Field(default="", description="UTC ISO-8601 timestamp of the audit")
