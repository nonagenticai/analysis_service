"""Tests for color_service."""

import pytest

from src.domains.brand.services.color_service import (
    _build_color_info,
    contrast_ratio,
    darken_hex,
    generate_palette,
    hex_to_hsl,
    hex_to_rgb,
    hsl_to_hex,
    lighten_hex,
    recommend_text_color,
    rgb_to_hex,
)


class TestHexToRgb:
    def test_valid_6char_hex(self) -> None:
        assert hex_to_rgb("#ff8800") == (255, 136, 0)

    def test_valid_3char_hex(self) -> None:
        assert hex_to_rgb("#fff") == (255, 255, 255)

    def test_without_hash_prefix(self) -> None:
        assert hex_to_rgb("ff0000") == (255, 0, 0)

    def test_invalid_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            hex_to_rgb("xyz")

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            hex_to_rgb("")

    def test_8char_hex_strips_alpha(self) -> None:
        # #3b82f680 → strip alpha → #3b82f6
        assert hex_to_rgb("#3b82f680") == (59, 130, 246)

    def test_4char_rgba_shorthand(self) -> None:
        # #f0fa → expand RGB (ff00ff), drop alpha
        assert hex_to_rgb("#f0fa") == (255, 0, 255)


class TestRgbToHex:
    def test_known_values(self) -> None:
        assert rgb_to_hex(255, 136, 0) == "#ff8800"

    def test_black(self) -> None:
        assert rgb_to_hex(0, 0, 0) == "#000000"

    def test_white(self) -> None:
        assert rgb_to_hex(255, 255, 255) == "#ffffff"


class TestHexHslRoundtrip:
    def test_red_roundtrip(self) -> None:
        h, s, lt = hex_to_hsl("#ff0000")
        assert h == 0
        assert s == 100
        assert lt == 50
        assert hsl_to_hex(h, s, lt) == "#ff0000"

    def test_pure_green(self) -> None:
        h, s, lt = hex_to_hsl("#00ff00")
        assert h == 120
        assert s == 100
        assert lt == 50
        assert hsl_to_hex(h, s, lt) == "#00ff00"

    def test_pure_blue(self) -> None:
        h, s, lt = hex_to_hsl("#0000ff")
        assert h == 240
        assert s == 100
        assert lt == 50
        assert hsl_to_hex(h, s, lt) == "#0000ff"

    def test_gray(self) -> None:
        h, s, lt = hex_to_hsl("#808080")
        assert s == 0  # gray has zero saturation
        result = hsl_to_hex(h, s, lt)
        # Gray roundtrip should stay gray (hue is irrelevant at s=0)
        r, g, b = hex_to_rgb(result)
        assert abs(r - 128) <= 1
        assert abs(g - 128) <= 1
        assert abs(b - 128) <= 1


class TestDarkenHex:
    def test_darkens_white(self) -> None:
        result = darken_hex("#ffffff")
        assert result == "#cccccc"

    def test_black_stays_black(self) -> None:
        assert darken_hex("#000000") == "#000000"

    def test_custom_factor(self) -> None:
        result = darken_hex("#ffffff", factor=0.5)
        # 255 * 0.5 = 127 -> int(127.5) = 127 -> #7f7f7f
        assert result == "#7f7f7f"

    def test_invalid_input_returns_original(self) -> None:
        assert darken_hex("not-a-color") == "not-a-color"

    def test_3char_hex(self) -> None:
        # #fff -> (255,255,255) * 0.8 = (204,204,204) -> #cccccc
        result = darken_hex("#fff")
        assert result == "#cccccc"


class TestLightenHex:
    def test_lightens_dark_color(self) -> None:
        result = lighten_hex("#555555")
        # 85 * 1.2 = 102 -> #666666
        assert result == "#666666"

    def test_clamps_to_255(self) -> None:
        result = lighten_hex("#ffffff", factor=2.0)
        # 255 * 2 = 510 -> clamped to 255
        assert result == "#ffffff"


class TestContrastRatio:
    def test_black_vs_white(self) -> None:
        ratio = contrast_ratio("#000000", "#ffffff")
        assert ratio == 21.0

    def test_same_color(self) -> None:
        ratio = contrast_ratio("#123456", "#123456")
        assert ratio == 1.0


class TestRecommendTextColor:
    def test_white_bg_returns_black_text(self) -> None:
        assert recommend_text_color("#ffffff") == "#000000"

    def test_black_bg_returns_white_text(self) -> None:
        assert recommend_text_color("#000000") == "#ffffff"

    def test_mid_gray(self) -> None:
        # Mid-gray (#808080) has luminance ~0.216
        # Contrast with white: (1+0.05)/(0.216+0.05) ~ 3.95
        # Contrast with black: (0.216+0.05)/(0+0.05) ~ 5.32
        # Black text should be recommended
        result = recommend_text_color("#808080")
        assert result == "#000000"


class TestBuildColorInfo:
    def test_structure_and_wcag_flags(self) -> None:
        info = _build_color_info("#000080", "primary")
        assert info["hex"] == "#000080"
        assert info["role"] == "primary"
        assert "contrast_ratio_white" in info
        assert "contrast_ratio_black" in info
        # Dark navy blue against white should have high contrast -> AA pass
        assert info["wcag_aa_text"] is True
        assert isinstance(info["wcag_aaa_text"], bool)

    def test_light_color_fails_wcag(self) -> None:
        info = _build_color_info("#ffff00", "accent")
        # Bright yellow against white has poor contrast
        assert info["wcag_aa_text"] is False
        assert info["wcag_aaa_text"] is False


class TestGeneratePalette:
    def test_returns_all_expected_keys(self) -> None:
        palette = generate_palette("#3366cc")
        expected_keys = {
            "primary",
            "secondary",
            "accent",
            "complementary",
            "analogous",
            "light_variant",
            "dark_variant",
            "recommended_text_color",
            "colors",
        }
        assert set(palette.keys()) == expected_keys

    def test_complementary_is_180_degrees(self) -> None:
        primary_hex = "#ff0000"  # hue=0
        palette = generate_palette(primary_hex)
        comp_hex = palette["complementary"]["hex"]
        h, _s, _lt = hex_to_hsl(comp_hex)
        # Complementary of red (hue 0) should be cyan (hue 180)
        assert abs(h - 180) <= 1

    def test_colors_list_length(self) -> None:
        palette = generate_palette("#3366cc")
        # primary, secondary, accent, complementary, 2x analogous, light, dark = 8
        assert len(palette["colors"]) == 8

    def test_recommended_text_color_present(self) -> None:
        palette = generate_palette("#3366cc")
        assert palette["recommended_text_color"] in ("#000000", "#ffffff")
