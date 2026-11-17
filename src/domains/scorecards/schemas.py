"""Shared request base for the synchronous scorecard domains.

``POST /api/cro/score``, ``/api/seo/audit`` and ``/api/marketing/scorecard`` all
take exactly one of ``url`` / ``html`` plus an ``include_llm`` flag, enforced by
an identical ``model_validator``. That base lives here; each domain subclasses it
(SEO adds competitor / cross-page fields).
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


class SourceXorValidatorMixin:
    """Mixin: enforce that a model declaring ``url`` / ``html`` sets exactly one.

    Holds *only* the validator (not the fields) so each model keeps its own
    ``url`` / ``html`` field declarations and descriptions; pydantic collects the
    decorated ``model_validator`` via the MRO. Mixed into
    :class:`ScorecardRequest` here and into the ``artifact_score`` request models,
    which previously each carried a byte-identical copy of this rule.
    """

    url: str | None
    html: str | None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> Self:
        has_url = bool(self.url and self.url.strip())
        has_html = bool(self.html and self.html.strip())
        if has_url == has_html:
            raise ValueError("exactly one of 'url' or 'html' is required")
        return self


class ScorecardRequest(SourceXorValidatorMixin, BaseModel):
    """Base scorecard request — exactly one of ``url`` or ``html`` is required."""

    url: str | None = Field(default=None, description="Public HTTP(S) URL of the page to score")
    html: str | None = Field(default=None, description="Raw rendered HTML (skips the fetch)")
    include_llm: bool = Field(
        default=False,
        description=(
            "Opt in to Tier-3 LLM micro-judgments. Advisory only — the deterministic "
            "score is unaffected and always returned."
        ),
    )
    # --- Tier-1 deterministic enrichment (all opt-in; off == byte-identical output) ---
    include_priority: bool = Field(
        default=False,
        description=(
            "Attach a deterministic ROI priority + rank to each quick-win / strategic "
            "finding. Additive: absent unless requested."
        ),
    )
    include_projection: bool = Field(
        default=False,
        description=(
            "Include a score projection — points regained if the top-ranked findings "
            "were fixed, computed with the real penalty-decay scorer."
        ),
    )
    include_evidence: bool = Field(
        default=False,
        description=(
            "Surface the raw evidence snippet / selector a scanner already captured "
            "for each finding (when present)."
        ),
    )
    bucket_depth_cap: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description=(
            "Cap how many findings surface per bucket (highest-priority first), "
            "applied within the quick-wins and strategic lists independently. "
            "Off by default — every finding surfaces."
        ),
    )
    include_unbucketed: bool = Field(
        default=False,
        description=(
            "Surface findings that match neither action bucket (low/info severity, "
            "or medium-effort non-critical) in the strategic list instead of dropping "
            "them. Off by default so the response is byte-identical."
        ),
    )
    include_recommendations: bool = Field(
        default=False,
        description=(
            "Opt in to the WM recommendation-writer: weave evidence-grounded fix "
            "prose onto each actionable finding. One WM call per finding — its own "
            "explicit knob, separate from include_llm's micro-judge. Off by default "
            "(titles only == today); fail-soft (a WM outage returns titles only)."
        ),
    )
    include_grounding: bool = Field(
        default=False,
        description=(
            "Opt in to the WM claim-grounding pass (A18): fact-check each written "
            "recommendation against the page text; unsupported advice is dropped "
            "(SOFT gate). Meaningful only with include_recommendations. Off by "
            "default (byte-identical); fail-soft."
        ),
    )
    include_exec_summary: bool = Field(
        default=False,
        description=(
            "Opt in to the WM exec-summary worker (A2): a short grounded narrative "
            "plus an id-locked fix roadmap over the computed scores. Meaningful only "
            "with include_llm. Off by default (byte-identical); fail-soft."
        ),
    )
    include_critic: bool = Field(
        default=False,
        description=(
            "Opt in to the WM report-critic (A11): a holistic ship gate that re-writes "
            "only the recommendations it flags as generic/ungrounded. Meaningful only "
            "with include_recommendations. Off by default (byte-identical); fail-soft."
        ),
    )
    intent: str | None = Field(
        default=None,
        description=(
            "Optional page intent (audience / goal) used only to steer the opt-in "
            "recommendation-writer prose (include_llm). Ignored on the deterministic path."
        ),
    )
    sources: list[str] | None = Field(
        default=None,
        description=(
            "Optional supporting source texts to ground recommendations against, in "
            "addition to the scanned page (used only with include_grounding). Each "
            "string is a source excerpt; capped/clipped downstream."
        ),
    )


class Recommendation(BaseModel):
    """Fix-prose written for a single finding by the audit recommendation-writer.

    Additive and presence-gated: attached to a finding ref only when the request
    opts into ``include_llm`` and the writer produced an evidence-cited fix.
    """

    fix_prose: str = Field(..., description="Specific, evidence-grounded fix for the finding")
    rationale: str = Field(default="", description="Why the fix matters for this severity")
    example: str = Field(default="", description="A concrete before/after or replacement snippet")
    effort: str = Field(default="", description="Estimated remediation effort: low/medium/high")


class ScoreProjection(BaseModel):
    """Projected score lift from fixing the top-ranked findings (opt-in).

    ``baseline`` is the current overall score; ``projected`` is the score if every
    ``removed`` check_id were fixed, re-computed through the real exponential-decay
    scorer (so it is exact, not a linear sum); ``delta`` is ``projected - baseline``.
    """

    baseline: float = Field(..., description="Current overall 0-100 score")
    projected: float = Field(..., description="Overall score if the top-K findings were fixed")
    delta: float = Field(..., description="Points regained (projected - baseline)")
    removed: list[str] = Field(
        default_factory=list, description="check_ids modelled as fixed for this projection"
    )


class GroundingResult(BaseModel):
    """One recommendation's claim-grounding verdict (A18), surfaced at the top level.

    Additive and presence-gated: the ``grounding`` list appears only when the
    request opts into ``include_grounding`` and at least one recommendation was
    checked. An unsupported verdict means the matching ``recommendation`` block was
    dropped from its finding (SOFT gate).
    """

    check_id: str = Field(..., description="The finding this verdict is about")
    supported: bool = Field(..., description="Whether the fix premise is grounded in the page")
    evidence_ref: str = Field(
        default="", description="Short supporting quote, or '' if unsupported"
    )


class ExecSummary(BaseModel):
    """Executive summary over the computed scores (A2), surfaced at the top level.

    Additive and presence-gated: appears only when the request opts into
    ``include_exec_summary`` and a summary was produced. ``roadmap`` is id-locked —
    every check_id it names is a real finding.
    """

    summary: str = Field(..., description="1-3 sentence overall narrative, grounded in the scores")
    so_what: str = Field(default="", description="One sentence on the business consequence")
    roadmap: list[str] = Field(
        default_factory=list, description="Ordered check_ids to fix first (id-locked to findings)"
    )


class CritiqueResult(BaseModel):
    """Holistic report-critic verdict (A11), surfaced at the top level.

    Additive and presence-gated: appears only when the request opts into
    ``include_critic`` and a critique was produced. ``rerun_stages`` (the flagged
    ids that were re-written) is empty when the report shipped as-is.
    """

    score: float = Field(..., description="Holistic 0-100 report-quality score")
    generic_check_ids: list[str] = Field(
        default_factory=list, description="check_ids whose advice reads generic"
    )
    ungrounded_check_ids: list[str] = Field(
        default_factory=list, description="check_ids whose advice is not clearly grounded"
    )
    rerun_stages: list[str] = Field(
        default_factory=list,
        description="Flagged check_ids re-written by the writer (empty if shipped)",
    )


# ---------------------------------------------------------------------------
# Audit-report contract (design 2026-10-17 §3.2 / §4 X-G1..X-G4). All additive:
# every scorecard response now carries ``ai``, ``findings``, ``checks`` and
# ``facts`` alongside the historical fields.
# ---------------------------------------------------------------------------

AiLayerLiteral = Literal[
    "recommendations",
    "grounding",
    "exec_summary",
    "critic",
    "micro_judge",
    "seo_intent",
    "seo_eeat",
    "content_gap",
]


class AiLayerStatus(BaseModel):
    """One WM-backed layer's outcome on this request."""

    layer: AiLayerLiteral = Field(..., description="Which AI layer this row is about")
    worker: str = Field(..., description="The worker_management worker name the layer calls")
    status: Literal["ok", "failed", "skipped", "empty"] = Field(
        ...,
        description=(
            "ok = produced output; failed = WM/transport/parse error (fail-soft); "
            "skipped = a precondition was not met; empty = the call succeeded but "
            "produced nothing usable"
        ),
    )
    reason: str | None = Field(
        default=None, description="Short human-readable reason, e.g. 'WM 401 Unauthorized'"
    )


