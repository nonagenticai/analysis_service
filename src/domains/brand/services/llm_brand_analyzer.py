"""LLM-powered brand analysis routed through worker_management.

w2v services NEVER call openai/anthropic/inference-router directly. Every
LLM call goes through ``worker_management.POST /workers/{name}/message``
so cost is attributed per tenant and providers are swapped server-side.
See memory ``wm-as-sole-llm-gateway.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from w2v_common.clients import WMClient

logger = logging.getLogger(__name__)

WORKER_NAME = "brand-analysis"


BRAND_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brand_voice": {"type": "string"},
        "tone": {"type": "string"},
        "formality_score": {"type": "number"},
        "tagline": {"type": "string"},
        "key_messages": {"type": "array", "items": {"type": "string"}},
        "target_audience": {"type": "string"},
        "brand_personality": {"type": "array", "items": {"type": "string"}},
        "industry": {"type": "string"},
        "sub_industry": {"type": "string"},
        "industry_confidence": {"type": "number"},
        "tone_confidence": {"type": "number"},
    },
    "required": ["brand_voice", "tone", "industry"],
}


@dataclass
class LLMBrandAnalysis:
    """Parsed result from LLM brand analysis."""

    brand_voice: str = ""
    tone: str = ""
    formality_score: float = 0.5
    tagline: str = ""
    key_messages: list[str] = field(default_factory=list)
    target_audience: str = ""
    brand_personality: list[str] = field(default_factory=list)
    industry: str = ""
    sub_industry: str = ""
    industry_confidence: float = 0.0
    tone_confidence: float = 0.0
    token_usage: dict[str, Any] | None = None

    def to_content_dict(self) -> dict[str, Any]:
        return {
            "tagline": self.tagline,
            "brand_voice": self.brand_voice,
            "key_messages": self.key_messages,
            "target_audience": self.target_audience,
            "brand_voice_confidence": self.tone_confidence,
            "source": "llm_analysis",
        }

    def to_tone_dict(self) -> dict[str, Any]:
        return {
            "tone": self.tone,
            "formality": self.formality_score,
            "confidence": self.tone_confidence,
            "source": "llm_analysis",
        }

    def to_industry_dict(self) -> dict[str, Any]:
        return {
            "industry": self.industry,
            "sub_industry": self.sub_industry,
            "confidence": self.industry_confidence,
            "source": "llm_analysis",
        }


def build_prompt(
    page_text: str,
    title: str = "",
    meta_description: str = "",
    headings: list[str] | None = None,
    domain: str = "",
    schema_org_type: str = "",
) -> str:
    """Build the user prompt for LLM brand analysis."""
    parts = []
    if domain:
        parts.append(f"Domain: {domain}")
    if title:
        parts.append(f"Page title: {title}")
    if meta_description:
        parts.append(f"Meta description: {meta_description}")
    if schema_org_type:
        parts.append(f"Schema.org type: {schema_org_type}")
    if headings:
        parts.append(f"Headings: {'; '.join(headings[:10])}")

    truncated_text = page_text[:16000]
    parts.append(f"\nPage content:\n{truncated_text}")

    return "\n".join(parts)


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences from LLM response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _coerce_to_dict(raw: Any) -> dict[str, Any]:
    """Normalize WM ``output`` (dict for structured-output, str for text) to dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        cleaned = _strip_markdown_fences(raw)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"Brand-analysis JSON is not an object: got {type(parsed).__name__}")
    raise ValueError(f"Unexpected WM output type: {type(raw).__name__}")


def _parse_response(raw: Any) -> LLMBrandAnalysis:
    """Parse a WM response (dict or JSON string) into :class:`LLMBrandAnalysis`."""
    data = _coerce_to_dict(raw)

    return LLMBrandAnalysis(
        brand_voice=str(data.get("brand_voice", "")),
        tone=str(data.get("tone", "")),
        formality_score=float(data.get("formality_score", 0.5)),
        tagline=str(data.get("tagline", "")),
        key_messages=[str(m) for m in data.get("key_messages", [])],
        target_audience=str(data.get("target_audience", "")),
        brand_personality=[str(p) for p in data.get("brand_personality", [])],
        industry=str(data.get("industry", "")),
        sub_industry=str(data.get("sub_industry", "")),
        industry_confidence=float(data.get("industry_confidence", 0.0)),
        tone_confidence=float(data.get("tone_confidence", 0.0)),
    )


async def analyze_brand_with_llm(
    wm_client: WMClient,
    prompt: str,
    *,
    model_override: str | None = None,
) -> LLMBrandAnalysis:
    """Call WM and return parsed brand analysis.

    Args:
        wm_client: Shared :class:`WMClient` instance.
        prompt: User prompt with website content.
        model_override: Optional WM model override.

    Returns:
        Parsed :class:`LLMBrandAnalysis`.

    Raises:
        Whatever ``WMClient.message`` raises (``httpx.HTTPStatusError`` /
        connection errors), plus :class:`ValueError` /
        :class:`json.JSONDecodeError` on shape errors.
    """
    response = await wm_client.message(
        worker_name=WORKER_NAME,
        message=prompt,
        output_schema=BRAND_OUTPUT_SCHEMA,
        model_override=model_override,
    )

    result = _parse_response(response.output)
    if response.usage_metrics:
        usage = response.usage_metrics
        result.token_usage = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }
    logger.info(
        "Brand LLM analysis complete: tone=%s industry=%s",
        result.tone,
        result.industry,
    )
    return result
