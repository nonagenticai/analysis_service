"""Opt-in Tier-3 micro-LLM analyzers for the SEO audit.

Three small, single-shot, schema-constrained Worker-Management calls — the *only*
places in the audit where computation genuinely cannot decide:

- :class:`SeoIntentClassifier` — classify the detected primary keyword's search
  intent (informational / commercial / transactional / navigational).
- :class:`SeoEeatDepthAnalyzer` — rate Experience/Expertise *depth* of the copy.
- :class:`SeoContentGapAnalyzer` — name topics competitors cover that the page
  misses (driven by the audit service, which fetches the competitors).

The analyzer classes follow the ICP idiom (pass ``output_schema``, coerce the
structured output). A WM/transport error **propagates** from them; malformed
output returns ``None``. :func:`enrich_seo_audit` is the fail-soft layer: it
turns each analyzer run into a :class:`SeoLlmOutcome` (ok / failed / empty /
skipped + reason + a structured annotation) so the caller can REPORT a failure
instead of it vanishing into a log line (design 2026-10-17 F8/F9/F11).

The outputs are annotations, never findings: they do not enter the scored or
bucketed findings list, so a flaky model can never change a customer's number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, TypedDict

from bs4 import BeautifulSoup, Tag
from w2v_common.clients.wm_client import WMClient

from src.domains.critique.services.scanners.seo.content_utils import main_text, words
from src.domains.critique.services.scanners.seo.keyword import detect_primary_keyword
from src.domains.critique.services.scanners.seo.signals import has_author_signal
from src.llm_client import get_wm_client

logger = logging.getLogger(__name__)

_MIN_WORDS_FOR_EEAT = 200
_EEAT_EXCERPT_CHARS = 4000
_INTENTS = ("informational", "commercial", "transactional", "navigational")
_DEPTH_LEVELS = ("strong", "present", "weak", "missing")


def _coerce_to_dict(output: Any) -> dict[str, Any]:
    """WM structured output is a dict; tolerate a JSON string just in case."""
    if isinstance(output, dict):
        return output
    if isinstance(output, str):
        import json

        try:
            parsed = json.loads(output)
        except ValueError, TypeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class SeoIntentClassifier:
    """Classify a keyword's search intent via one WM call."""

    WORKER_NAME = "seo-intent-classify"
    _SYSTEM_PROMPT = (
        "You classify the search intent of a keyword. Reply with ONLY the JSON "
        "object requested — no markdown, no commentary."
    )
    OUTPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(_INTENTS)},
            "confidence": {"type": "number"},
        },
        "required": ["intent"],
    }

    def __init__(self, wm_client: WMClient | None = None, model: str | None = None) -> None:
        self._wm = wm_client if wm_client is not None else get_wm_client()
        self._model = model

    async def classify(self, keyword: str, title: str) -> dict[str, Any] | None:
        prompt = (
            f'Keyword: "{keyword}"\n'
            f'Page title: "{title}"\n\n'
            "Classify the dominant search intent as one of: informational, "
            "commercial, transactional, navigational. Respond as JSON "
            '{"intent": "...", "confidence": 0.0-1.0}.'
        )
        response = await self._wm.message(
            worker_name=self.WORKER_NAME,
            message=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            output_schema=self.OUTPUT_SCHEMA,
            model_override=self._model,
        )
        data = _coerce_to_dict(response.output)
        intent = data.get("intent")
        return data if intent in _INTENTS else None