class AiAnnotation(BaseModel):
    """One structured AI observation (e.g. SEO search intent, E-E-A-T depth, gaps).

    Replaces the old trick of smuggling AI output in as info-severity findings,
    which the action buckets then dropped (design F8). ``data`` carries the
    layer-specific structured payload.
    """

    layer: AiLayerLiteral
    kind: str = Field(..., description="Stable annotation kind, e.g. 'search_intent'")
    title: str = Field(default="", description="Short human label")
    summary: str = Field(default="", description="One-paragraph rendering of the observation")
    data: dict[str, object] = Field(default_factory=dict, description="Structured payload")


class AiStatus(BaseModel):
    """Machine-readable AI-analysis status — the report reads this, never infers it.

    ``status``: ``not_requested`` (no AI layer asked for), ``complete`` (every
    layer ok or skipped by design), ``partial`` (some layers did not deliver),
    ``unavailable`` (no layer delivered). A request for AI that recorded no layer
    is ``unavailable``, never ``complete``.
    """

    requested: bool = Field(..., description="Whether the request opted into any AI layer")
    status: Literal["not_requested", "complete", "partial", "unavailable"]
    reason: str | None = Field(
        default=None,
        description="One line naming each degraded layer and why (null when healthy)",
    )
    layers: list[AiLayerStatus] = Field(default_factory=list)
    annotations: list[AiAnnotation] = Field(
        default_factory=list,
        description="Structured AI observations not tied to a single finding (SEO analyzers)",
    )


