"""UX reviewer using AI for website critique, routed through worker_management."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.scanners.base import make_finding
from src.domains.critique.services.scanners.ux_claims import (
    CLAIM_TYPES,
    KINDS,
    SCOPES,
    apply_page_guards,
)
from src.domains.critique.services.scanners.ux_template_loader import DIMENSIONS
from src.llm_client import get_wm_client
from src.llm_credentials import model_override_from

if TYPE_CHECKING:
    from w2v_common.clients import WMClient


@runtime_checkable
class UXReviewerProtocol(Protocol):
    """Structural type for UX reviewer collaborators.

    Captures the surface ``CritiqueService`` actually consumes: an async
    ``scan`` that returns finding dicts, plus a ``_last_token_usage`` slot
    populated as a side-effect of ``scan``. Tests can satisfy this Protocol
    with a duck-typed fake instead of a real ``UXReviewer``.
    """

    _last_token_usage: dict[str, int] | None

    async def scan(
        self,
        html: str,
        url: str = "",
        instructions: str | None = None,
        site_analysis: dict | None = None,
        llm_config: LLMConfigOverride | None = None,
        soup: BeautifulSoup | None = None,
    ) -> list[dict]: ...


logger = logging.getLogger(__name__)

MAX_HTML_CHARS = 50000

_SEVERITIES = ("critical", "high", "medium", "low", "info")

#: Structured fields the page guards in ``ux_claims`` act on. They are asked for
#: in the prompt AND enforced by ``output_schema`` -- the guards never read the
#: title's wording.
FINDING_FIELD_RULES = """Every finding carries these fields:
- kind: "issue" for a problem, "strength" for something the page does well.
- claim_type: when the finding says something is ABSENT from the page, the matching
  value (missing_contact_form = no form collecting email/phone/message;
  missing_contact_method = no way at all to reach the business; missing_phone;
  missing_email; missing_search; missing_breadcrumbs). Otherwise "other".
- scope: "page" if the claim is about the whole page; "region" if about part of it
  (above the fold, the footer, the pricing section); "conditional" if about a state
  (zero search results, validation errors, after submit).
- evidence: a CSS selector matching the element, or text copied exactly from the page.
  Leave empty for an absence claim."""

_FINDING_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "severity": {"type": "string", "enum": list(_SEVERITIES)},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "recommendation": {"type": "string"},
        "claim_type": {"type": "string", "enum": list(CLAIM_TYPES)},
        "scope": {"type": "string", "enum": list(SCOPES)},
        "evidence": {"type": "string"},
    },
    "required": ["kind", "severity", "title", "description", "claim_type", "scope"],
}

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "object",
            "properties": {
                dim: {
                    "type": "object",
                    "properties": {
                        "score": {"type": "number"},
                        "findings": {"type": "array", "items": _FINDING_SCHEMA},
                    },
                    "required": ["score", "findings"],
                }
                for dim in DIMENSIONS
            },
        }
    },
    "required": ["dimensions"],
}

_DATA_URI_RE = re.compile(r"^data:", re.IGNORECASE)


def _strip_noise(soup: BeautifulSoup) -> str:
    """Strip script/style content, SVGs, and data URIs for LLM analysis.

    Re-parses from string to avoid mutating the caller's soup.
    """
    clone = BeautifulSoup(str(soup), "lxml")
    for tag in clone.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()
    for tag in clone.find_all(True):
        for attr in list(tag.attrs):
            val = tag.get(attr, "")
            if isinstance(val, str) and _DATA_URI_RE.match(val):
                tag[attr] = "[data-uri]"
    return str(clone)


DEFAULT_UX_PROMPT = """Analyze the following HTML page for UX issues across these dimensions:
1. Navigation: clarity, discoverability, consistency
2. Visual Hierarchy: content organization, readability, spacing
3. CTA Effectiveness: call-to-action clarity, placement, urgency
4. Mobile Responsiveness: responsive indicators, touch-friendly design
5. Form Usability: form design, validation, error messages
6. Error Handling: feedback patterns, error states, recovery
7. Trust Signals: credibility indicators, social proof, security cues
8. Value Proposition: clear value communication, benefit-driven copy

Report problems as kind "issue". Anything the page does well is kind "strength" -- it is listed separately and never scored.

