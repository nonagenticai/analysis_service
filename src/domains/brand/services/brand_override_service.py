"""Brand override service - manual brand data specification."""

from __future__ import annotations

import re
from typing import ClassVar

from pydantic import BaseModel

from src.domains.brand.schemas import BrandResponse


class BrandOverride(BaseModel):
    """Override fields for brand data. All optional - only non-None fields applied."""

    name: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    logo_url: str | None = None
    font_family: str | None = None
    tagline: str | None = None
    tone: str | None = None


class BrandOverrideService:
    """Apply, validate, and diff brand overrides."""

    # Valid hex color pattern
    _HEX_PATTERN = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

    # Valid tone values
    _VALID_TONES: ClassVar[set[str]] = {
        "professional",
        "casual",
        "friendly",
        "technical",
        "corporate",
        "playful",
        "luxury",
        "authoritative",
    }

    def apply_overrides(self, brand: BrandResponse, overrides: BrandOverride) -> BrandResponse:
        """Apply non-None override fields to a copy of the brand.

        Args:
            brand: Original brand data to copy.
            overrides: Override fields to apply (only non-None fields are merged).

        Returns:
            A new BrandResponse with overrides applied. Does not modify the original.
        """
        # Create a copy
        data = brand.model_dump()

        if overrides.name is not None:
            data["name"] = overrides.name
        if overrides.primary_color is not None:
            data["primary_color"] = overrides.primary_color
        if overrides.secondary_color is not None:
            data["secondary_color"] = overrides.secondary_color
        if overrides.logo_url is not None:
            data["logo_url"] = overrides.logo_url
            data["has_logo"] = bool(overrides.logo_url)
        if overrides.font_family is not None:
            # Update first font or add one
            if data.get("fonts") and len(data["fonts"]) > 0:
                data["fonts"][0]["family"] = overrides.font_family
            else:
                data["fonts"] = [
                    {
                        "family": overrides.font_family,
                        "category": "",
                        "role": "heading",
                        "weights": [],
                        "is_google_font": False,
                        "source_url": "",
                    }
                ]

        # tagline and tone will be set via setattr after construction if BrandResponse supports them
        result = BrandResponse(**data)

        if overrides.tagline is not None:
            result.tagline = overrides.tagline
        if overrides.tone is not None:
            result.brand_voice = overrides.tone

        return result

    def _validate_colors(self, overrides: BrandOverride, errors: list[str]) -> None:
        if overrides.primary_color is not None and not self._HEX_PATTERN.match(
            overrides.primary_color
        ):
            errors.append(f"Invalid primary_color hex format: {overrides.primary_color}")
        if overrides.secondary_color is not None and not self._HEX_PATTERN.match(
            overrides.secondary_color
        ):
            errors.append(f"Invalid secondary_color hex format: {overrides.secondary_color}")

    @staticmethod
    def _validate_name(overrides: BrandOverride, errors: list[str]) -> None:
        if overrides.name is None:
            return
        if not overrides.name.strip():
            errors.append("Name cannot be empty")
        elif len(overrides.name) > 200:
            errors.append("Name too long (max 200 characters)")

    @staticmethod
    def _validate_logo_url(overrides: BrandOverride, errors: list[str]) -> None:
        if overrides.logo_url and not overrides.logo_url.startswith(("http://", "https://", "/")):
            errors.append("Invalid logo_url: must start with http://, https://, or /")

    def _validate_tone(self, overrides: BrandOverride, errors: list[str]) -> None:
        if overrides.tone is not None and overrides.tone not in self._VALID_TONES:
            errors.append(f"Invalid tone: {overrides.tone}. Valid: {sorted(self._VALID_TONES)}")

    @staticmethod
    def _validate_tagline_and_font(overrides: BrandOverride, errors: list[str]) -> None:
        if overrides.tagline is not None and len(overrides.tagline) > 200:
            errors.append("Tagline too long (max 200 characters)")
        if overrides.font_family is not None and not overrides.font_family.strip():
            errors.append("Font family cannot be empty")

    def validate_override(self, overrides: BrandOverride) -> list[str]:
        """Validate override values against format and length constraints.

        Args:
            overrides: The override fields to validate.

        Returns:
            List of validation error messages. Empty list means valid.
        """
        errors: list[str] = []
        self._validate_colors(overrides, errors)
        self._validate_name(overrides, errors)
        self._validate_logo_url(overrides, errors)
        self._validate_tone(overrides, errors)
        self._validate_tagline_and_font(overrides, errors)
        return errors

    def diff(self, original: BrandResponse, overridden: BrandResponse) -> dict[str, tuple]:
        """Show what changed between original and overridden brand.

        Args:
            original: The brand before overrides.
            overridden: The brand after overrides.

        Returns:
            Dict of {field_name: (original_value, new_value)} for changed fields.
        """
        changes: dict[str, tuple] = {}

        fields_to_compare = [
            "name",
            "primary_color",
            "secondary_color",
            "logo_url",
            "has_logo",
        ]

        for field_name in fields_to_compare:
            old_val = getattr(original, field_name, None)
            new_val = getattr(overridden, field_name, None)
            if old_val != new_val:
                changes[field_name] = (old_val, new_val)

        # Compare first font family
        old_font = original.fonts[0].family if original.fonts else ""
        new_font = overridden.fonts[0].family if overridden.fonts else ""
        if old_font != new_font:
            changes["font_family"] = (old_font, new_font)

        # Compare extended fields
        for ext_field in ("tagline", "brand_voice"):
            old_val = getattr(original, ext_field, "")
            new_val = getattr(overridden, ext_field, "")
            if old_val != new_val:
                changes[ext_field] = (old_val, new_val)

        return changes