class AuditFindingBase(BaseModel):
    """A full finding (every severity, never bucket-dropped) — design X-G2.

    Each domain subclasses this to add its bucket key (``group`` / ``section`` /
    ``dimension``), mirroring the lightweight ``*FindingRef`` models. ``rank`` and
    ``priority`` are deterministic and computed over **all** findings (not only
    the actionable quick-win/strategic subset), so they are always present.
    """

    check_id: str
    title: str
    severity: str
    description: str = Field(default="", description="The scanner's own explanation")
    fix: str = Field(default="", description="The scanner's deterministic fix text")
    effort: str = Field(default="", description="Remediation effort: low/medium/high")
    evidence: str | None = Field(default=None, description="Raw evidence snippet, if captured")
    selector: str | None = Field(default=None, description="CSS selector, if known")
    rank: int = Field(..., description="1-based deterministic rank over all findings")
    priority: float = Field(..., description="Deterministic ROI priority score")
    action_bucket: Literal["quick_win", "strategic", "none"] = Field(
        default="none",
        description="Which action list this finding appears in (none = neither list)",
    )
    recommendation: Recommendation | None = Field(
        default=None,
        description="AI fix prose, when include_recommendations wrote one that survived grounding",
    )


class AuditCheckBase(BaseModel):
    """One check that the scanner knows about, with its outcome — design X-G1.

    Each domain subclasses this to add its bucket key. ``not_run`` is distinct
    from ``pass``: a check whose precondition was missing (no URL for a live
    fetch, no form on the page, crawl not requested) never reads as clean.
    """

    check_id: str
    title: str = Field(default="", description="Human label for the check")
    status: Literal["pass", "fail", "not_run"]
    severity: str | None = Field(default=None, description="Finding severity when status=fail")
    measured: str | None = Field(default=None, description="The measured value, when known")
    target: str | None = Field(default=None, description="The target the check compares against")
    reason: str | None = Field(default=None, description="Why the check did not run")
