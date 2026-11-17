"""Shared finding rollups for the synchronous scorecard domains.

The action-bucket split (quick-win vs strategic) and the per-bucket summary
rollup (score / weight / count / worst-severity) were byte-identical across the
cro / seo_audit / marketing services apart from the bucket key name
(``section`` / ``group`` / ``dimension``) and its resolver. Factored out here,
parameterised on ``bucket_of`` + the key name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.domains.scorecards.priority import rank_findings

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

BucketResolver = Callable[[str | None], str | None]


def worse_severity(current: str, candidate: str) -> str:
    """Return whichever of two severities ranks worse under ``SEVERITY_ORDER``.

    Unknown labels rank as ``info`` (4). The per-key worst-severity rollups
    (``summarize_buckets`` here, ``artifact_score._summarize``) share this one
    comparison instead of hand-inlining the ``SEVERITY_ORDER.get`` pair.
    """
    return (
        candidate if SEVERITY_ORDER.get(candidate, 4) < SEVERITY_ORDER.get(current, 4) else current
    )


def action_buckets(
    findings: list[dict],
    *,
    bucket_of: BucketResolver,
    bucket_key: str,
    drop_unbucketed: bool = False,
    include_unbucketed: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Split findings into quick-wins (high-impact/low-effort) and strategic.

    ``bucket_key`` names the per-finding bucket field (``section``/``group``/
    ``dimension``). ``drop_unbucketed=True`` skips findings whose ``bucket_of``
    is ``None`` (marketing's composite pulls in cro-/seo-/mkt- check_ids, some of
    which map to no dimension); the cro/seo single-engine cases keep them with an
    empty bucket, matching the original per-domain behaviour exactly.

    The severity/effort split is intentionally non-exhaustive: a finding that is
    neither critical/high-effort nor a low-effort high/medium (e.g. a low- or
    info-severity item, or a medium-effort non-critical one) matches neither
    branch. By default such findings are **dropped from both lists** (the
    historical behaviour, kept byte-identical). ``include_unbucketed=True`` opts
    into surfacing them in ``strategic`` instead of silently dropping them, so the
    downstream recommendation-writer can still advise on them. The count of
    would-be-dropped findings is logged either way so the drop is observable.
    """
    quick_wins: list[dict] = []
    strategic: list[dict] = []
    dropped = 0
    for f in findings:
        bucket = bucket_of(f.get("check_id"))
        if drop_unbucketed and bucket is None:
            continue
        sev = f.get("severity", "info")
        effort = f.get("remediation_effort", "low")
        ref = {
            "check_id": f.get("check_id", ""),
            bucket_key: bucket or "",
            "title": f.get("title", ""),
            "severity": sev,
        }
        if sev == "critical" or effort == "high":
            strategic.append(ref)
        elif sev in {"high", "medium"} and effort == "low":
            quick_wins.append(ref)
        elif include_unbucketed:
            strategic.append(ref)
        else:
            dropped += 1
    if dropped:
        logger.debug(
            "action_buckets: %d finding(s) matched no action bucket (surface via "
            "include_unbucketed)",
            dropped,
        )
    return quick_wins, strategic


def summarize_buckets(
    findings: list[dict],
    bucket_scores: dict[str, float],
    *,
    weights: dict[str, float],
    bucket_of: BucketResolver,
    bucket_key: str,
) -> list[dict[str, Any]]:
    """One row per weighted bucket: score, weight, finding count, worst severity."""
    counts: dict[str, int] = {b: 0 for b in weights}
    top: dict[str, str] = {b: "info" for b in weights}
    for f in findings:
        bucket = bucket_of(f.get("check_id"))
        if bucket not in counts:
            continue
        counts[bucket] += 1
        top[bucket] = worse_severity(top[bucket], f.get("severity", "info"))
    return [
        {
            bucket_key: bucket,
            "score": bucket_scores.get(bucket, 100.0),
            "weight": weight,
            "count": counts[bucket],
            "top_severity": top[bucket] if counts[bucket] else "info",
        }
        for bucket, weight in weights.items()
    ]


# ---------------------------------------------------------------------------
# Full findings + check inventory (design 2026-10-17 X-G1 / X-G2)
# ---------------------------------------------------------------------------