Return a JSON object (NOT an array) with this structure:
{
  "dimensions": {
    "navigation": {"score": <0-100>, "findings": [{"kind": "issue|strength", "severity": "critical|high|medium|low|info", "title": "...", "description": "...", "recommendation": "...", "claim_type": "missing_contact_form|missing_contact_method|missing_phone|missing_email|missing_search|missing_breadcrumbs|other", "scope": "page|region|conditional", "evidence": "CSS selector or exact page text"}]},
    "visual_hierarchy": {"score": <0-100>, "findings": [...]},
    "cta_effectiveness": {"score": <0-100>, "findings": [...]},
    "mobile_responsiveness": {"score": <0-100>, "findings": [...]},
    "form_usability": {"score": <0-100>, "findings": [...]},
    "error_handling": {"score": <0-100>, "findings": [...]},
    "trust_signals": {"score": <0-100>, "findings": [...]},
    "value_proposition": {"score": <0-100>, "findings": [...]}
  }
}

Return ONLY the JSON object, no other text.

HTML to analyze:
{html}
"""


def _enum(item: dict, key: str, allowed: tuple[str, ...], default: str | None) -> str | None:
    value = item.get(key)
    return value if value in allowed else default


def _llm_finding(item: dict, url: str, description: str) -> dict:
    """A UX finding carrying the structured fields ``ux_claims`` acts on.

    A missing or unknown field falls back to the value that suppresses nothing:
    ``issue``, ``other``, no scope.
    """
    evidence = item.get("evidence")
    finding = make_finding(
        category="ux",
        severity=item.get("severity", "info"),
        title=item.get("title", "UX Issue"),
        description=description,
        url=url,
        recommendation=item.get("recommendation", ""),
        selector=item.get("selector"),
        evidence=evidence.strip() or None if isinstance(evidence, str) else None,
        source_tool="ux_reviewer",
        false_positive_risk="medium",
    )
    finding["kind"] = _enum(item, "kind", KINDS, "issue")
    finding["claim_type"] = _enum(item, "claim_type", CLAIM_TYPES, "other")
    finding["scope"] = _enum(item, "scope", SCOPES, None)
    return finding


class UXReviewer:
    """AI-powered UX analysis routed through worker_management."""

    WORKER_NAME = "critique-ux-review"

    _SYSTEM_PROMPT = (
        "You are a UX expert. Reply with the JSON object requested in the "
        "prompt. Output ONLY the JSON object, no markdown fences, no commentary."
    )

    def __init__(
        self,
        wm_client: WMClient | None = None,
        model: str | None = None,
    ) -> None:
        self._wm = wm_client if wm_client is not None else get_wm_client()
        # ``model`` is retained as a constructor-level WM model override,
        # so existing tests that pass ``model="..."`` keep working. Per-call
        # ``llm_config.model`` overrides this when set.
        self._default_model_override = model
        self._last_dimension_scores: dict[str, float] = {}
        self._last_token_usage: dict | None = None
        #: ``kind == "strength"`` items from the last scan -- kept out of findings.
        self._last_strengths: list[dict] = []

    async def scan(
        self,
        html: str,
        url: str = "",
        instructions: str | None = None,
        site_analysis: dict | None = None,
        llm_config: LLMConfigOverride | None = None,
        soup: BeautifulSoup | None = None,
    ) -> list[dict]:
        """Analyze HTML for UX issues using LLM.

        Args:
            html: HTML content to analyze.
            url: Source URL for findings attribution.
            instructions: Optional custom analysis instructions.
            site_analysis: Optional pre-computed site analysis with tech stack info.
            llm_config: Optional LLM provider overrides.
            soup: Optional pre-parsed BeautifulSoup tree (avoids re-parsing).

        Returns:
            List of UX finding dicts.
        """
        self._last_strengths = []
        # Strip noise (scripts, styles, SVGs, data URIs) before truncation
        page = soup if soup is not None else BeautifulSoup(html, "lxml")
        cleaned = _strip_noise(page)
        truncated = cleaned[:MAX_HTML_CHARS]

        # Try template system, fall back to inline prompt
        try:
            from src.domains.critique.services.scanners.ux_template_loader import build_ux_prompt

            prompt = build_ux_prompt(truncated, instructions)
        except ValueError, TypeError, KeyError, OSError, RuntimeError:
            prompt = DEFAULT_UX_PROMPT.format(html=truncated)
            if instructions:
                prompt = f"Additional instructions: {instructions}\n\n{prompt}"

        # Both prompt shapes end with the page; the field rules go just before it.
        prompt = prompt.replace("HTML to analyze", f"{FINDING_FIELD_RULES}\n\nHTML to analyze", 1)

        # Inject tech stack context so LLM knows what technology the site uses
        if site_analysis and isinstance(site_analysis, dict):
            tech_stack = site_analysis.get("tech_stack", "unknown")
            site_type = site_analysis.get("site_type", "unknown")
            tech_context = f"Site context: Tech stack: {tech_stack}. Site type: {site_type}.\n\n"
            prompt = tech_context + prompt

        try:
            import time

            model_override = model_override_from(llm_config) or self._default_model_override
            logger.info(
                "Starting UX LLM review: url=%s worker=%s llm_backend=%s",
                url,
                self.WORKER_NAME,
                getattr(self._wm, "llm_backend", "unknown"),
            )
            t0 = time.monotonic()
            response = await self._wm.message(
                worker_name=self.WORKER_NAME,
                message=prompt,
                system_prompt=self._SYSTEM_PROMPT,
                output_schema=OUTPUT_SCHEMA,
                model_override=model_override,
            )
            usage = response.usage_metrics or {}
            self._last_token_usage = (
                {
                    "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                    "total_tokens": int(usage.get("total_tokens", 0) or 0),
                }
                if usage
                else None
            )
            elapsed = round(time.monotonic() - t0, 2)
            logger.info("UX LLM review completed: url=%s elapsed=%.2fs", url, elapsed)

            text = (
                response.output if isinstance(response.output, str) else json.dumps(response.output)
            )
            if not text:
                text = "[]"
            # The model's claims are checked against the parsed page before
            # they can move the score -- see ``ux_claims``.
            issues, strengths = apply_page_guards(self._parse_response(text, url), page, url)
            self._last_strengths = strengths
            return issues

        except (ValueError, TypeError, KeyError, OSError, RuntimeError, httpx.HTTPError) as e:
            logger.error("UX review failed: %s", e)
            return [
                make_finding(
                    category="ux",
                    severity="info",
                    title="UX review error",
                    description=f"AI UX analysis failed: {e}",
                    url=url,
                    recommendation="Check WM connectivity.",
                    source_tool="ux_reviewer",
                    false_positive_risk="medium",
                )
            ]

    def _parse_response(self, text: str, url: str) -> list[dict]:
        """Parse Claude's response into findings."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]

        # Try to find JSON
        # First try: object with dimensions
        start_obj = text.find("{")
        end_obj = text.rfind("}")
        start_arr = text.find("[")
        end_arr = text.rfind("]")

        findings: list[dict] = []
        self._last_dimension_scores = {}

        # Try structured format first
        if start_obj != -1 and end_obj != -1:
            try:
                data = json.loads(text[start_obj : end_obj + 1])
                if "dimensions" in data:
                    return self._parse_dimensions(data["dimensions"], url)
            except json.JSONDecodeError:
                pass

        # Fall back to array format
        if start_arr != -1 and end_arr != -1:
            try:
                items = json.loads(text[start_arr : end_arr + 1])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    findings.append(_llm_finding(item, url, item.get("description", "")))
                return findings
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse UX review response as JSON")
        return [
            make_finding(
                category="ux",
                severity="info",
                title="UX review parse error",
                description="Could not parse AI response. Raw output available in logs.",
                url=url,
                source_tool="ux_reviewer",
                false_positive_risk="medium",
            )
        ]

    def _parse_dimensions(self, dimensions: dict, url: str) -> list[dict]:
        """Parse structured dimension data into findings."""
        findings: list[dict] = []
        for dim_name, dim_data in dimensions.items():
            if not isinstance(dim_data, dict):
                continue
            score = dim_data.get("score")
            if score is not None:
                self._last_dimension_scores[dim_name] = score
            for item in dim_data.get("findings", []):
                if not isinstance(item, dict):
                    continue
                dim_label = dim_name.replace("_", " ").title()
                description = item.get("description", "")
                findings.append(_llm_finding(item, url, f"[{dim_label}] {description}"))
        return findings
