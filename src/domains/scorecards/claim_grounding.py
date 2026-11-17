"""Tier-2 audit claim-grounding (A18) — verify each recommendation against the page.

The recommendation-writer (:mod:`recommendation_writer`) produces fix *prose* for a
finding. This module adds the opt-in ``audit-claim-grounding`` WM worker: for each
finding that already carries a ``recommendation`` block it checks the fix premise
against the page text the scanner already parsed (plus optional ``brief.sources[]``,
which the orchestrator will pass in PR4). The gate is **SOFT** — an unsupported
recommendation is *dropped* from the finding ref (so ungrounded advice never reaches
the wire), the run always completes, and nothing is ever hard-blocked.

Design mirrors ``recommendation_writer.py`` / ``domains/cro/llm_judge.py`` exactly:
tiny prompt + strict ``output_schema`` + ``response.usage_metrics``, all through the
shared :class:`WMClient` (never a provider directly — see the parent CLAUDE.md).
Every call is advisory + fail-soft: the caller wraps :func:`maybe_ground_claims`,
so a WM outage leaves the recommendations exactly as the writer produced them.
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

WORKER_NAME = "audit-claim-grounding"

# Safety bound on the per-audit WM fan-out — grounds at most this many
# recommendations (a run never issues more than one WM call per already-written
# recommendation, and never more than this).
_MAX_GROUNDINGS = 25

# The scanned page text is capped before it enters the (small) grounding prompt —
# grounding needs enough context to confirm a fix references a real element, not
# the whole DOM. Mirrors the snippet caps in cro/service.py.
_PAGE_TEXT_CAP = 4000

# Each source string is capped the same way so a long corpus cannot blow the prompt.
_SOURCE_CAP = 500
_MAX_SOURCES = 5


class GroundingResult(TypedDict):
    """One recommendation's grounding verdict (surfaced at the response top level)."""

    check_id: str
    supported: bool
    evidence_ref: str


GROUNDING_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "check_id": {"type": "string"},
        "supported": {"type": "boolean"},
        "evidence_ref": {"type": "string"},
    },
    "required": ["check_id", "supported"],
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
        raise ValueError(f"grounding JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _normalize_grounding(
    data: Mapping[str, object], *, expected_check_id: str
) -> GroundingResult | None:
    """Coerce raw WM output to a :class:`GroundingResult`, applying the echo clamp.

    Returns ``None`` (skip) when the ``check_id`` does not echo the finding's — a
    grounding verdict must be about the recommendation it was asked about
    (anti-drift). ``supported`` defaults to ``False`` when missing/garbage, so a
    malformed verdict fails safe (the recommendation is dropped, never leaked).
    """
    if str(data.get("check_id", "")).strip() != expected_check_id:
        return None
    supported = data.get("supported")
    if not isinstance(supported, bool):
        supported = str(supported).strip().lower() in {"true", "1", "yes"}
    evidence_ref = str(data.get("evidence_ref", "") or "").strip()[:120]
    return {"check_id": expected_check_id, "supported": supported, "evidence_ref": evidence_ref}


def _premise(ref: Mapping[str, object]) -> str:
    """The claim to fact-check — the recommendation's fix prose (title as fallback)."""
    rec = ref.get("recommendation")
    if isinstance(rec, dict):
        prose = str(cast("dict[str, object]", rec).get("fix_prose", "") or "").strip()
        if prose:
            return prose
    return str(ref.get("title", "") or "").strip()


def _clip_sources(sources: Sequence[str] | None) -> list[str]:
    """Cap the number and length of source strings entering the prompt."""
    if not sources:
        return []
    return [str(s).strip()[:_SOURCE_CAP] for s in sources[:_MAX_SOURCES] if str(s).strip()]


def build_prompt(
    ref: Mapping[str, object], *, page_text: str, sources: Sequence[str] | None
) -> str:
    """Render the tiny per-recommendation grounding prompt."""
    lines = [
        "Fact-check this proposed audit fix against the page text below.",
        f"- check_id: {ref.get('check_id', '')}",
        f"- fix: {_premise(ref)}",
        "",
        f"PAGE TEXT:\n{page_text[:_PAGE_TEXT_CAP]}",
    ]
    clipped = _clip_sources(sources)
    if clipped:
        lines.append("\nSOURCES:")
        lines.extend(f"- {s}" for s in clipped)
    return "\n".join(lines)


def _has_recommendation(ref: Mapping[str, object]) -> bool:
    """True when the writer already attached a recommendation block to this ref."""
    return isinstance(ref.get("recommendation"), Mapping)