def build_findings(
    findings: Sequence[Mapping[str, Any]],
    *,
    bucket_of: BucketResolver,
    bucket_key: str,
    weights: Mapping[str, float],
    severity_weights: Mapping[str, int],
    quick_wins: Sequence[Mapping[str, Any]] = (),
    strategic: Sequence[Mapping[str, Any]] = (),
    keep: Callable[[str | None], bool] | None = None,
) -> list[dict[str, object]]:
    """Every finding as a full row, ranked deterministically — never bucket-dropped.

    Unlike :func:`action_buckets` (which keeps only refs and drops low/info and
    medium-effort non-critical findings), this keeps **all** severities with the
    scanner's own ``description`` and deterministic ``fix``. ``rank``/``priority``
    come from :func:`priority.rank_findings` over this whole set, so they are
    stable and present whatever the request flags. ``action_bucket`` says which
    action list (if any) the finding also appears in, and an AI ``recommendation``
    already woven onto a ref is copied across by ``check_id``.

    ``keep`` filters by bucket (marketing keeps only its own dimensions so the
    scorecard never re-lists cro-/seo- findings). One row per ``check_id``
    (first occurrence wins), matching the refs' de-duplication.
    """
    in_quick = {str(r.get("check_id", "")) for r in quick_wins}
    in_strategic = {str(r.get("check_id", "")) for r in strategic}
    rec_by_check: dict[str, dict[str, object]] = {}
    for ref in (*quick_wins, *strategic):
        rec = ref.get("recommendation")
        cid = str(ref.get("check_id", ""))
        if isinstance(rec, Mapping) and cid and cid not in rec_by_check:
            rec_by_check[cid] = dict(rec)

    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for f in findings:
        cid = str(f.get("check_id") or "")
        if not cid or cid in seen:
            continue
        bucket = bucket_of(cid)
        if keep is not None and not keep(bucket):
            continue
        seen.add(cid)
        unique.append(f)

    rows: list[dict[str, object]] = []
    for rank, f, priority in rank_findings(
        unique, weights=weights, bucket_of=bucket_of, severity_weights=severity_weights
    ):
        cid = str(f.get("check_id") or "")
        row: dict[str, object] = {
            "check_id": cid,
            bucket_key: bucket_of(cid) or "",
            "title": str(f.get("title") or ""),
            "severity": str(f.get("severity") or "info"),
            "description": str(f.get("description") or ""),
            "fix": str(f.get("recommendation") or ""),
            "effort": str(f.get("remediation_effort") or ""),
            "evidence": f.get("evidence"),
            "selector": f.get("selector"),
            "rank": rank,
            "priority": priority,
            "action_bucket": (
                "quick_win" if cid in in_quick else "strategic" if cid in in_strategic else "none"
            ),
        }
        if cid in rec_by_check:
            row["recommendation"] = rec_by_check[cid]
        rows.append(row)
    return rows


@dataclass(frozen=True)
class CheckSpec:
    """One entry of a scanner's static check catalog (design X-G1).

    ``requires`` names a capability the check needs beyond the page HTML —
    ``"url"`` (a live fetch: robots.txt, sitemap, llms.txt), ``"crawl"``
    (cross-page linking), ``"llm"``, ``"competitors"``, or a scanner-specific
    precondition such as ``"form"``. A check whose requirement is not in the
    request's ``available`` set is reported ``not_run``, never ``pass``.
    """

    check_id: str
    bucket: str
    title: str = ""
    requires: str | None = None
    target: str | None = None


def build_checks(
    catalog: Iterable[CheckSpec],
    findings: Sequence[Mapping[str, Any]],
    *,
    bucket_key: str,
    available: Iterable[str] = (),
    not_run_reasons: Mapping[str, str] | None = None,
    not_run_buckets: Mapping[str, str] | None = None,
    measured: Mapping[str, str] | None = None,
    bucket_of: BucketResolver | None = None,
    keep: Callable[[str | None], bool] | None = None,
) -> list[dict[str, object]]:
    """The check inventory: every catalogued check with pass / fail / not_run.

    * ``fail`` — a finding with that ``check_id`` is present (severity copied).
    * ``not_run`` — the check's bucket is in ``not_run_buckets`` (e.g. a crawl
      that timed out), or its ``requires`` capability is not ``available``. The
      reason comes from ``not_run_buckets`` / ``not_run_reasons[requires]``.
    * ``pass`` — otherwise.

    A failing ``check_id`` that is NOT in the catalog (dynamic ids such as
    ``seo-schema-<type>-incomplete``) is appended as a ``fail`` row (bucket via
    ``bucket_of``) so the inventory can never under-report a failure.
    """
    have = set(available)
    reasons = dict(not_run_reasons or {})
    bucket_reasons = dict(not_run_buckets or {})
    values = dict(measured or {})
    failing: dict[str, Mapping[str, Any]] = {}
    for f in findings:
        cid = str(f.get("check_id") or "")
        if cid and cid not in failing:
            failing[cid] = f

    rows: list[dict[str, object]] = []
    catalogued: set[str] = set()
    for spec in catalog:
        if spec.check_id in catalogued:
            continue
        catalogued.add(spec.check_id)
        row: dict[str, object] = {
            "check_id": spec.check_id,
            bucket_key: spec.bucket,
            "title": spec.title,
            "target": spec.target,
            "measured": values.get(spec.check_id),
        }
        row.update(_check_outcome(spec, failing.get(spec.check_id), have, reasons, bucket_reasons))
        if not row["title"] and (hit := failing.get(spec.check_id)) is not None:
            row["title"] = str(hit.get("title") or "")
        rows.append(row)

    for cid, f in failing.items():
        if cid in catalogued:
            continue
        bucket = bucket_of(cid) if bucket_of is not None else None
        if keep is not None and not keep(bucket):
            continue
        rows.append(
            {
                "check_id": cid,
                bucket_key: bucket or "",
                "title": str(f.get("title") or ""),
                "status": "fail",
                "severity": str(f.get("severity") or "info"),
                "target": None,
                "measured": values.get(cid),
            }
        )
    return rows


def _check_outcome(
    spec: CheckSpec,
    hit: Mapping[str, Any] | None,
    available: set[str],
    reasons: Mapping[str, str],
    bucket_reasons: Mapping[str, str],
) -> dict[str, object]:
    """``status`` (+ ``severity`` / ``reason``) for one catalogued check."""
    if hit is not None:
        return {"status": "fail", "severity": str(hit.get("severity") or "info")}
    if spec.bucket in bucket_reasons:
        return {"status": "not_run", "reason": bucket_reasons[spec.bucket]}
    if spec.requires is not None and spec.requires not in available:
        return {
            "status": "not_run",
            "reason": reasons.get(spec.requires, f"requires {spec.requires}"),
        }
    return {"status": "pass"}
