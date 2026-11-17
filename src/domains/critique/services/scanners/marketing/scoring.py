"""Marketing scorecard composite — six weighted go-to-market dimensions.

The composite reuses the production per-category exponential-decay model
(:func:`compute_category_score`) and reconciles it with the reference marketing
rubric's weighted dimensions, exactly as ``cro.scoring`` does for CRO
sub-sections. Three dimensions are sourced from *other* engines (reused, not
recomputed here):

* ``content``    = mean of the CRO ``hero`` + ``valueprop`` + ``cta`` sub-scores.
* ``conversion`` = the CRO overall score.
* ``seo``        = the SEO Health Score.

The remaining three come from the marketing scanner's ``mkt-<dimension>-*``
findings, each scored with the same decay model over its own slice:

* ``competitive`` · ``brand`` · ``growth``

A dimension with no penalising evidence scores 100.
"""

from __future__ import annotations

from typing import Any

from src.domains.critique.services.report_generator import score_to_grade
from src.domains.critique.services.scanners.composite import score_buckets, weighted_overall

# Reference marketing-audit dimension weights (sum = 1.0).
DIMENSION_WEIGHTS: dict[str, float] = {
    "content": 0.25,
    "conversion": 0.20,
    "seo": 0.20,
    "competitive": 0.15,
    "brand": 0.10,
    "growth": 0.10,
}

# Dimensions whose score is computed from the marketing scanner's own findings
# (the other three are supplied by the CRO/SEO engines).
_SCANNER_DIMENSIONS: tuple[str, ...] = ("competitive", "brand", "growth")

# Which engine produces each dimension's score (design 2026-10-17 §2.3). The
# borrowed rows are reference-only on the scorecard: their findings belong to the
# CRO / SEO reports, which ``REPORT_POINTERS`` names.
DIMENSION_SOURCES: dict[str, str] = {
    "content": "cro",
    "conversion": "cro",
    "seo": "seo",
    **dict.fromkeys(_SCANNER_DIMENSIONS, "marketing"),
}
REPORT_POINTERS: dict[str, str | None] = {
    "cro": "cro_audit",
    "seo": "seo_audit",
    "marketing": None,
}


def _mkt_dim_of(check_id: str | None) -> str | None:
    """Extract the dimension token from a ``mkt-<dimension>-<check>`` check_id."""
    if not check_id or not check_id.startswith("mkt-"):
        return None
    parts = check_id.split("-")
    return parts[1] if len(parts) >= 3 else None


def dimension_of(check_id: str | None) -> str | None:
    """Map any finding's check_id to the single scorecard dimension it belongs to.

    Used for the per-dimension finding rollup and action buckets. Content is the
    hero/value-prop slice of CRO; every other CRO finding rolls up to conversion.
    """
    if not check_id:
        return None
    mkt = _mkt_dim_of(check_id)
    if mkt is not None:
        return mkt
    if check_id.startswith(("cro-hero-", "cro-valueprop-")):
        return "content"
    if check_id.startswith("cro-"):
        return "conversion"
    if check_id.startswith("seo-"):
        return "seo"
    return None


def compute_marketing_score(
    *,
    cro_overall: float,
    seo_overall: float,
    cro_subsection_scores: dict[str, float],
    marketing_findings: list[dict],
) -> dict[str, Any]:
    """Return ``{overall_score, grade, dimension_scores}`` for the marketing scorecard.

    ``cro_overall`` / ``seo_overall`` are the composite scores from the CRO and
    SEO engines; ``cro_subsection_scores`` supplies the hero/value-prop content
    proxy; ``marketing_findings`` are the ``category == "marketing"`` findings
    from :class:`MarketingScanner`.
    """
    # Content & messaging ≈ the reference's headline / value-prop / CTA axes,
    # proxied by the CRO hero + value-prop + cta sub-section scores.
    _content_sections = ("hero", "valueprop", "cta")
    content = round(
        sum(cro_subsection_scores.get(s, 100.0) for s in _content_sections)
        / len(_content_sections),
        1,
    )

    # The three scanner-sourced dimensions share the composite kernel; the other
    # three are borrowed from the CRO/SEO engines and assembled here.
    scanner_weights = {dim: DIMENSION_WEIGHTS[dim] for dim in _SCANNER_DIMENSIONS}
    dimension_scores: dict[str, float] = {
        "content": content,
        "conversion": round(cro_overall, 1),
        "seo": round(seo_overall, 1),
        **score_buckets(
            marketing_findings,
            category="marketing",
            weights=scanner_weights,
            bucket_of=_mkt_dim_of,
        ),
    }

    overall = weighted_overall(dimension_scores, DIMENSION_WEIGHTS)

    return {
        "overall_score": overall,
        "grade": score_to_grade(overall),
        "dimension_scores": dimension_scores,
    }