async def ground_claims(
    refs: Sequence[dict],
    *,
    page_text: str,
    sources: Sequence[str] | None = None,
    wm: WMClient,
    model_override: str | None = None,
    failures: list[str] | None = None,
) -> tuple[list[GroundingResult], dict[str, int]]:
    """Ground every recommendation-carrying ref against the page (SOFT drop).

    One WM call per ref that carries a ``recommendation``. An unsupported verdict
    **drops** ``ref["recommendation"]`` in place (SOFT — the deterministic finding
    still surfaces; only the ungrounded advice is removed). The verdict is recorded
    in the returned list regardless, so the caller can surface a grounding summary.

    Because refs are mutated **in place while iterating**, grounding is best-effort
    *per ref*: a WM/transport/parse error on one ref is swallowed (that ref is left
    untouched and skipped) so it can never strip an *earlier* ref's recommendation
    and then vanish the whole grounding list. The result reflects exactly the refs
    that were successfully checked. Does not raise on a per-ref WM failure; when
    ``failures`` is passed, each swallowed per-ref error's description is appended
    to it so the caller can report the layer honestly.
    """
    from src.domains.scorecards.ai_status import describe_exception

    results: list[GroundingResult] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    attempted = 0
    seen: set[str] = set()
    for ref in refs:
        if attempted >= _MAX_GROUNDINGS:
            break
        check_id = str(ref.get("check_id", ""))
        if not check_id or check_id in seen or not _has_recommendation(ref):
            continue
        seen.add(check_id)
        attempted += 1
        try:
            response = await wm.message(
                worker_name=WORKER_NAME,
                message=build_prompt(ref, page_text=page_text, sources=sources),
                output_schema=GROUNDING_OUTPUT_SCHEMA,
                model_override=model_override,
            )
            verdict = _normalize_grounding(
                _coerce_to_dict(response.output), expected_check_id=check_id
            )
        except Exception as exc:
            if failures is not None:
                failures.append(describe_exception(exc))
            logger.warning(
                "audit claim-grounding failed for %s; leaving its recommendation intact",
                check_id,
                exc_info=True,
                extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
            )
            continue
        _accumulate_usage(usage_total, response.usage_metrics)
        if verdict is None:
            continue
        results.append(verdict)
        if not verdict["supported"]:
            # SOFT gate: strip the ungrounded recommendation; the finding remains.
            ref.pop("recommendation", None)
    logger.info(
        "audit claim-grounding checked %d recommendation(s); %d unsupported (dropped)",
        len(results),
        sum(1 for r in results if not r["supported"]),
    )
    return results, usage_total


async def maybe_ground_claims(
    refs: Sequence[dict],
    *,
    include_grounding: bool,
    wm_client: WMClient | None,
    page_text: str,
    sources: Sequence[str] | None = None,
    ai: AiStatusCollector | None = None,
) -> list[GroundingResult] | None:
    """Fail-soft entry point the scorecard services call unconditionally.

    Returns ``None`` (no ``grounding`` key surfaced) when ``include_grounding`` is
    off (byte-identical), when no ref carries a recommendation, or when nothing was
    successfully checked (every per-ref call failed) — so a ``grounding`` list only
    ever appears with at least one real verdict. Otherwise it grounds each
    recommendation, dropping the unsupported ones in place, and returns the verdict
    list. Per-ref failures are swallowed inside :func:`ground_claims`; this outer
    guard backstops any remaining error (e.g. ``get_wm_client``) so grounding failure
    never breaks the deterministic score. Every outcome is recorded into ``ai`` as
    layer ``grounding`` (design §3.2) — a swallowed failure is never silent.
    """
    if not include_grounding:
        return None
    if not any(_has_recommendation(r) for r in refs):
        if ai is not None:
            ai.skipped("grounding", WORKER_NAME, "no written recommendation to ground")
        return None
    failures: list[str] = []
    try:
        from src.llm_client import get_wm_client

        wm = wm_client or get_wm_client()
        results, _ = await ground_claims(
            refs, page_text=page_text, sources=sources, wm=wm, failures=failures
        )
    except Exception as exc:
        logger.warning(
            "audit claim-grounding failed; leaving recommendations ungrounded",
            exc_info=True,
            extra={"event": "audit_wm_failsoft", "worker": WORKER_NAME},
        )
        if ai is not None:
            ai.failed("grounding", WORKER_NAME, exc)
        return None
    if ai is not None:
        checked = len(results) + len(failures)
        if failures:
            ai.failed(
                "grounding",
                WORKER_NAME,
                f"{len(failures)} of {checked} recommendations failed: {failures[0]}",
            )
        elif not results:
            ai.empty("grounding", WORKER_NAME, "no grounding verdict was produced")
        else:
            ai.ok("grounding", WORKER_NAME)
    return results or None


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
