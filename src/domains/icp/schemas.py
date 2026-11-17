"""Request/response models for GTM/ICP extraction from a URL.

The response keys are the **wire contract** consumed by Merqi's recon
understanding layer (``marketing/src/services/recon/understanding/brand.py``,
``_BRAND_FIELD_MAP``). Recon maps each present key into a grounded evidence
record and **drops any value in ``(None, "", [], {})``** — so an ungrounded
key is honestly reported as ``unknown`` rather than fabricated. Emit only keys
grounded in the fetched page; the route returns the model with
``response_model_exclude_unset=True`` so ungrounded keys never hit the wire.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.domains.brand.schemas import LLMConfigOverride

__all__ = ["ICPFromUrlRequest", "ICPResponse", "LLMConfigOverride"]


class ICPFromUrlRequest(BaseModel):
    """Request to extract a flat GTM/ICP record from a URL.

    Mirrors the minimal subset of ``BrandFromUrlRequest`` recon actually sends:
    only ``url`` is required. Extra fields are accepted for forward-compat.
    """

    url: str = Field(..., description="Public HTTPS URL to extract ICP signals from")
    llm_config: LLMConfigOverride | None = None
    max_pages: int = Field(
        default=3,
        ge=1,
        le=5,
        description=(
            "Max number of ADDITIONAL on-domain pages (pricing/about/product) fetched "
            "beyond the landing page to enrich extraction. Higher values populate "
            "pricing_signals / competitors that usually live off the landing page. "
            "If no matching links are discovered, only the landing page is used."
        ),
    )


class ICPResponse(BaseModel):
    """Flat ICP record — recon's exact key set (snake_case is canonical).

    Every field is optional. Fields left unset are excluded from the response
    (``response_model_exclude_unset=True``); recon treats an absent key as
    ``unknown``. ``source_url`` / ``token_usage`` are debug metadata recon
    ignores.
    """

    # --- product facet ---
    value_props: list[str] = Field(default_factory=list)
    product: str = ""
    offering: str = ""
    pricing_signals: str | list[str] = ""
    # --- company facet ---
    brand_identity: dict[str, Any] | None = None
    company: str = ""
    # --- market facet ---
    positioning: str = ""
    category: str = ""
    competitors: list[str] = Field(default_factory=list)
    # --- problem facet ---
    pains: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    # --- metadata (additive; ignored by recon, which stamps its own) ---
    source_url: str = ""
    token_usage: dict[str, Any] | None = None
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Grounded-key coverage: grounded contract keys / 11. Debug metric only.",
    )
    pages_fetched: list[str] = Field(
        default_factory=list,
        description="URLs whose content fed the extraction (landing first, then extras).",
    )
