"""Unit tests for brand comparator service."""

from __future__ import annotations

from src.domains.brand.schemas import BrandResponse, FontInfo
from src.domains.brand.services.brand_comparator import BrandComparator
from src.domains.brand.services.color_service import hex_to_rgb


class TestHexToRgb:
    def test_standard_6_digit_hex(self) -> None:
        assert hex_to_rgb("#FF5733") == (255, 87, 51)

    def test_3_digit_shorthand(self) -> None:
        assert hex_to_rgb("#F00") == (255, 0, 0)

    def test_without_hash(self) -> None:
        assert hex_to_rgb("00FF00") == (0, 255, 0)

    def test_black(self) -> None:
        assert hex_to_rgb("#000000") == (0, 0, 0)

    def test_white(self) -> None:
        assert hex_to_rgb("#FFFFFF") == (255, 255, 255)

    def test_invalid_length_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            hex_to_rgb("#FFFFF")

    def test_invalid_chars_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            hex_to_rgb("#XXYYZZ")

    def test_empty_string_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            hex_to_rgb("")


class TestColorDistance:
    def setup_method(self) -> None:
        self.comparator = BrandComparator()

    def test_identical_colors_zero_distance(self) -> None:
        assert self.comparator._color_distance("#FF5733", "#FF5733") == 0.0

    def test_black_to_white_max_distance(self) -> None:
        dist = self.comparator._color_distance("#000000", "#FFFFFF")
        # sqrt(255^2 + 255^2 + 255^2) = 441.67...
        assert 441.0 < dist < 442.0

    def test_red_to_blue(self) -> None:
        dist = self.comparator._color_distance("#FF0000", "#0000FF")
        # sqrt(255^2 + 0 + 255^2) = 360.62...
        assert 360.0 < dist < 361.0


class TestCompareBrands:
    def setup_method(self) -> None:
        self.comparator = BrandComparator()

    def test_identical_brands_high_similarity(self) -> None:
        brand_a = BrandResponse(
            name="Acme Corp",
            domain="acme.com",
            primary_color="#FF5733",
            secondary_color="#1E40AF",
            has_logo=True,
            tagline="Innovation First",
            brand_voice="professional",
            fonts=[FontInfo(family="Inter")],
        )
        brand_b = BrandResponse(
            name="Acme Corp",
            domain="acme.com",
            primary_color="#FF5733",
            secondary_color="#1E40AF",
            has_logo=True,
            tagline="Innovation First",
            brand_voice="professional",
            fonts=[FontInfo(family="Inter")],
        )
        result = self.comparator.compare(brand_a, brand_b)

        assert result["overall_similarity"] == 1.0
        assert result["color_distance"] == 0.0
        assert len(result["field_comparisons"]) > 0

    def test_completely_different_brands_low_similarity(self) -> None:
        brand_a = BrandResponse(
            name="Acme Corp",
            domain="acme.com",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            has_logo=True,
            tagline="Innovation First",
            brand_voice="professional",
            fonts=[FontInfo(family="Inter")],
        )
        brand_b = BrandResponse(
            name="Widget Inc",
            domain="widget.com",
            primary_color="#0000FF",
            secondary_color="#FF00FF",
            has_logo=False,
            tagline="Quality Matters",
            brand_voice="casual",
            fonts=[FontInfo(family="Roboto")],
        )
        result = self.comparator.compare(brand_a, brand_b)

        assert result["overall_similarity"] < 0.5
        assert result["color_distance"] > 0

    def test_field_comparisons_structure(self) -> None:
        brand_a = BrandResponse(name="A", domain="a.com", primary_color="#111111")
        brand_b = BrandResponse(name="B", domain="b.com", primary_color="#222222")
        result = self.comparator.compare(brand_a, brand_b)

        field_names = [fc["field"] for fc in result["field_comparisons"]]
        assert "name" in field_names
        assert "primary_color" in field_names
        assert "has_logo" in field_names
        assert "fonts" in field_names

    def test_matching_has_logo_counted(self) -> None:
        brand_a = BrandResponse(has_logo=True)
        brand_b = BrandResponse(has_logo=True)
        result = self.comparator.compare(brand_a, brand_b)

        has_logo_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "has_logo")
        assert has_logo_comp["match"] is True

    def test_mismatched_has_logo(self) -> None:
        brand_a = BrandResponse(has_logo=True)
        brand_b = BrandResponse(has_logo=False)
        result = self.comparator.compare(brand_a, brand_b)

        has_logo_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "has_logo")
        assert has_logo_comp["match"] is False

    def test_font_overlap_similarity(self) -> None:
        brand_a = BrandResponse(fonts=[FontInfo(family="Inter"), FontInfo(family="Roboto")])
        brand_b = BrandResponse(fonts=[FontInfo(family="inter"), FontInfo(family="Arial")])
        result = self.comparator.compare(brand_a, brand_b)

        fonts_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "fonts")
        # "Inter" overlaps (case-insensitive), union = {inter, roboto, arial} => 1/3 < 0.5
        assert fonts_comp["match"] is False

    def test_font_high_overlap(self) -> None:
        brand_a = BrandResponse(fonts=[FontInfo(family="Inter"), FontInfo(family="Roboto")])
        brand_b = BrandResponse(fonts=[FontInfo(family="Inter"), FontInfo(family="Roboto")])
        result = self.comparator.compare(brand_a, brand_b)

        fonts_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "fonts")
        assert fonts_comp["match"] is True

    def test_no_fonts_results_in_no_match(self) -> None:
        brand_a = BrandResponse(fonts=[])
        brand_b = BrandResponse(fonts=[])
        result = self.comparator.compare(brand_a, brand_b)

        fonts_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "fonts")
        # No fonts -> no union -> similarity 0.0
        assert fonts_comp["match"] is False

    def test_empty_string_fields_not_counted_as_match(self) -> None:
        """Two empty taglines should not count as a match (bool(val_a) is False)."""
        brand_a = BrandResponse(tagline="")
        brand_b = BrandResponse(tagline="")
        result = self.comparator.compare(brand_a, brand_b)

        tagline_comp = next(fc for fc in result["field_comparisons"] if fc["field"] == "tagline")
        assert tagline_comp["match"] is False

    def test_result_keys(self) -> None:
        brand_a = BrandResponse()
        brand_b = BrandResponse()
        result = self.comparator.compare(brand_a, brand_b)

        assert "overall_similarity" in result
        assert "field_comparisons" in result
        assert "color_distance" in result

    def test_overall_similarity_bounded(self) -> None:
        brand_a = BrandResponse(name="X", primary_color="#000000")
        brand_b = BrandResponse(name="Y", primary_color="#FFFFFF")
        result = self.comparator.compare(brand_a, brand_b)

        assert 0.0 <= result["overall_similarity"] <= 1.0