class SeoEeatDepthAnalyzer:
    """Rate Experience/Expertise depth of the page copy via one WM call."""

    WORKER_NAME = "seo-eeat-depth"
    _SYSTEM_PROMPT = (
        "You are an SEO quality rater assessing E-E-A-T. Reply with ONLY the JSON "
        "object requested — no markdown, no commentary."
    )
    OUTPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "experience": {"type": "string", "enum": list(_DEPTH_LEVELS)},
            "expertise": {"type": "string", "enum": list(_DEPTH_LEVELS)},
            "evidence": {"type": "string"},
        },
        "required": ["experience", "expertise"],
    }

    def __init__(self, wm_client: WMClient | None = None, model: str | None = None) -> None:
        self._wm = wm_client if wm_client is not None else get_wm_client()
        self._model = model

    async def analyze(self, excerpt: str, author: str) -> dict[str, Any] | None:
        prompt = (
            f"Author signal: {author or 'none detected'}\n\n"
            f"Content excerpt:\n{excerpt}\n\n"
            "Rate the first-hand Experience and the Expertise depth of this "
            "content, each as one of: strong, present, weak, missing. Respond as "
            'JSON {"experience": "...", "expertise": "...", "evidence": "one short sentence"}.'
        )
        response = await self._wm.message(
            worker_name=self.WORKER_NAME,
            message=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            output_schema=self.OUTPUT_SCHEMA,
            model_override=self._model,
        )
        data = _coerce_to_dict(response.output)
        if data.get("experience") in _DEPTH_LEVELS and data.get("expertise") in _DEPTH_LEVELS:
            return data
        return None


class SeoContentGapAnalyzer:
    """Name the topics competitors cover that the target page misses, via one WM call.

    Deterministic competitor keyword sets go *into* the prompt; the model only
    NAMES the missing topics grounded in those lists — it never invents. A WM error
    propagates (the caller records it); the result is an annotation, score-neutral.
    """

    WORKER_NAME = "seo-content-gap"
    _SYSTEM_PROMPT = (
        "You are an SEO content strategist. You are given the target page's "
        "keyword set and each competitor's keyword set (already extracted). Name "
        "ONLY topics that competitors clearly cover and the target does NOT. "
        "Ground every topic in the supplied keyword lists — never invent. Reply "
        "with ONLY the JSON object requested — no markdown, no commentary."
    )
    OUTPUT_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "covered_by": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["topic"],
                },
            }
        },
        "required": ["gaps"],
    }

    def __init__(self, wm_client: WMClient | None = None, model: str | None = None) -> None:
        self._wm = wm_client if wm_client is not None else get_wm_client()
        self._model = model

    async def analyze(
        self,
        target_keywords: list[str],
        competitor_sets: list[tuple[str, list[str]]],
        title: str,
    ) -> dict[str, Any] | None:
        competitor_lines = "\n".join(
            f"Competitor {chr(ord('A') + i)} ({comp_url}): {', '.join(comp_kw)}"
            for i, (comp_url, comp_kw) in enumerate(competitor_sets)
        )
        prompt = (
            f'Target page title: "{title}"\n'
            f"Target keywords (already extracted): {', '.join(target_keywords)}\n"
            f"{competitor_lines}\n\n"
            "List the distinct topics the competitors clearly cover that the target "
            "is missing. Ground every topic in the supplied keyword lists — never "
            'invent. Respond as JSON {"gaps": [{"topic": "...", '
            '"covered_by": ["A"], "rationale": "one short sentence"}]}.'
        )
        response = await self._wm.message(
            worker_name=self.WORKER_NAME,
            message=prompt,
            system_prompt=self._SYSTEM_PROMPT,
            output_schema=self.OUTPUT_SCHEMA,
            model_override=self._model,
        )
        data = _coerce_to_dict(response.output)
        raw_gaps = data.get("gaps")
        if not isinstance(raw_gaps, list):
            return None
        if not raw_gaps:
            # A well-formed "no gaps" answer is a RESULT, not a malformed reply.
            return {"gaps": []}
        # Anti-fabrication: keep only well-formed gaps with a non-empty topic.
        gaps = [
            gap
            for gap in raw_gaps
            if isinstance(gap, dict) and isinstance(gap.get("topic"), str) and gap["topic"].strip()
        ]
        return {"gaps": gaps} if gaps else None


SeoLlmLayer = Literal["seo_intent", "seo_eeat", "content_gap"]
SeoLlmStatus = Literal["ok", "failed", "empty", "skipped"]


class SeoLlmAnnotation(TypedDict):
    """A structured AI observation (wire: one ``ai.annotations[]`` entry, minus ``layer``)."""

    kind: str
    title: str
    summary: str
    data: dict[str, object]


