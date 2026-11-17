"""CRO composite scoring — per-section penalty decay + weighted mean.

w2v scores every category with an exponential-decay penalty model
(:func:`compute_category_score`). The reference CRO rubric is expressed as
weighted sub-sections. We reconcile the two by applying the *same* production
decay to each CRO sub-section's findings independently, then taking the
reference-weighted mean of the sub-scores. A sub-section with no findings scores
100 (nothing wrong), exactly as the category engine treats an empty category.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from src.domains.critique.services.scanners.composite import compute_scorecard

# Sub-section weights (sum = 1.0), re-balanced toward the hardest-evidence areas
# (form friction + technical speed). Keys match the ``cro-<section>-…`` check_id
# namespace token.
SECTION_WEIGHTS: dict[str, float] = {
    "form": 0.20,
    "hero": 0.18,
    "cta": 0.15,
    "social": 0.12,
    "valueprop": 0.10,
    "objection": 0.10,
    "tech": 0.10,
    "footer": 0.05,
}


def _section_of(check_id: str | None) -> str | None:
    """Extract the sub-section token from a ``cro-<section>-<check>`` check_id."""
    if not check_id or not check_id.startswith("cro-"):
        return None
    parts = check_id.split("-")
    return parts[1] if len(parts) >= 3 else None


def compute_cro_score(findings: list[dict], *, unmeasured: Collection[str] = ()) -> dict[str, Any]:
    """Return ``{overall_score, grade, subsection_scores}`` for CRO findings.

    Only ``category == "cro"`` findings contribute. Each sub-section is scored
    with the production exponential-decay model over just that section's
    findings, so severity (evidence strength) drives the penalty consistently
    with every other category in the service.

    ``unmeasured`` names sections whose checks could not run on this page (e.g.
    ``form`` when the page has no conversion form). Each scores ``None`` and is
    dropped from the weighted mean, which is renormalised over the measured
    sections; ``unmeasured`` + ``coverage`` keys are then added. The default
    (empty) is byte-identical to the historical behaviour, so callers that do not
    pass it (marketing) are unaffected.
    """
    return compute_scorecard(
        findings,
        category="cro",
        weights=SECTION_WEIGHTS,
        bucket_of=_section_of,
        breakdown_key="subsection_scores",
        unmeasured=unmeasured,
    )
