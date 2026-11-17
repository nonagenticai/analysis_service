"""Rendering, external-tool integration, and peer-client settings.

Playwright browser rendering (LIVE — the brand pipeline's fetch stage), the
video-compiler peer, prompt templates, the CGH code-quality client, and the
external-tool + scanner (critique-era, still live) configuration.

The image-generation classes that used to live here — ``DalleSettings``,
``FluxSettings``, ``ScreenshotSettings``, ``ContentGenerationSettings`` — were
deleted: no code in this service ever read them. See the retired-prefix table
in ``src/config/__init__.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "CGHSettings",
    "ExternalToolSettings",
    "PlaywrightSettings",
    "ScannerSettings",
    "TemplateSettings",
    "VideoCompilerSettings",
]


class ScannerSettings(BaseSettings):
    """Per-scanner timeout and configuration (critique domain legacy)."""

    seo_timeout: int = Field(default=30, description="SEO scanner timeout in seconds")
    security_timeout: int = Field(default=30, description="Security scanner timeout in seconds")
    a11y_timeout: int = Field(default=30, description="Accessibility scanner timeout in seconds")
    performance_timeout: int = Field(
        default=30, description="Performance scanner timeout in seconds"
    )
    ux_timeout: int = Field(default=180, description="UX reviewer timeout in seconds")
    code_quality_timeout: int = Field(default=120, description="Code quality scanner timeout")
    cro_timeout: int = Field(default=30, description="CRO scanner timeout in seconds")
    overall_timeout: int = Field(default=300, description="Overall job timeout in seconds")
    max_retries: int = Field(default=2, description="Max retries per scanner on failure")

    model_config = SettingsConfigDict(
        env_prefix="SCANNER_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class ExternalToolSettings(BaseSettings):
    """External tool integration configuration (critique domain legacy)."""

    enabled: bool = Field(default=True, description="Enable external tool integration")
    axe_enabled: bool = Field(default=True, description="Enable axe-core adapter")
    axe_timeout: int = Field(default=60, description="axe-core scan timeout in seconds")
    lighthouse_enabled: bool = Field(default=False, description="Enable Lighthouse adapter")
    lighthouse_timeout: int = Field(default=120, description="Lighthouse scan timeout in seconds")
    lighthouse_node_path: str = Field(default="node", description="Path to Node.js binary")
    lighthouse_cli_path: str = Field(default="lighthouse", description="Path to Lighthouse CLI")
    zap_enabled: bool = Field(default=False, description="Enable OWASP ZAP adapter")
    # Empty default — ZAP scans are opt-in (zap_enabled=False by default).
    # Set ZAP_API_URL when zap_enabled=True; no localhost lock-in.
    zap_api_url: str = Field(default="", description="ZAP REST API URL")
    zap_api_key: str = Field(default="", description="ZAP API key")
    zap_timeout: int = Field(default=180, description="ZAP scan timeout in seconds")
    nuclei_enabled: bool = Field(default=False, description="Enable Nuclei adapter")
    nuclei_path: str = Field(default="nuclei", description="Path to Nuclei binary")
    nuclei_timeout: int = Field(default=120, description="Nuclei scan timeout in seconds")

    model_config = SettingsConfigDict(
        env_prefix="EXTERNAL_TOOL_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class PlaywrightSettings(BaseSettings):
    """Playwright browser rendering configuration.

    ⚠️ LIVE — not legacy. Every field here is read by
    ``src/domains/brand/stages/fetch.py`` (``get_settings().playwright``), which
    binds the section to a local before reading it. That indirection is why an
    earlier audit mis-filed this class as inert; deleting it would break brand
    extraction. See ``tests/unit/shared/test_no_dead_settings_surface.py``.
    """

    headless: bool = Field(default=True, description="Run browser in headless mode")
    timeout_ms: int = Field(default=30000, description="Page load timeout in milliseconds")
    viewport_width: int = Field(default=1440, description="Browser viewport width")
    viewport_height: int = Field(default=900, description="Browser viewport height")
    wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = Field(
        default="networkidle", description="Playwright wait_until strategy"
    )

    model_config = SettingsConfigDict(
        env_prefix="PLAYWRIGHT_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class TemplateSettings(BaseSettings):
    """Prompt template configuration."""

    version: str = Field(default="v1", description="Template version to use")
    directory: str | None = Field(default=None, description="Override template directory path")

    model_config = SettingsConfigDict(
        env_prefix="TEMPLATE_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class CGHSettings(BaseSettings):
    """Code Quality (CGH) API configuration."""

    api_url: str = Field(default="", description="CGH API URL")
    api_key: str = Field(default="", description="CGH API key")
    timeout: int = Field(default=120, description="CGH API timeout in seconds")
    enabled: bool = Field(default=False, description="Enable code quality scanning")

    model_config = SettingsConfigDict(
        env_prefix="CGH_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class VideoCompilerSettings(BaseSettings):
    """Video compiler service client configuration.

    The peer URL is resolved via :func:`w2v_common.discovery.service_url`
    using the canonical ``video_production`` logical name.
    """

    enabled: bool = Field(default=True, description="Enable video generation")
    timeout: int = Field(default=300, description="Video compilation timeout in seconds")

    model_config = SettingsConfigDict(
        env_prefix="VIDEO_COMPILER_", env_file=".env", case_sensitive=False, extra="ignore"
    )