@dataclass(frozen=True)
class SeoLlmOutcome:
    """What one SEO analyzer run produced — reported, never swallowed.

    ``annotation`` (``{kind, title, summary, data}``) is set only on ``ok``.
    ``by_design`` is False for a skip that means the requested analysis did not
    happen (e.g. no competitor page could be fetched), so it degrades the status.
    """

    layer: SeoLlmLayer
    worker: str
    status: SeoLlmStatus
    reason: str | None = None
    error: BaseException | None = None
    annotation: SeoLlmAnnotation | None = None
    by_design: bool = True


def _page_title(soup: BeautifulSoup) -> str:
    title_tag = soup.find("title")
    return title_tag.get_text(" ", strip=True) if isinstance(title_tag, Tag) else ""


async def run_intent(
    soup: BeautifulSoup, *, wm_client: WMClient, model_override: str | None = None
) -> SeoLlmOutcome:
    """Classify the primary keyword's search intent; fail-soft, outcome reported."""
    worker = SeoIntentClassifier.WORKER_NAME
    primary = detect_primary_keyword(soup)
    if not primary:
        return SeoLlmOutcome(
            "seo_intent", worker, "skipped", "no dominant primary keyword detected on the page"
        )
    try:
        intent = await SeoIntentClassifier(wm_client, model_override).classify(
            primary, _page_title(soup)
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never fatal
        logger.warning("seo-intent-classify failed: %s", exc)
        return SeoLlmOutcome("seo_intent", worker, "failed", error=exc)
    if not intent:
        return SeoLlmOutcome("seo_intent", worker, "empty", "the model returned no valid intent")
    label = intent["intent"]
    return SeoLlmOutcome(
        "seo_intent",
        worker,
        "ok",
        annotation={
            "kind": "search_intent",
            "title": f"Search intent: {label} (primary keyword '{primary}')",
            "summary": (
                f"The detected primary keyword '{primary}' has {label} search intent. "
                "Align the page's format and CTA with this intent."
            ),
            "data": {"intent": label, "keyword": primary, "confidence": intent.get("confidence")},
        },
    )


async def run_eeat(
    soup: BeautifulSoup, *, wm_client: WMClient, model_override: str | None = None
) -> SeoLlmOutcome:
    """Rate E-E-A-T depth of the main copy; fail-soft, outcome reported."""
    worker = SeoEeatDepthAnalyzer.WORKER_NAME
    text = main_text(soup)
    word_count = len(words(text))
    if word_count < _MIN_WORDS_FOR_EEAT:
        return SeoLlmOutcome(
            "seo_eeat",
            worker,
            "skipped",
            f"main content is {word_count} words, under the {_MIN_WORDS_FOR_EEAT}-word "
            "minimum for an E-E-A-T depth rating",
        )
    author = "present" if has_author_signal(soup) else ""
    try:
        depth = await SeoEeatDepthAnalyzer(wm_client, model_override).analyze(
            text[:_EEAT_EXCERPT_CHARS], author
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never fatal
        logger.warning("seo-eeat-depth failed: %s", exc)
        return SeoLlmOutcome("seo_eeat", worker, "failed", error=exc)
    if not depth:
        return SeoLlmOutcome("seo_eeat", worker, "empty", "the model returned no valid rating")
    evidence = str(depth.get("evidence") or "")
    return SeoLlmOutcome(
        "seo_eeat",
        worker,
        "ok",
        annotation={
            "kind": "eeat_depth",
            "title": (
                f"E-E-A-T depth — experience: {depth['experience']}, "
                f"expertise: {depth['expertise']}"
            ),
            "summary": (
                f"Model assessment of content depth. Experience: {depth['experience']}; "
                f"Expertise: {depth['expertise']}. {evidence}"
            ).strip(),
            "data": {
                "experience": depth["experience"],
                "expertise": depth["expertise"],
                "evidence": evidence,
                "author_signal": bool(author),
            },
        },
    )


async def enrich_seo_audit(
    soup: BeautifulSoup,
    *,
    wm_client: WMClient,
    model_override: str | None = None,
) -> list[SeoLlmOutcome]:
    """Run the intent + E-E-A-T analyzers; one :class:`SeoLlmOutcome` each.

    Never raises for a WM failure — each failure comes back as a ``failed``
    outcome carrying the exception, so the audit can report it.
    """
    return [
        await run_intent(soup, wm_client=wm_client, model_override=model_override),
        await run_eeat(soup, wm_client=wm_client, model_override=model_override),
    ]
