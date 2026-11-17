"""Tier-3 audit recommendation-writer — turns bare finding titles into fix-prose.

The deterministic scorecards (``cro`` / ``seo_audit`` / ``marketing``) surface
findings as bare *titles* — a checklist of problem names, no advice. This module
adds the opt-in ``audit-recommendation-writer`` WM worker: for each actionable
finding that carries an evidence snippet it produces specific fix prose, clamped
so the prose **must cite that evidence** (no hallucinated fixes), bounded by a
short evaluator-optimizer loop (``<=2`` passes), and woven back onto the finding
ref as an additive ``recommendation`` block.

Design mirrors the live ``domains/cro/llm_judge.py`` exactly: tiny prompt +
strict ``output_schema`` + ``response.usage_metrics``, all through the shared
:class:`WMClient` (never a provider directly — see the parent CLAUDE.md). Every
call is **advisory**: the deterministic score is never an input to it and the
caller (each scorecard ``service.py``) treats any failure here as fail-soft —
the deterministic titles-only response is returned unchanged.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.scorecards.ai_status import AiStatusCollector

logger = logging.getLogger(__name__)

WORKER_NAME = "audit-recommendation-writer"

# Safety bound on the per-audit WM fan-out: even a large scan writes prose for at
# most this many findings (highest-priority first, since refs arrive rank-sorted
# when the caller opted into priority). Keeps a pathological page from issuing
# hundreds of WM calls.
_MAX_RECOMMENDATIONS = 25

# Evaluator-optimizer: at most this many WM calls per finding (a first draft plus
# one revise pass). The counter is asserted in tests — it must never be exceeded.
_MAX_PASSES = 2

# The evaluator's accept threshold on the heuristic quality score (see
# :func:`_score_recommendation`). A draft at or above this is accepted without a
# revise pass; below it triggers exactly one revision, then the best draft wins.
_ACCEPT_THRESHOLD = 4

# Evidence tokens shorter than this are ignored when checking whether the prose
# cites the evidence — they are function words / markup noise, not anchors.
_MIN_TOKEN_LEN = 4

# Hard cap on each prose field returned to the caller. The WM path is advisory
# and free-text; without a clamp a runaway generation could weave a multi-KB
# ramble onto a finding ref. Truncation is applied only on the opt-in WM path
# (off ⇒ byte-identical).
_MAX_PROSE_LEN = 600

_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class WriteStats:
    """What one :func:`attach_recommendations` pass actually did (for ``ai`` status)."""

    attempted: int = 0
    written: int = 0
    failures: list[str] = field(default_factory=list)


class Recommendation(TypedDict):
    """The fix-prose block woven onto a finding ref (all fields plain strings)."""

    fix_prose: str
    rationale: str
    example: str
    effort: str


RECOMMENDATION_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "check_id": {"type": "string"},
        "fix_prose": {"type": "string"},
        "rationale": {"type": "string"},
        "example": {"type": "string"},
        "effort": {"type": "string"},
    },
    "required": ["check_id", "fix_prose"],
}

_EFFORT_VALUES = {"low", "medium", "high"}


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from an LLM response (mirror cro/llm_judge)."""
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
        raise ValueError(f"recommendation JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _evidence_tokens(evidence: str) -> set[str]:
    """Meaningful lower-cased tokens from an evidence snippet (markup stripped).

    Tokens shorter than :data:`_MIN_TOKEN_LEN` are dropped as noise. An evidence
    string that yields no usable token cannot be cited, so the caller skips
    writing a recommendation for it (there is nothing to clamp against).
    """
    text = _TAG_RE.sub(" ", evidence).lower()
    return {t for t in _TOKEN_RE.findall(text) if len(t) >= _MIN_TOKEN_LEN}


def _cites_evidence(fix_prose: str, tokens: set[str]) -> bool:
    """True when the prose references at least one meaningful evidence token.

    Overlap is at the WORD level (the prose is tokenised the same way as the
    evidence), not substring: an evidence token ``form`` must not be considered
    "cited" merely because the prose contains ``transform``. Substring matching
    would let generic prose slip past the clamp on any short common fragment.
    """
    if not tokens:
        return False
    prose_tokens = {t for t in _TOKEN_RE.findall(fix_prose.lower()) if len(t) >= _MIN_TOKEN_LEN}
    return bool(tokens & prose_tokens)


def _normalize_recommendation(
    data: Mapping[str, object], *, expected_check_id: str
) -> Recommendation | None:
    """Coerce raw WM output to a :class:`Recommendation`, applying the echo clamp.

    Returns ``None`` (drop) when the ``check_id`` does not echo the finding's
    (anti-drift — a recommendation must be about the finding it was asked about)
    or the ``fix_prose`` is empty. The evidence clamp is applied by the caller,
    which holds the evidence tokens.
    """
    if str(data.get("check_id", "")).strip() != expected_check_id:
        return None
    fix_prose = str(data.get("fix_prose", "") or "").strip()
    if not fix_prose:
        return None
    effort = str(data.get("effort", "") or "").strip().lower()
    if effort not in _EFFORT_VALUES:
        effort = ""
    return {
        "fix_prose": fix_prose[:_MAX_PROSE_LEN],
        "rationale": str(data.get("rationale", "") or "").strip()[:_MAX_PROSE_LEN],
        "example": str(data.get("example", "") or "").strip()[:_MAX_PROSE_LEN],
        "effort": effort,
    }


def _score_recommendation(rec: Recommendation, *, cites: bool) -> int:
    """Heuristic quality score for the evaluator-optimizer (higher is better).

    Deterministic and side-effect free so the loop is reproducible in tests:
      +2 the prose cites the evidence (the load-bearing clamp signal),
      +1 the prose is a substantive length (not a stub, not a ramble),
      +1 a concrete example is present,
      +1 a rationale is present.
    A draft scoring ``>= _ACCEPT_THRESHOLD`` is accepted without revision.
    """
    score = 0
    if cites:
        score += 2
    if 20 <= len(rec["fix_prose"]) <= 400:
        score += 1
    if rec["example"]:
        score += 1
    if rec["rationale"]:
        score += 1
    return score


def build_prompt(
    ref: Mapping[str, object],
    *,
    evidence: str,
    intent: str | None,
    critique: str | None = None,
) -> str:
    """Render the tiny per-finding prompt (optionally with a revise critique)."""
    lines = [
        "Write a fix for this audit finding:",
        f"- check_id: {ref.get('check_id', '')}",
        f"- title: {ref.get('title', '')}",
        f"- severity: {ref.get('severity', '')}",
    ]
    if intent:
        lines.append(f"- page intent: {intent}")
    lines.append(f"- evidence: {evidence}")
    if critique:
        lines.append(
            "\nYour previous draft was inadequate: "
            f"{critique} Rewrite it, citing the evidence above."
        )
    return "\n".join(lines)


async def write_recommendation(
    wm: WMClient,
    ref: Mapping[str, object],
    *,
    evidence: str,
    intent: str | None = None,
    model_override: str | None = None,
) -> tuple[Recommendation | None, dict[str, int]]:
    """Write one finding's fix prose via WM with a bounded evaluator-optimizer loop.

    Runs at most :data:`_MAX_PASSES` WM calls: a first draft and, only if the
    draft scores below :data:`_ACCEPT_THRESHOLD`, one revise pass. Both the echo
    clamp (:func:`_normalize_recommendation`) and the evidence clamp
    (:func:`_cites_evidence`) must pass or the draft is discarded; the best
    *cited* draft across the passes is returned, else ``None`` (SOFT drop).

    Raises whatever :meth:`WMClient.message` raises — the caller fail-softs.
    """
    tokens = _evidence_tokens(evidence)
    check_id = str(ref.get("check_id", ""))
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    best: Recommendation | None = None
    best_score = -1
    critique: str | None = None

    for _ in range(_MAX_PASSES):
        response = await wm.message(
            worker_name=WORKER_NAME,
            message=build_prompt(ref, evidence=evidence, intent=intent, critique=critique),
            output_schema=RECOMMENDATION_OUTPUT_SCHEMA,
            model_override=model_override,
        )
        _accumulate_usage(usage_total, response.usage_metrics)
        rec = _normalize_recommendation(
            _coerce_to_dict(response.output), expected_check_id=check_id
        )
        if rec is None:
            critique = "It must echo the given check_id and give a non-empty fix."
            continue
        cites = _cites_evidence(rec["fix_prose"], tokens)
        score = _score_recommendation(rec, cites=cites)
        if cites and score > best_score:
            best, best_score = rec, score
        if cites and score >= _ACCEPT_THRESHOLD:
            break
        critique = "Be more specific and quote the exact evidence snippet."

    return best, usage_total


async def attach_recommendations(
    refs: Sequence[dict],
    findings: Sequence[Mapping[str, object]],
    *,
    wm: WMClient,
    intent: str | None = None,
    model_override: str | None = None,
    stats: WriteStats | None = None,
) -> tuple[int, dict[str, int]]:
    """Weave fix-prose onto every actionable ref that carries citable evidence.

    Evidence is resolved from the authoritative ``findings`` (by ``check_id``),
    not from the ref, so this works whether or not the caller opted into evidence
    surfacing. Each written recommendation is attached in place as
    ``ref["recommendation"]`` (an additive key). Refs with no citable evidence are
    skipped (SOFT — there is nothing for the clamp to anchor on), and the fan-out
    is capped at :data:`_MAX_RECOMMENDATIONS`.

    Returns ``(count_written, aggregate_usage)``. A per-ref WM/parse error is
    skipped (see below); when ``stats`` is passed, attempts / writes / per-ref
    failure reasons are recorded into it so the caller can report the layer's
    real outcome instead of a silent "titles only".
    """
    from src.domains.scorecards.ai_status import describe_exception

    evidence_by_check = _evidence_by_check(findings)
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    written = 0
    attempted = 0  # bounds the WM fan-out — capped on findings TRIED, not written
    seen: set[str] = set()
    for ref in refs:
        # Cap on *attempts*, not writes: a run where every draft is dropped
        # (uncited / wrong check_id) must still not fan out past the cap.
        if attempted >= _MAX_RECOMMENDATIONS:
            break
        check_id = str(ref.get("check_id", ""))
        # Ref lists (quick_wins + strategic) can repeat a check_id; write once.
        if not check_id or check_id in seen:
            continue
        evidence = evidence_by_check.get(check_id)
        if not evidence or not _evidence_tokens(evidence):
            continue
        seen.add(check_id)
        attempted += 1
        # Per-ref fail-soft (mirrors claim_grounding.ground_claims): a WM/transport
        # error or a non-JSON draft on ONE finding must not abort the loop and strand
        # the remaining findings — which would ship a partial, non-deterministic set
        # while the caller logs "titles only" (false). Skip the bad ref, keep going.
        try:
            rec, usage = await write_recommendation(
                wm, ref, evidence=evidence, intent=intent, model_override=model_override
            )
        except Exception as exc:
            if stats is not None:
                stats.failures.append(describe_exception(exc))
            logger.warning(
                "audit recommendation-writer: skipping check_id=%s after WM/parse error",
                check_id,
                exc_info=True,
                extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
            )
            continue
        _accumulate_usage(usage_total, usage)
        if rec is not None:
            ref["recommendation"] = rec
            written += 1
    if stats is not None:
        stats.attempted += attempted
        stats.written += written
    logger.info(
        "audit recommendation-writer wrote %d recommendation(s) from %d attempt(s)",
        written,
        attempted,
    )
    return written, usage_total


async def maybe_write_recommendations(
    refs: Sequence[dict],
    findings: Sequence[Mapping[str, object]],
    *,
    include_recommendations: bool,
    wm_client: WMClient | None,
    intent: str | None = None,
    ai: AiStatusCollector | None = None,
) -> None:
    """Fail-soft entry point the scorecard services call unconditionally.

    A no-op when ``include_recommendations`` is off (byte-identical). Otherwise it
    resolves the WM client and weaves prose onto ``refs`` in place, swallowing any
    WM/transport error so a writer failure never breaks the deterministic score.
    This is the **single** fail-soft boundary — and since design §3.2 it is no
    longer silent: the outcome is recorded into ``ai`` as layer
    ``recommendations`` (``ok`` / ``failed`` with a ref-failure count / ``empty``).
    """
    if not include_recommendations:
        return
    stats = WriteStats()
    try:
        from src.llm_client import get_wm_client

        wm = wm_client or get_wm_client()
        await attach_recommendations(refs, findings, wm=wm, intent=intent, stats=stats)
    except Exception as exc:
        logger.warning(
            "audit recommendation-writer failed; returning titles only",
            exc_info=True,
            extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
        )
        if ai is not None:
            ai.failed("recommendations", WORKER_NAME, exc)
        return
    if ai is not None:
        _record_write_outcome(ai, stats)


def _record_write_outcome(ai: AiStatusCollector, stats: WriteStats) -> None:
    """Translate one writer pass into the ``recommendations`` layer status."""
    if stats.failures:
        ai.failed(
            "recommendations",
            WORKER_NAME,
            f"{len(stats.failures)} of {stats.attempted} findings failed: {stats.failures[0]}",
        )
    elif stats.attempted == 0:
        ai.empty("recommendations", WORKER_NAME, "no finding carried citable evidence")
    elif stats.written == 0:
        ai.empty(
            "recommendations",
            WORKER_NAME,
            f"0 of {stats.attempted} drafts cited their evidence; none attached",
        )
    else:
        ai.ok("recommendations", WORKER_NAME, f"{stats.written} of {stats.attempted} written")


def _evidence_by_check(findings: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Map check_id -> first non-empty evidence snippet among the findings."""
    out: dict[str, str] = {}
    for f in findings:
        cid = str(f.get("check_id", ""))
        if not cid or cid in out:
            continue
        ev = f.get("evidence")
        if isinstance(ev, str) and ev.strip():
            out[cid] = ev
    return out


def _accumulate_usage(total: dict[str, int], usage: Mapping[str, object] | None) -> None:
    """Add a WM call's token usage into the running total (missing keys -> 0)."""
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = usage.get(key, 0)
        if isinstance(val, (int, float, str)):
            try:
                total[key] += int(val)
            except TypeError, ValueError:
                continue
