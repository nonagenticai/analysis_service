"""Tier-3 audit report-critic (A11) — a holistic ship gate over the assembled report.

The recommendation-writer and exec-summary each judge one thing at a time. This
module adds the opt-in ``audit-report-critic`` WM worker: it reads the *assembled*
report text (scores + ranked titles + surviving fix prose + exec summary — never
raw HTML) and returns a holistic quality score plus the ``check_id``s whose advice
reads generic or ungrounded. When the score is below the ship threshold it emits
``rerun_stages`` — the flagged ids — and the caller re-runs the recommendation
writer **once** over only those ids, closing the evaluator-optimizer loop in
process. The critic itself is single-shot and never re-critiques, so the loop is
finite by construction.

Mirrors ``domains/cro/llm_judge.py``: tiny prompt + strict ``output_schema`` +
``usage_metrics`` through the shared :class:`WMClient`. Advisory + fail-soft: the
caller wraps :func:`maybe_critique_report`, so a WM outage yields the scorecard
unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.scorecards.ai_status import AiStatusCollector

logger = logging.getLogger(__name__)

WORKER_NAME = "audit-report-critic"

# The critic is single-shot — it NEVER re-critiques (no loop). The only follow-on
# work is one writer re-run over the flagged ids, so the loop is finite by
# construction.

# Ship threshold on the critic's 0-100 holistic score. At or above it the report
# ships as-is (empty rerun_stages); below it the flagged ids are re-written once.
_SHIP_THRESHOLD = 70.0

# Only the top-ranked findings' titles/prose enter the (bounded) assembled report
# text — enough for a holistic read without shipping the whole finding list.
_TOP_K = 12


class CritiqueResult(TypedDict):
    """The holistic critique block merged onto the scorecard response (top level)."""

    score: float
    generic_check_ids: list[str]
    ungrounded_check_ids: list[str]
    rerun_stages: list[str]


CRITIC_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "generic_check_ids": {"type": "array", "items": {"type": "string"}},
        "ungrounded_check_ids": {"type": "array", "items": {"type": "string"}},
        "rerun_stages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score"],
}


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from an LLM response (mirror recommendation_writer)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _coerce_to_dict(raw: object) -> dict[str, object]:
    """Normalize WM ``output`` (dict for structured-output, str for text) to a dict."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        parsed = json.loads(_strip_markdown_fences(raw))
        if isinstance(parsed, dict):
            return {str(k): v for k, v in parsed.items()}
        raise ValueError(f"critic JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _lock_ids(raw: object, valid_check_ids: set[str]) -> list[str]:
    """Keep only real finding ids (id-lock), de-duplicated, order preserved."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        cid = str(item).strip()
        if cid and cid in valid_check_ids and cid not in out:
            out.append(cid)
    return out


def _normalize_critique(
    data: Mapping[str, object], *, valid_check_ids: set[str]
) -> CritiqueResult | None:
    """Coerce raw WM output to a :class:`CritiqueResult`, id-locking every id list.

    Returns ``None`` when ``score`` is missing/garbage — a critique with no score
    cannot gate anything. ``rerun_stages`` is forced empty when the report ships
    (score >= threshold), so a model that scores high but still lists rerun ids
    cannot trigger a needless re-write.
    """
    raw_score = data.get("score")
    if not isinstance(raw_score, (int, float, str)):
        return None
    try:
        score = float(raw_score)
    except TypeError, ValueError:
        return None
    score = max(0.0, min(100.0, score))
    generic = _lock_ids(data.get("generic_check_ids"), valid_check_ids)
    ungrounded = _lock_ids(data.get("ungrounded_check_ids"), valid_check_ids)
    rerun = _lock_ids(data.get("rerun_stages"), valid_check_ids) if score < _SHIP_THRESHOLD else []
    return {
        "score": score,
        "generic_check_ids": generic,
        "ungrounded_check_ids": ungrounded,
        "rerun_stages": rerun,
    }


def assemble_report_text(result: Mapping[str, object], refs: Sequence[Mapping[str, object]]) -> str:
    """Assemble the report text the critic reads — scores + titles + prose + summary.

    Deliberately the *assembled text*, never raw HTML (the critic reasons over the
    written report, not the DOM). Bounded to the top-K findings.
    """
    lines = [
        f"OVERALL SCORE: {result.get('overall_score', '')} ({result.get('grade', '')})",
    ]
    exec_summary = result.get("exec_summary")
    if isinstance(exec_summary, dict):
        es = cast("dict[str, object]", exec_summary)
        lines.append(f"EXEC SUMMARY: {es.get('summary', '')}")
        if es.get("so_what"):
            lines.append(f"SO WHAT: {es.get('so_what')}")
    lines.append("\nFINDINGS:")
    for ref in refs[:_TOP_K]:
        line = f"- {ref.get('check_id', '')} — {ref.get('title', '')}"
        rec = ref.get("recommendation")
        if isinstance(rec, dict):
            fix_prose = cast("dict[str, object]", rec).get("fix_prose")
            if fix_prose:
                line += f"\n    fix: {fix_prose}"
        lines.append(line)
    return "\n".join(lines)


def build_prompt(report_text: str, scores: Mapping[str, object]) -> str:
    """Render the tiny critic prompt from the assembled report text."""
    return (
        "Do a final holistic quality pass on this audit report.\n"
        f"- overall_score: {scores.get('overall_score', '')}\n\n"
        f"REPORT:\n{report_text}"
    )


async def critique_report(
    report_text: str,
    scores: Mapping[str, object],
    findings: Sequence[Mapping[str, object]],
    *,
    wm: WMClient,
    model_override: str | None = None,
) -> tuple[CritiqueResult | None, dict[str, int]]:
    """Critique the assembled report via a single WM call (id-locked verdicts).

    Raises whatever :meth:`WMClient.message` raises — the caller fail-softs.
    """
    valid_check_ids = {str(f.get("check_id", "")) for f in findings if f.get("check_id")}
    response = await wm.message(
        worker_name=WORKER_NAME,
        message=build_prompt(report_text, scores),
        output_schema=CRITIC_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    usage = _usage_dict(response.usage_metrics)
    critique = _normalize_critique(
        _coerce_to_dict(response.output), valid_check_ids=valid_check_ids
    )
    return critique, usage


async def maybe_critique_report(
    result: dict,
    *,
    refs: Sequence[dict],
    findings: Sequence[Mapping[str, object]],
    scores: Mapping[str, object],
    include_critic: bool,
    wm_client: WMClient | None,
    intent: str | None = None,
    ai: AiStatusCollector | None = None,
) -> None:
    """Fail-soft entry point the scorecard services call unconditionally.

    A no-op when ``include_critic`` is off (byte-identical). Otherwise it critiques
    the assembled report and, when the critique flags ids for re-write, re-runs the
    recommendation writer **once** over only those flagged refs that already carry a
    recommendation (never adds prose where there was none — so a critic run cannot
    change the include_recommendations-off behaviour). Bounded:
    ``min(|rerun_stages|, 25) x 2`` WM calls, no recursion. Swallows any WM error so
    a critic failure never breaks the deterministic score, and records the outcome
    into ``ai`` as layer ``critic`` (``failed`` / ``empty`` / ``ok``).
    """
    if not include_critic:
        return
    try:
        from src.llm_client import get_wm_client

        wm = wm_client or get_wm_client()
        report_text = assemble_report_text(result, refs)
        critique, _ = await critique_report(report_text, scores, findings, wm=wm)
    except Exception as exc:
        logger.warning(
            "audit report-critic failed; returning scorecard without a critique",
            exc_info=True,
            extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
        )
        if ai is not None:
            ai.failed("critic", WORKER_NAME, exc)
        return
    if critique is None:
        if ai is not None:
            ai.empty("critic", WORKER_NAME, "the model returned no usable critique")
        return
    # Surface the critique BEFORE the bounded re-run: the critique is already
    # computed and valid, so a failure in the (best-effort) re-write must not
    # discard it. The re-run only refines flagged prose; the verdict stands.
    result["critique"] = critique
    if ai is not None:
        ai.ok("critic", WORKER_NAME)
    await _rerun_flagged(critique, refs, findings, wm=wm, intent=intent, ai=ai)


async def _rerun_flagged(
    critique: CritiqueResult,
    refs: Sequence[dict],
    findings: Sequence[Mapping[str, object]],
    *,
    wm: WMClient,
    intent: str | None,
    ai: AiStatusCollector | None,
) -> None:
    """Re-write only the flagged refs that already carry prose (best-effort)."""
    from src.domains.scorecards.recommendation_writer import (
        WriteStats,
        attach_recommendations,
    )

    rerun_ids = set(critique["rerun_stages"])
    flagged = [
        r
        for r in refs
        if str(r.get("check_id", "")) in rerun_ids and isinstance(r.get("recommendation"), Mapping)
    ]
    if not flagged:
        return
    stats = WriteStats()
    try:
        await attach_recommendations(flagged, findings, wm=wm, intent=intent, stats=stats)
    except Exception as exc:
        logger.warning(
            "audit report-critic re-write failed; keeping the original prose",
            exc_info=True,
            extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
        )
        stats.failures.append(str(exc) or type(exc).__name__)
    if stats.failures and ai is not None:
        ai.ok(
            "critic",
            WORKER_NAME,
            f"re-write of {len(stats.failures)} flagged recommendation(s) failed; "
            "original prose kept",
        )


def _usage_dict(usage: Mapping[str, object] | None) -> dict[str, int]:
    """Coerce WM ``usage_metrics`` to an int token dict (missing keys -> 0)."""
    out = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not usage:
        return out
    for key in out:
        val = usage.get(key, 0)
        if isinstance(val, (int, float, str)):
            try:
                out[key] = int(val)
            except TypeError, ValueError:
                out[key] = 0
    return out
