"""Tier-3 audit exec-summary (A2) — a grounded narrative over the computed scores.

The deterministic scorecard is a pile of numbers and ranked finding titles. This
module adds the opt-in ``audit-exec-summary`` WM worker: it writes a short
executive narrative (``summary`` + ``so_what``) plus an ordered ``roadmap`` of
``check_id``s to fix first. The narrative is grounded strictly on the §3 computed
scores + the top-ranked findings + the grounding verdicts — never the raw page —
and the ``roadmap`` is **id-locked**: every id it names must be a real finding, so
the summary can never point the operator at a fix that does not exist.

Single-shot (a summary has nothing to converge on — unlike the recommendation
writer's evaluator-optimizer loop). Mirrors ``domains/cro/llm_judge.py``: tiny
prompt + strict ``output_schema`` + ``usage_metrics`` through the shared
:class:`WMClient`. Advisory + fail-soft: the caller wraps
:func:`maybe_narrate_report`, so a WM outage yields the scorecard unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.scorecards.ai_status import AiStatusCollector

logger = logging.getLogger(__name__)

WORKER_NAME = "audit-exec-summary"

# Only the top-ranked findings enter the (small) prompt — the summary is about the
# few things that matter, and refs arrive rank-sorted when the caller opted into
# priority. Also bounds the roadmap length.
_TOP_K = 8

# Prose-length clamp: a summary/so_what longer than this is truncated so a rambling
# generation can never dominate the response (folds the missing-length-clamp smell
# from recommendation_writer into the new workers from the start).
_MAX_PROSE = 600


class ExecSummary(TypedDict):
    """The executive-summary block merged onto the scorecard response (top level)."""

    summary: str
    so_what: str
    roadmap: list[str]


EXEC_SUMMARY_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "so_what": {"type": "string"},
        "roadmap": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary"],
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
        raise ValueError(f"exec-summary JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _lock_roadmap(roadmap: object, valid_check_ids: set[str]) -> list[str]:
    """Drop any roadmap id that is not a real finding (id-lock), preserving order.

    A hallucinated check_id — one the model invented or drifted to — is removed so
    the roadmap can only ever point at findings that exist. Duplicates are removed
    (first occurrence wins) and the length is bounded by :data:`_TOP_K`.
    """
    if not isinstance(roadmap, list):
        return []
    out: list[str] = []
    for item in roadmap:
        cid = str(item).strip()
        if cid and cid in valid_check_ids and cid not in out:
            out.append(cid)
        if len(out) >= _TOP_K:
            break
    return out


def _normalize_exec_summary(
    data: Mapping[str, object], *, valid_check_ids: set[str]
) -> ExecSummary | None:
    """Coerce raw WM output to an :class:`ExecSummary` with the id-lock + prose clamp.

    Returns ``None`` (drop) when ``summary`` is empty — an exec-summary with no
    summary is worthless. ``roadmap`` is id-locked to real findings and ``summary`` /
    ``so_what`` are length-clamped.
    """
    summary = str(data.get("summary", "") or "").strip()[:_MAX_PROSE]
    if not summary:
        return None
    so_what = str(data.get("so_what", "") or "").strip()[:_MAX_PROSE]
    roadmap = _lock_roadmap(data.get("roadmap"), valid_check_ids)
    return {"summary": summary, "so_what": so_what, "roadmap": roadmap}


def _grounding_line(grounding: Sequence[Mapping[str, object]] | None) -> str:
    """One-line grounding digest for the prompt (supported vs dropped counts)."""
    if not grounding:
        return "grounding: not run"
    supported = sum(1 for g in grounding if g.get("supported"))
    return f"grounding: {supported}/{len(grounding)} recommendations supported"


def build_prompt(
    scores: Mapping[str, object],
    refs: Sequence[Mapping[str, object]],
    grounding: Sequence[Mapping[str, object]] | None,
    *,
    intent: str | None,
) -> str:
    """Render the tiny exec-summary prompt from scores + top-K findings + grounding."""
    lines = [
        "Write an executive summary for this audit.",
        f"- overall_score: {scores.get('overall_score', '')}",
        f"- grade: {scores.get('grade', '')}",
    ]
    if intent:
        lines.append(f"- page intent: {intent}")
    lines.append(_grounding_line(grounding))
    lines.append("\nTOP FINDINGS (id — title):")
    for ref in refs[:_TOP_K]:
        lines.append(f"- {ref.get('check_id', '')} — {ref.get('title', '')}")
    return "\n".join(lines)


async def narrate_report(
    scores: Mapping[str, object],
    refs: Sequence[Mapping[str, object]],
    grounding: Sequence[Mapping[str, object]] | None,
    findings: Sequence[Mapping[str, object]],
    *,
    wm: WMClient,
    intent: str | None = None,
    model_override: str | None = None,
) -> tuple[ExecSummary | None, dict[str, int]]:
    """Write the executive summary via a single WM call (id-locked roadmap).

    Raises whatever :meth:`WMClient.message` raises — the caller fail-softs.
    """
    valid_check_ids = {str(f.get("check_id", "")) for f in findings if f.get("check_id")}
    response = await wm.message(
        worker_name=WORKER_NAME,
        message=build_prompt(scores, refs, grounding, intent=intent),
        output_schema=EXEC_SUMMARY_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    usage = _usage_dict(response.usage_metrics)
    exec_summary = _normalize_exec_summary(
        _coerce_to_dict(response.output), valid_check_ids=valid_check_ids
    )
    return exec_summary, usage


async def maybe_narrate_report(
    result: dict,
    *,
    refs: Sequence[Mapping[str, object]],
    findings: Sequence[Mapping[str, object]],
    scores: Mapping[str, object],
    grounding: Sequence[Mapping[str, object]] | None,
    include_exec_summary: bool,
    wm_client: WMClient | None,
    intent: str | None = None,
    ai: AiStatusCollector | None = None,
) -> None:
    """Fail-soft entry point the scorecard services call unconditionally.

    A no-op when ``include_exec_summary`` is off (byte-identical). Otherwise it
    writes the summary and merges it onto ``result["exec_summary"]`` **only when
    produced**, swallowing any WM/transport error so a summary failure never breaks
    the deterministic score. The outcome is recorded into ``ai`` as layer
    ``exec_summary``: ``failed`` on an error, ``empty`` when no summary came back.
    """
    if not include_exec_summary:
        return
    try:
        from src.llm_client import get_wm_client

        wm = wm_client or get_wm_client()
        exec_summary, _ = await narrate_report(
            scores, refs, grounding, findings, wm=wm, intent=intent
        )
    except Exception as exc:
        logger.warning(
            "audit exec-summary failed; returning scorecard without a summary",
            exc_info=True,
            extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
        )
        if ai is not None:
            ai.failed("exec_summary", WORKER_NAME, exc)
        return
    if exec_summary is None:
        if ai is not None:
            ai.empty("exec_summary", WORKER_NAME, "the model returned no summary")
        return
    result["exec_summary"] = exec_summary
    if ai is not None:
        ai.ok("exec_summary", WORKER_NAME)


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
