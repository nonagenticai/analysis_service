"""Tier-3 CRO micro-judge — tiny, schema-driven conversion-copy scoring via WM.

Opt-in enrichment for ``POST /api/cro/score`` (``include_llm=true``). Unlike the
heavy ``critique.services.scanners.ux_reviewer`` (which ships the whole ~50k-char
page and hand-parses free-form JSON), this judges only a handful of short,
already-extracted on-page snippets (headline / value-prop / testimonial) in a
single structured call, mirroring ``domains/icp/service.py``: tiny prompt +
strict ``output_schema`` + ``response.usage_metrics``.

All LLM calls go through ``worker_management`` via the shared :class:`WMClient`
— never providers directly (see the parent CLAUDE.md). The judgments are
**advisory annotation only**: the deterministic CRO score is never derived from
them, and the caller (``cro.service.score_cro``) treats any failure here as
fail-soft (the deterministic score is still returned).
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.scorecards.ai_status import AiStatusCollector

logger = logging.getLogger(__name__)

WORKER_NAME = "cro-micro-judge"

# The snippet elements we know how to judge. Anything the LLM returns outside
# this set is dropped (anti-drift), so a hallucinated element never reaches the wire.
_KNOWN_ELEMENTS = {"headline", "value_prop", "testimonial"}

SYSTEM_PROMPT = """\
You are a conversion-copy analyst. You are given a few short snippets of copy \
taken from one landing page, each labelled by its element type. Judge each \
snippet ON ITS OWN AXIS and return ONLY the requested JSON object.

Axes:
- headline: the 4-U test — is it Useful, Urgent, Unique, and Ultra-specific? \
Reward a concrete quantified benefit; penalise vague/generic claims.
- value_prop: clarity and specificity — does it state a concrete outcome for a \
concrete audience, or is it fluff?
- testimonial: credibility — named attribution (person + role/company), a \
specific result, and believability; penalise anonymous or generic praise.

For each snippet return: element (echo the given label), score (0-100 integer \
for that axis), assessment (one short sentence), and suggestion (one short, \
concrete rewrite/action). Ground every judgment strictly in the provided \
snippet text — use no outside knowledge and do not invent facts. Return ONLY \
valid JSON, no markdown fences, no explanation."""

CRO_JUDGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "element": {"type": "string"},
                    "score": {"type": "number"},
                    "assessment": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["element"],
            },
        }
    },
    "required": [],
}


def build_prompt(snippets: dict[str, str]) -> str:
    """Render the (small) per-snippet prompt from the extracted snippet map."""
    lines = ["Judge each of the following on-page snippets:\n"]
    for element, text in snippets.items():
        lines.append(f"- {element}: {text}")
    return "\n".join(lines)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from an LLM response (mirror icp/brand)."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


class CroJudgment(TypedDict):
    """One normalised micro-judgment row (the ``CroLlmJudgment`` wire shape)."""

    element: str
    score: float
    assessment: str
    suggestion: str


def _coerce_to_dict(raw: Any) -> dict[str, Any]:
    """Normalize WM ``output`` (dict for structured-output, str for text) to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        parsed = json.loads(_strip_markdown_fences(raw))
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"cro-micro-judge JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _normalize_judgments(data: dict[str, Any]) -> list[CroJudgment]:
    """Coerce the raw ``judgments`` array to the wire contract, dropping unknowns."""
    raw = data.get("judgments")
    if not isinstance(raw, list):
        return []
    out: list[CroJudgment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        element = str(item.get("element", "")).strip()
        if element not in _KNOWN_ELEMENTS:
            continue
        try:
            score = float(item.get("score", 0) or 0)
        except TypeError, ValueError:
            score = 0.0
        out.append(
            {
                "element": element,
                "score": max(0.0, min(100.0, score)),
                "assessment": str(item.get("assessment", "") or ""),
                "suggestion": str(item.get("suggestion", "") or ""),
            }
        )
    return out


async def judge_cro_snippets(
    wm_client: WMClient,
    snippets: dict[str, str],
    *,
    model_override: str | None = None,
) -> tuple[list[CroJudgment], dict[str, Any] | None]:
    """Score extracted snippets via WM; return ``(judgments, token_usage)``.

    One structured WM call for all snippets (cheaper/lighter than N per-snippet
    calls; the prompt is a handful of short strings). Raises whatever
    :meth:`WMClient.message` raises plus :class:`ValueError` / JSON errors on
    shape problems — the caller decides fail-soft vs surfaced error.
    """
    response = await wm_client.message(
        worker_name=WORKER_NAME,
        message=build_prompt(snippets),
        system_prompt=SYSTEM_PROMPT,
        output_schema=CRO_JUDGE_OUTPUT_SCHEMA,
        model_override=model_override,
    )
    judgments = _normalize_judgments(_coerce_to_dict(response.output))
    token_usage: dict[str, Any] | None = None
    if response.usage_metrics:
        usage = response.usage_metrics
        token_usage = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    logger.info("CRO micro-judge complete: elements=%s", sorted(j["element"] for j in judgments))
    return judgments, token_usage


async def maybe_judge_snippets(
    snippets: dict[str, str],
    *,
    include_llm: bool,
    wm_client: WMClient | None,
    ai: AiStatusCollector | None = None,
) -> list[CroJudgment] | None:
    """Fail-soft micro-judge entry point; records layer ``micro_judge`` into ``ai``.

    Returns the judgments, or ``None`` when the judge did not run or failed. A page
    with no judgeable snippet is ``skipped`` (by design); a WM error is ``failed``
    with its reason; a successful call that judged nothing is ``empty``. The
    deterministic score is never an input and never affected.
    """
    if not include_llm:
        return None
    if not snippets:
        if ai is not None:
            ai.skipped(
                "micro_judge", WORKER_NAME, "no headline / value prop / testimonial to judge"
            )
        return None
    try:
        from src.llm_client import get_wm_client

        wm = wm_client or get_wm_client()
        judgments, _usage = await judge_cro_snippets(wm, snippets)
    except Exception as exc:
        logger.warning("CRO micro-judge failed; returning deterministic score only", exc_info=True)
        if ai is not None:
            ai.failed("micro_judge", WORKER_NAME, exc)
        return None
    if ai is not None:
        if judgments:
            ai.ok("micro_judge", WORKER_NAME)
        else:
            ai.empty("micro_judge", WORKER_NAME, "the model returned no usable judgment")
    return judgments
