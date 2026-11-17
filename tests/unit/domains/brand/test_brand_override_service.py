"""Tests for brand override service."""

import pytest

from src.domains.brand.schemas import BrandResponse, FontInfo
from src.domains.brand.services.brand_override_service import BrandOverride, BrandOverrideService


@pytest.fixture
def service() -> BrandOverrideService:
    return BrandOverrideService()


@pytest.fixture
def base_brand() -> BrandResponse:
    """A baseline BrandResponse used across tests."""
    return BrandResponse(
        name="Acme Corp",
        domain="acme.com",
        logo_url="https://acme.com/logo.png",
        primary_color="#FF0000",
        secondary_color="#00FF00",
        has_logo=True,
        fonts=[
            FontInfo(
                family="Inter",
                category="sans-serif",
                role="heading",
                weights=["400", "700"],
                is_google_font=True,
                source_url="https://fonts.google.com/specimen/Inter",
            )
        ],
    )


# ── apply_overrides tests ──────────────────────────────────────────────


class TestApplyNameOverride:
    def test_apply_name_override(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """Name changes when override.name is set."""
        overrides = BrandOverride(name="New Name")
        result = service.apply_overrides(base_brand, overrides)
        assert result.name == "New Name"


class TestApplyColorOverride:
    def test_apply_color_override(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """primary_color changes when override.primary_color is set."""
        overrides = BrandOverride(primary_color="#AABBCC")
        result = service.apply_overrides(base_brand, overrides)
        assert result.primary_color == "#AABBCC"
        # secondary_color should remain unchanged
        assert result.secondary_color == "#00FF00"


class TestApplyLogoOverride:
    def test_apply_logo_override(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """logo_url changes and has_logo is updated accordingly."""
        overrides = BrandOverride(logo_url="https://newsite.com/logo.svg")
        result = service.apply_overrides(base_brand, overrides)
        assert result.logo_url == "https://newsite.com/logo.svg"
        assert result.has_logo is True

    def test_apply_empty_logo_clears_has_logo(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """Setting logo_url to empty string sets has_logo to False."""
        overrides = BrandOverride(logo_url="")
        result = service.apply_overrides(base_brand, overrides)
        assert result.logo_url == ""
        assert result.has_logo is False


class TestApplyFontOverride:
    def test_apply_font_override(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """font_family changes the first font's family when fonts already exist."""
        overrides = BrandOverride(font_family="Roboto")
        result = service.apply_overrides(base_brand, overrides)
        assert result.fonts[0].family == "Roboto"
        # Other font attributes preserved
        assert result.fonts[0].category == "sans-serif"

    def test_apply_font_override_no_existing(self, service: BrandOverrideService) -> None:
        """font_family adds a new font entry when fonts list is empty."""
        brand = BrandResponse(name="Empty Fonts Co", fonts=[])
        overrides = BrandOverride(font_family="Poppins")
        result = service.apply_overrides(brand, overrides)
        assert len(result.fonts) == 1
        assert result.fonts[0].family == "Poppins"
        assert result.fonts[0].role == "heading"


class TestApplyMultipleOverrides:
    def test_apply_multiple_overrides(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """Several fields change at once."""
        overrides = BrandOverride(
            name="Beta Inc",
            primary_color="#112233",
            secondary_color="#445566",
            logo_url="https://beta.com/logo.png",
            font_family="Montserrat",
        )
        result = service.apply_overrides(base_brand, overrides)
        assert result.name == "Beta Inc"
        assert result.primary_color == "#112233"
        assert result.secondary_color == "#445566"
        assert result.logo_url == "https://beta.com/logo.png"
        assert result.has_logo is True
        assert result.fonts[0].family == "Montserrat"


class TestApplyNoneFieldsUnchanged:
    def test_apply_none_fields_unchanged(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """None fields are not applied - original values preserved."""
        overrides = BrandOverride()  # all None
        result = service.apply_overrides(base_brand, overrides)
        assert result.name == "Acme Corp"
        assert result.primary_color == "#FF0000"
        assert result.secondary_color == "#00FF00"
        assert result.logo_url == "https://acme.com/logo.png"
        assert result.has_logo is True
        assert result.fonts[0].family == "Inter"


class TestOriginalNotModified:
    def test_original_not_modified(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """Original BrandResponse is unchanged after apply_overrides."""
        overrides = BrandOverride(name="Changed", primary_color="#000000")
        _ = service.apply_overrides(base_brand, overrides)
        # Original must be untouched
        assert base_brand.name == "Acme Corp"
        assert base_brand.primary_color == "#FF0000"


# ── validate_override tests ────────────────────────────────────────────


class TestValidateValidOverrides:
    def test_validate_valid_overrides(self, service: BrandOverrideService) -> None:
        """No errors returned for fully valid overrides."""
        overrides = BrandOverride(
            name="Good Name",
            primary_color="#AABB00",
            secondary_color="#CCD",
            logo_url="https://example.com/logo.png",
            font_family="Arial",
            tone="professional",
            tagline="We build things",
        )
        errors = service.validate_override(overrides)
        assert errors == []


class TestValidateInvalidHexColor:
    def test_validate_invalid_hex_primary(self, service: BrandOverrideService) -> None:
        """Error returned for invalid hex color format."""
        overrides = BrandOverride(primary_color="not-a-color")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "primary_color" in errors[0]

    def test_validate_invalid_hex_secondary(self, service: BrandOverrideService) -> None:
        """Error returned for invalid secondary hex color format."""
        overrides = BrandOverride(secondary_color="#GGHHII")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "secondary_color" in errors[0]


class TestValidateEmptyName:
    def test_validate_empty_name(self, service: BrandOverrideService) -> None:
        """Error returned for empty (whitespace-only) name."""
        overrides = BrandOverride(name="   ")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "empty" in errors[0].lower() or "Name" in errors[0]


class TestValidateLongName:
    def test_validate_long_name(self, service: BrandOverrideService) -> None:
        """Error returned for name exceeding 200 characters."""
        overrides = BrandOverride(name="A" * 201)
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "200" in errors[0]


class TestValidateInvalidLogoUrl:
    def test_validate_invalid_logo_url(self, service: BrandOverrideService) -> None:
        """Error returned for logo_url that doesn't start with http(s) or /."""
        overrides = BrandOverride(logo_url="ftp://bad.com/logo.png")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "logo_url" in errors[0]

    def test_validate_relative_path_logo_ok(self, service: BrandOverrideService) -> None:
        """No error for logo_url starting with /."""
        overrides = BrandOverride(logo_url="/static/logo.png")
        errors = service.validate_override(overrides)
        assert errors == []


class TestValidateInvalidTone:
    def test_validate_invalid_tone(self, service: BrandOverrideService) -> None:
        """Error returned for unknown tone value."""
        overrides = BrandOverride(tone="aggressive")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "tone" in errors[0].lower() or "aggressive" in errors[0]

    def test_validate_valid_tone_no_error(self, service: BrandOverrideService) -> None:
        """No error for a recognized tone."""
        overrides = BrandOverride(tone="casual")
        errors = service.validate_override(overrides)
        assert errors == []


class TestValidateLongTagline:
    def test_validate_long_tagline(self, service: BrandOverrideService) -> None:
        """Error returned for tagline exceeding 200 characters."""
        overrides = BrandOverride(tagline="X" * 201)
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "Tagline" in errors[0] or "200" in errors[0]


class TestValidateEmptyFontFamily:
    def test_validate_empty_font_family(self, service: BrandOverrideService) -> None:
        """Error returned for blank font_family."""
        overrides = BrandOverride(font_family="  ")
        errors = service.validate_override(overrides)
        assert len(errors) == 1
        assert "Font family" in errors[0] or "empty" in errors[0].lower()


# ── diff tests ─────────────────────────────────────────────────────────


class TestDiffNameChange:
    def test_diff_name_change(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """diff shows name difference."""
        overrides = BrandOverride(name="New Name")
        modified = service.apply_overrides(base_brand, overrides)
        changes = service.diff(base_brand, modified)
        assert "name" in changes
        assert changes["name"] == ("Acme Corp", "New Name")


class TestDiffColorChange:
    def test_diff_color_change(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """diff shows color difference."""
        overrides = BrandOverride(primary_color="#111111", secondary_color="#222222")
        modified = service.apply_overrides(base_brand, overrides)
        changes = service.diff(base_brand, modified)
        assert "primary_color" in changes
        assert changes["primary_color"] == ("#FF0000", "#111111")
        assert "secondary_color" in changes
        assert changes["secondary_color"] == ("#00FF00", "#222222")


class TestDiffNoChanges:
    def test_diff_no_changes(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """Empty dict when original and overridden are identical."""
        overrides = BrandOverride()  # no changes
        modified = service.apply_overrides(base_brand, overrides)
        changes = service.diff(base_brand, modified)
        assert changes == {}


class TestDiffFontChange:
    def test_diff_font_change(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """diff shows font_family difference."""
        overrides = BrandOverride(font_family="Roboto")
        modified = service.apply_overrides(base_brand, overrides)
        changes = service.diff(base_brand, modified)
        assert "font_family" in changes
        assert changes["font_family"] == ("Inter", "Roboto")


class TestDiffLogoChange:
    def test_diff_logo_change(
        self, service: BrandOverrideService, base_brand: BrandResponse
    ) -> None:
        """diff shows logo_url difference."""
        overrides = BrandOverride(logo_url="https://new.com/new-logo.svg")
        modified = service.apply_overrides(base_brand, overrides)
        changes = service.diff(base_brand, modified)
        assert "logo_url" in changes
        assert changes["logo_url"] == ("https://acme.com/logo.png", "https://new.com/new-logo.svg")
