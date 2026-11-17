"""Tests for API schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.domains.brand.schemas import (
    AnalysisData,
    BatchExtractRequest,
    BrandFromUrlRequest,
    BrandResponse,
    ColorInfo,
    ColorPalette,
    FontInfo,
    LogoAsset,
    PageData,
    SiteData,
)


class TestBrandResponseBackwardCompat:
    def test_default_values(self) -> None:
        """BrandResponse with no args should have all defaults."""
        brand = BrandResponse()
        assert brand.name == ""
        assert brand.domain == ""
        assert brand.logo_url == ""
        assert brand.primary_color == "#2563EB"
        assert brand.secondary_color == "#1E40AF"
        assert brand.has_logo is False

    def test_new_fields_have_defaults(self) -> None:
        """New fields should all have defaults (backward compat)."""
        brand = BrandResponse()
        assert brand.logo_asset is None
        assert brand.logo_serve_url == ""
        assert brand.color_palette is None
        assert brand.fonts == []
        assert brand.social_images is None
        assert brand.schema_org is None
        assert brand.manifest_theme_color == ""
        assert brand.favicon_urls == []
        assert brand.metadata is None

    def test_serialization_roundtrip(self) -> None:
        brand = BrandResponse(name="Test", domain="test.com")
        data = brand.model_dump()
        restored = BrandResponse.model_validate(data)
        assert restored.name == "Test"
        assert restored.domain == "test.com"


class TestBrandFromUrlRequest:
    def test_minimal(self) -> None:
        req = BrandFromUrlRequest(url="https://example.com")
        assert req.url == "https://example.com"
        assert req.download_logo is False
        assert req.include_fonts is True

    def test_with_options(self) -> None:
        req = BrandFromUrlRequest(
            url="https://example.com",
            download_logo=True,
            include_fonts=False,
        )
        assert req.download_logo is True
        assert req.include_fonts is False


class TestFontInfo:
    def test_defaults(self) -> None:
        font = FontInfo()
        assert font.family == ""
        assert font.is_google_font is False

    def test_with_values(self) -> None:
        font = FontInfo(family="Roboto", category="sans-serif", role="body", is_google_font=True)
        assert font.family == "Roboto"
        assert font.is_google_font is True


class TestColorInfo:
    def test_defaults(self) -> None:
        color = ColorInfo()
        assert color.hex == ""
        assert color.wcag_aa_text is False


class TestColorPalette:
    def test_defaults(self) -> None:
        palette = ColorPalette()
        assert palette.primary is None
        assert palette.colors == []

    def test_with_colors(self) -> None:
        palette = ColorPalette(
            primary=ColorInfo(hex="#FF0000", role="primary"),
            colors=[ColorInfo(hex="#FF0000", role="primary")],
        )
        assert palette.primary is not None
        assert len(palette.colors) == 1


class TestBatchExtractRequest:
    def test_valid(self) -> None:
        req = BatchExtractRequest(urls=["https://a.com", "https://b.com"])
        assert len(req.urls) == 2

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            BatchExtractRequest(urls=[])


class TestAnalysisData:
    def test_defaults(self) -> None:
        data = AnalysisData()
        assert data.pages == []
        assert data.site is None

    def test_with_data(self) -> None:
        data = AnalysisData(
            pages=[PageData(url="https://example.com", title="Test")],
            site=SiteData(description="A site"),
        )
        assert len(data.pages) == 1
        assert data.site is not None


class TestLogoAsset:
    def test_defaults(self) -> None:
        asset = LogoAsset()
        assert asset.asset_id == ""
        assert asset.size_bytes == 0
