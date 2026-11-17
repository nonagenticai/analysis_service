"""Deterministic Tier-1 audit compute — priority/ROI rank + score-delta projection.

Pure, I/O-free functions layered on top of the existing scorecard engines
(``cro`` / ``seo_audit`` / ``marketing``). Everything here is **additive and
opt-in**: a scorecard run that requests none of the new flags never calls
:func:`enrich_refs`, so its response is byte-identical to before.

* :func:`priority_score` / :func:`rank_findings` turn the binary quick-win /
  strategic split into a total, deterministic order, computed from values the
  scanners already carry — severity, the finding's weighted bucket, and its
  remediation effort (an ROI divisor).
* :func:`project_delta` answers *"fix the top-K → how many points do we regain"*
  by re-running the **real** exponential-decay scorer with those findings
  removed. Per-finding deltas are non-additive under ``exp``, so the whole set is
  removed together and the domain scorer re-run — never a sum of singles.
* :func:`cap_depth` bounds how many findings surface per bucket.
* :func:`enrich_refs` composes the above onto the action-bucket refs a scorecard
  service already produces, attaching only the requested aspects.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# A finding's check_id → the weighted bucket it belongs to (``None`` if it maps
# to no scored bucket). Each engine supplies its own resolver + weights table.
BucketResolver = Callable[[str | None], str | None]
# A domain's top-level scorer, wrapped to return just the overall 0-100 float.
ScoreFn = Callable[[list[dict]], float]


class ProjectionResult(TypedDict):
    """The score-projection payload attached to a scorecard response."""

    baseline: float
    projected: float
    delta: float
    removed: list[str]


# Effort acts as an ROI divisor: a high-effort fix must clear a higher impact bar
# to rank above a cheap fix of the same severity.
EFFORT_DIVISOR: dict[str, float] = {"low": 1.0, "medium": 2.0, "high": 3.0}


def priority_score(
    finding: Mapping[str, Any],
    *,
    weights: Mapping[str, float],
    bucket_of: BucketResolver,
    severity_weights: Mapping[str, int],
) -> float:
    """ROI-style priority for one finding: (severity x bucket-weight) / effort.

    Every input is a value the scorecard engines already hold. Unknown severity →
    weight 0, unknown/absent bucket → weight 0.0, unknown effort → divisor 1.0.
    Rounded to 6 dp so the ordering is stable and free of float noise.
    """
    severity = str(finding.get("severity", "info"))
    check_id = finding.get("check_id")
    bucket = bucket_of(check_id)
    bucket_key = bucket or ""
    # A severity or bucket-weight miss both collapse this finding to a 0/0.0
    # factor — indistinguishable from a genuinely-trivial finding. A typo'd
    # check_id resolves to no bucket and silently ranks at the floor, so surface
    # the offending id. Logging only — the returned score is unchanged.
    if severity not in severity_weights:
        logger.warning(
            "priority_score: unknown severity %r for check_id %r (weight -> 0)",
            severity,
            check_id,
        )
    if bucket_key not in weights:
        logger.warning(
            "priority_score: unknown/missing bucket %r for check_id %r (weight -> 0.0)",
            bucket,
            check_id,
        )
    sev_w = severity_weights.get(severity, 0)
    bucket_w = weights.get(bucket_key, 0.0)
    divisor = EFFORT_DIVISOR.get(str(finding.get("remediation_effort", "low")), 1.0)
    return round(sev_w * bucket_w / divisor, 6)


def rank_findings(
    findings: Sequence[Mapping[str, Any]],
    *,
    weights: Mapping[str, float],
    bucket_of: BucketResolver,
    severity_weights: Mapping[str, int],
) -> list[tuple[int, Mapping[str, Any], float]]:
    """Rank findings by priority desc, ties broken by ``check_id`` (deterministic).

    Returns ``(rank, finding, priority)`` with ``rank`` 1..N. The ``check_id``
    secondary sort key guarantees a total, reproducible order — never a random
    tie-break — so two runs over the same findings yield identical ranks.
    """
    scored = [
        (
            priority_score(
                f, weights=weights, bucket_of=bucket_of, severity_weights=severity_weights
            ),
            f,
        )
        for f in findings
    ]
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("check_id", ""))))
    return [(i + 1, f, score) for i, (score, f) in enumerate(scored)]


def project_delta(
    score_fn: ScoreFn,
    all_findings: Sequence[dict],
    remove: Sequence[Mapping[str, Any]],
) -> ProjectionResult:
    """Points regained if every finding in ``remove`` were fixed, via the real scorer.

    ``score_fn`` is the domain's own top-level scorer (``compute_cro_score`` etc.
    wrapped to return the overall float). Because the production score is an
    exponential decay of summed severity penalties, per-finding deltas are
    **non-additive** — so the whole ``remove`` set is dropped together and the
    scorer re-run, never a sum of singles. Removal is by object identity (findings
    are unhashable dicts and ``check_id`` may repeat as ``""``), so the members of
    ``remove`` must be the same objects present in ``all_findings``.

    ``baseline`` and ``projected`` are each rounded to 1 dp and ``delta`` is
    derived from those rounded values, so the identity ``baseline + delta ==
    projected`` always holds even if a scorer returns finer-than-1dp numbers.
    """
    remove_ids = {id(f) for f in remove}
    kept = [f for f in all_findings if id(f) not in remove_ids]
    baseline = round(score_fn(list(all_findings)), 1)
    projected = round(score_fn(kept), 1)
    return {
        "baseline": baseline,
        "projected": projected,
        "delta": round(projected - baseline, 1),
        "removed": [str(f.get("check_id", "")) for f in remove],
    }


def cap_depth(refs: Sequence[dict], *, bucket_key: str, cap: int) -> list[dict]:
    """Keep at most ``cap`` refs per ``bucket_key`` value **within this list**.

    Preserves input order, so if ``refs`` are pre-sorted by rank the survivors are
    the top-``cap`` of each bucket. ``cap <= 0`` is a no-op returning the refs
    unchanged — depth capping never silently empties the response. Applied to the
    quick-wins and strategic lists independently, so a bucket that appears in both
    can surface up to ``cap`` refs in each (that is by design — they are separate
    response sections).
    """
    if cap <= 0:
        return list(refs)
    seen: dict[str, int] = {}
    out: list[dict] = []
    for ref in refs:
        bucket = str(ref.get(bucket_key, ""))
        n = seen.get(bucket, 0)
        if n < cap:
            seen[bucket] = n + 1
            out.append(ref)
    return out


def apply_enrichment(
    quick_wins: list[dict],
    strategic: list[dict],
    findings: Sequence[dict],
    *,
    weights: Mapping[str, float],
    bucket_of: BucketResolver,
    bucket_key: str,
    severity_weights: Mapping[str, int],
    score_fn: ScoreFn,
    include_priority: bool = False,
    include_projection: bool = False,
    include_evidence: bool = False,
    depth_cap: int | None = None,
) -> tuple[list[dict], list[dict], ProjectionResult | None]:
    """Guarded entry point for the scorecard services.

    When nothing is requested it returns the two ref lists **unchanged** (same
    objects) and ``projection`` ``None`` — so the caller does zero extra work and
    its response is byte-identical. Otherwise it delegates to :func:`enrich_refs`.
    Keeping the guard here lets each service call one flat statement (no added
    branch), so the shared enrichment does not push a scorer over its complexity
    budget.
    """
    if not (include_priority or include_projection or include_evidence or depth_cap):
        return quick_wins, strategic, None
    return enrich_refs(
        quick_wins,
        strategic,
        findings,
        weights=weights,
        bucket_of=bucket_of,
        bucket_key=bucket_key,
        severity_weights=severity_weights,
        score_fn=score_fn,
        include_priority=include_priority,
        include_projection=include_projection,
        include_evidence=include_evidence,
        depth_cap=depth_cap,
    )


def with_projection(
    result: dict[str, object], projection: ProjectionResult | None
) -> dict[str, object]:
    """Attach ``projection`` to ``result`` only when present (never a null key).

    An explicit ``"projection": None`` would serialise and break byte-identity for
    the default path, so the key is added only when a projection was computed.
    """
    if projection is not None:
        result["projection"] = projection
    return result


def enrich_refs(
    quick_wins: list[dict],
    strategic: list[dict],
    findings: Sequence[dict],
    *,
    weights: Mapping[str, float],
    bucket_of: BucketResolver,
    bucket_key: str,
    severity_weights: Mapping[str, int],
    score_fn: ScoreFn,
    include_priority: bool = False,
    include_evidence: bool = False,
    include_projection: bool = False,
    depth_cap: int | None = None,
    projection_top_k: int = 3,
) -> tuple[list[dict], list[dict], ProjectionResult | None]:
    """Additively enrich action-bucket refs with priority/rank/evidence + a projection.

    Opt-in and pure: with every ``include_*`` flag false and ``depth_cap`` None the
    two ref lists are returned unchanged (same objects) and ``projection`` is
    ``None``. Only requested aspects are attached; the caller (via
    :func:`with_projection`) omits an absent ``projection`` key so it never
    serialises.

    Ranking is over the union of the two ref lists' findings (the *actionable*
    set), giving a single total order shared by ``priority``/``rank`` and the
    projection's top-K.
    """
    by_check = _index_by_check(findings)
    ranked = rank_findings(
        _actionable_findings((*quick_wins, *strategic), by_check),
        weights=weights,
        bucket_of=bucket_of,
        severity_weights=severity_weights,
    )
    rank_by_id: dict[int, tuple[int, float]] = {id(f): (rank, score) for rank, f, score in ranked}

    projection: ProjectionResult | None = None
    if include_projection and ranked:
        top = [f for _, f, _ in ranked[: max(projection_top_k, 0)]]
        if top:
            projection = project_delta(score_fn, list(findings), top)

    if include_priority or include_evidence:
        _attach_aspects(
            (*quick_wins, *strategic),
            by_check,
            rank_by_id,
            include_priority=include_priority,
            include_evidence=include_evidence,
        )

    out_quick, out_strategic = quick_wins, strategic
    if depth_cap:
        out_quick = cap_depth(
            _sorted_by_rank(quick_wins, by_check, rank_by_id), bucket_key=bucket_key, cap=depth_cap
        )
        out_strategic = cap_depth(
            _sorted_by_rank(strategic, by_check, rank_by_id), bucket_key=bucket_key, cap=depth_cap
        )

    return out_quick, out_strategic, projection


def _index_by_check(findings: Sequence[dict]) -> dict[str, dict]:
    """Map check_id → first finding with it (empty check_ids are not indexable)."""
    by_check: dict[str, dict] = {}
    for f in findings:
        cid = f.get("check_id")
        if cid and cid not in by_check:
            by_check[cid] = f
    return by_check


def _actionable_findings(refs: Sequence[dict], by_check: Mapping[str, dict]) -> list[dict]:
    """The distinct findings behind ``refs`` (identity-deduped, in ref order)."""
    out: list[dict] = []
    seen: set[int] = set()
    for ref in refs:
        f = by_check.get(str(ref.get("check_id", "")))
        if f is not None and id(f) not in seen:
            seen.add(id(f))
            out.append(f)
    return out


def _attach_aspects(
    refs: Sequence[dict],
    by_check: Mapping[str, dict],
    rank_by_id: Mapping[int, tuple[int, float]],
    *,
    include_priority: bool,
    include_evidence: bool,
) -> None:
    """Mutate each ref in place with the requested priority/rank + evidence/selector."""
    for ref in refs:
        f = by_check.get(str(ref.get("check_id", "")))
        if f is None:
            continue
        if include_priority and (rank_score := rank_by_id.get(id(f))) is not None:
            ref["rank"], ref["priority"] = rank_score
        if include_evidence:
            if (ev := f.get("evidence")) is not None:
                ref["evidence"] = ev
            if (sel := f.get("selector")) is not None:
                ref["selector"] = sel


def _sorted_by_rank(
    refs: Sequence[dict],
    by_check: Mapping[str, dict],
    rank_by_id: Mapping[int, tuple[int, float]],
) -> list[dict]:
    """Order refs by their finding's computed rank (unranked refs sort last)."""

    def _key(ref: dict) -> tuple[int, str]:
        f = by_check.get(str(ref.get("check_id", "")))
        rank = rank_by_id.get(id(f), (1_000_000, 0.0))[0] if f is not None else 1_000_000
        return (rank, str(ref.get("check_id", "")))

    return sorted(refs, key=_key)
