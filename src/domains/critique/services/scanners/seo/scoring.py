"""SEO audit composite scoring — per-group penalty decay + weighted mean.

The SEO audit reuses the production exponential-decay penalty model
(:func:`compute_category_score`) but reports a *grouped* breakdown so a health
score decomposes into on-page / technical / schema / content / keyword / snippet
(more groups land as later phases add checks). Every group is scored with the
same decay over just that group's findings, then combined as a weighted mean —
identical in spirit to :mod:`cro.scoring`.

All audit findings are ``category == "seo"``; the *group* is derived from the
``check_id``. New audit checks use the ``seo-<group>-<check>`` namespace so the
group is the second token; the pre-existing 19 base-SEO checks predate that
convention and are mapped explicitly in :data:`LEGACY_CHECK_GROUPS`.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import NotRequired, TypedDict

from src.domains.critique.services.scanners.composite import compute_scorecard


class SeoAuditScore(TypedDict):
    """The SEO audit composite score payload.

    ``unmeasured`` / ``coverage`` are present ONLY when a group's scanner did not
    run, so the default payload stays byte-identical for every existing caller.
    """

    overall_score: float
    grade: str
    group_scores: dict[str, float | None]
    unmeasured: NotRequired[list[str]]
    coverage: NotRequired[float]


# Group weights (sum = 1.0), weighted toward the highest-leverage ranking signals
# (on-page + technical). The full audit weights all nine groups.
GROUP_WEIGHTS: dict[str, float] = {
    "on_page": 0.22,
    "technical": 0.18,
    "content": 0.12,
    "keyword": 0.10,
    "schema": 0.10,
    "eeat": 0.10,
    "internal_linking": 0.07,
    "snippet": 0.06,
    "geo": 0.05,
}

# The 19 pre-existing base-SEO checks (namespace ``seo-<slug>``), mapped to audit
# groups. Consulted before the ``seo-<group>-<check>`` prefix convention.
LEGACY_CHECK_GROUPS: dict[str, str] = {
    # on-page content signals
    "seo-missing-title": "on_page",
    "seo-title-too-short": "on_page",
    "seo-title-too-long": "on_page",
    "seo-meta-description-missing": "on_page",
    "seo-meta-description-too-short": "on_page",
    "seo-meta-description-too-long": "on_page",
    "seo-missing-h1": "on_page",
    "seo-multiple-h1": "on_page",
    "seo-heading-hierarchy-skip": "on_page",
    "seo-images-missing-alt": "on_page",
    "seo-keyword-alignment": "on_page",
    "seo-duplicate-meta": "on_page",
    # technical / crawlability / mobile / social-meta
    "seo-canonical-missing": "technical",
    "seo-canonical-protocol-mismatch": "technical",
    "seo-internal-nofollow": "technical",
    "seo-url-underscores": "technical",
    "seo-url-query-params": "technical",
    "seo-url-depth": "technical",
    "seo-meta-robots-noindex": "technical",
    "seo-meta-robots-nofollow": "technical",
    "seo-favicon-missing": "technical",
    "seo-hreflang-no-default": "technical",
    "seo-hreflang-invalid": "technical",
    "seo-hreflang-duplicate": "technical",
    "seo-robots-txt-missing": "technical",
    "seo-sitemap-missing": "technical",
    # "we could not check" variants: same audit group as the check they stand in
    # for, so an unreachable origin does not silently reassign them to the
    # default bucket in the customer-facing group breakdown.
    "seo-robots-txt-errored": "technical",
    "seo-sitemap-errored": "technical",
    "seo-viewport-missing": "technical",
    "seo-mobile-font-size": "technical",
    "seo-og-tags-missing": "technical",
    "seo-twitter-card-missing": "technical",
    "seo-twitter-meta-incomplete": "technical",
    # structured data
    "seo-structured-data-missing": "schema",
    "seo-structured-data-no-type": "schema",
    "seo-structured-data-no-name": "schema",
    "seo-structured-data-invalid": "schema",
    "seo-breadcrumb-missing": "schema",
}

_DEFAULT_GROUP = "on_page"

# Prefix token -> group, where the ``seo-<token>-<check>`` token differs from the
# group name (kept short in check_ids for readability).
_PREFIX_ALIASES: dict[str, str] = {"linking": "internal_linking"}


def group_of(check_id: str | None) -> str:
    """Map an SEO ``check_id`` to its audit group.

    Legacy map wins; then the ``seo-<group>-<check>`` prefix (with aliases); else a
    safe default so a stray/unknown check_id still contributes to the composite.
    """
    if not check_id:
        return _DEFAULT_GROUP
    if check_id in LEGACY_CHECK_GROUPS:
        return LEGACY_CHECK_GROUPS[check_id]
    parts = check_id.split("-")
    if len(parts) >= 3 and parts[0] == "seo":
        group = _PREFIX_ALIASES.get(parts[1], parts[1])
        if group in GROUP_WEIGHTS:
            return group
    return _DEFAULT_GROUP


def compute_seo_audit_score(
    findings: list[dict], *, unmeasured: Collection[str] = ()
) -> SeoAuditScore:
    """Return ``{overall_score, grade, group_scores}`` for SEO findings.

    Only ``category == "seo"`` findings contribute. Each group is scored with the
    production exponential-decay model over just that group's findings (100.0 for
    an empty group), then combined as the weighted mean of group scores.

    ``unmeasured`` names groups whose scanner never ran — an empty finding slice
    there means "unknown", not "clean". Those groups score ``None`` and are
    excluded from the weighted mean, and the result carries ``unmeasured`` +
    ``coverage``. Empty by default ⇒ unchanged for every existing caller.
    """
    raw = compute_scorecard(
        findings,
        category="seo",
        weights=GROUP_WEIGHTS,
        bucket_of=group_of,
        breakdown_key="group_scores",
        unmeasured=unmeasured,
    )
    # Rebuilt key-by-key (same insertion order, same values) rather than cast, so
    # the declared shape is checked rather than asserted.
    score: SeoAuditScore = {
        "overall_score": raw["overall_score"],
        "grade": raw["grade"],
        "group_scores": raw["group_scores"],
    }
    if "unmeasured" in raw:
        score["unmeasured"] = raw["unmeasured"]
        score["coverage"] = raw["coverage"]
    return score
