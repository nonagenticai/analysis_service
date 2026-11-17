"""Pipeline context: shared state bag passed through all stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from src.domains.brand.services.extraction_config import BrandExtractConfig, default_config
from src.domains.brand.services.extraction_tracker import ExtractionTracker

if TYPE_CHECKING:
    import httpx
    from w2v_common.clients import WMClient

    from src.domains.brand.services.logo_service import LogoService


@dataclass
class PipelineContext:
    """Shared state bag for pipeline stages."""

    url: str = ""
    tenant_id: str = ""
    config: BrandExtractConfig = field(default_factory=default_config)
    http_client: httpx.AsyncClient | None = None

    # Populated by FetchStage
    html: str = ""
    soup: BeautifulSoup | None = None

    # Populated by Playwright FetchStage
    screenshot_bytes: bytes = b""
    computed_styles: dict[str, Any] = field(default_factory=dict)
    page_text: str = ""

    # Populated by VisualColorStage
    visual_dominant_colors: list[str] = field(default_factory=list)

    # Feature flags from request
    download_logo: bool = False
    include_fonts: bool = True
    include_colors: bool = True
    include_metadata: bool = True

    # Shared intermediate state
    background_color: str = "#ffffff"
    schema_org_dict: dict[str, Any] | None = None
    css_urls: list[str] = field(default_factory=list)

    # Tracker
    tracker: ExtractionTracker = field(default_factory=ExtractionTracker)

    # Dependencies (injected from app.state)
    logo_service: LogoService | None = None
    wm_client: WMClient | None = None
    llm_model_override: str | None = None
