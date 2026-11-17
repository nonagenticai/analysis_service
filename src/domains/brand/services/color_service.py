"""Color utilities for brand extraction.

Pure-function color manipulation with zero external dependencies.
"""

from __future__ import annotations

import colorsys
import math
import re


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple. Handles 3 and 6 char hex.

    Raises ValueError on invalid input.
    """
    if not hex_color or not isinstance(hex_color, str):
        raise ValueError(f"Invalid hex color: {hex_color!r}")

    cleaned = hex_color.lstrip("#")

    # Expand shorthand / strip alpha channel
    if len(cleaned) == 3:
        cleaned = "".join(c * 2 for c in cleaned)
    elif len(cleaned) == 4:
        # RGBA shorthand — expand RGB and drop alpha
        cleaned = "".join(c * 2 for c in cleaned[:3])
    elif len(cleaned) == 8:
        # RRGGBBAA — drop alpha channel
        cleaned = cleaned[:6]

    if len(cleaned) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", cleaned):
        raise ValueError(f"Invalid hex color: {hex_color!r}")

    r = int(cleaned[0:2], 16)
    g = int(cleaned[2:4], 16)
    b = int(cleaned[4:6], 16)
    return (r, g, b)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB values to hex string."""
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    """Convert hex to HSL (hue 0-360, saturation 0-100, lightness 0-100)."""
    r, g, b = hex_to_rgb(hex_color)
    # colorsys expects 0-1 range
    h, lt, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    # h is 0-1 -> 0-360, s and lt are 0-1 -> 0-100
    return (round(h * 360, 2), round(s * 100, 2), round(lt * 100, 2))


def hsl_to_hex(h: float, s: float, lt: float) -> str:
    """Convert HSL (hue 0-360, saturation 0-100, lightness 0-100) to hex."""
    # Normalize to 0-1 range for colorsys
    h_norm = h / 360.0
    s_norm = s / 100.0
    l_norm = lt / 100.0
    # colorsys uses HLS order
    r, g, b = colorsys.hls_to_rgb(h_norm, l_norm, s_norm)
    return rgb_to_hex(round(r * 255), round(g * 255), round(b * 255))


def darken_hex(hex_color: str, factor: float = 0.8) -> str:
    """Darken a hex color by a given factor.

    Port of existing _darken_hex(). Returns original color string on invalid
    input (graceful degradation).
    """
    try:
        r, g, b = hex_to_rgb(hex_color)
    except ValueError:
        return hex_color

    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return rgb_to_hex(r, g, b)


def lighten_hex(hex_color: str, factor: float = 1.2) -> str:
    """Lighten a hex color by a given factor.

    Returns original color string on invalid input (graceful degradation).
    """
    try:
        r, g, b = hex_to_rgb(hex_color)
    except ValueError:
        return hex_color

    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return rgb_to_hex(r, g, b)


def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG 2.1 relative luminance. sRGB values 0-255."""

    def linearize(channel: int) -> float:
        c = channel / 255.0
        if c <= 0.04045:
            return c / 12.92
        return math.pow((c + 0.055) / 1.055, 2.4)

    r_lin = linearize(r)
    g_lin = linearize(g)
    b_lin = linearize(b)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(color1: str, color2: str) -> float:
    """WCAG 2.1 contrast ratio between two hex colors.

    Returns value between 1 and 21.
    """
    r1, g1, b1 = hex_to_rgb(color1)
    r2, g2, b2 = hex_to_rgb(color2)

    lum1 = _relative_luminance(r1, g1, b1)
    lum2 = _relative_luminance(r2, g2, b2)

    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)

    return round((lighter + 0.05) / (darker + 0.05), 2)


def recommend_text_color(bg_hex: str) -> str:
    """Returns '#000000' or '#ffffff' based on which has better contrast with bg."""
    ratio_white = contrast_ratio(bg_hex, "#ffffff")
    ratio_black = contrast_ratio(bg_hex, "#000000")

    if ratio_black >= ratio_white:
        return "#000000"
    return "#ffffff"


def _build_color_info(hex_color: str, role: str, background_color: str = "#ffffff") -> dict:
    """Build a color info dict with WCAG AA/AAA compliance flags.

    Returns dict with keys: hex, role, contrast_ratio_white, contrast_ratio_black,
    wcag_aa_text, wcag_aaa_text.

    When a background_color is provided, WCAG AA/AAA compliance is evaluated
    against that background instead of hardcoded white (#ffffff).
    """
    cr_white = contrast_ratio(hex_color, "#ffffff")
    cr_black = contrast_ratio(hex_color, "#000000")

    # Use actual background color for WCAG compliance checks
    cr_bg = contrast_ratio(hex_color, background_color)

    return {
        "hex": hex_color,
        "role": role,
        "contrast_ratio_white": cr_white,
        "contrast_ratio_black": cr_black,
        "wcag_aa_text": cr_bg >= 4.5,
        "wcag_aaa_text": cr_bg >= 7.0,
    }


def generate_palette(primary_hex: str, background_color: str = "#ffffff") -> dict:
    """Generate a full color palette from a primary color.

    Args:
        primary_hex: Primary brand color in hex format.
        background_color: Actual page background color for WCAG contrast
            calculations. Defaults to white (#ffffff).

    Returns dict with keys: primary, secondary, accent, complementary,
    analogous, light_variant, dark_variant, recommended_text_color, colors.
    """
    h, s, lt = hex_to_hsl(primary_hex)

    primary_info = _build_color_info(primary_hex, "primary", background_color)

    secondary_hex = darken_hex(primary_hex, 0.7)
    secondary_info = _build_color_info(secondary_hex, "secondary", background_color)

    accent_hue = (h + 60) % 360
    accent_hex = hsl_to_hex(accent_hue, s, lt)
    accent_info = _build_color_info(accent_hex, "accent", background_color)

    complementary_hue = (h + 180) % 360
    complementary_hex = hsl_to_hex(complementary_hue, s, lt)
    complementary_info = _build_color_info(complementary_hex, "complementary", background_color)

    analogous_hue_1 = (h - 30) % 360
    analogous_hue_2 = (h + 30) % 360
    analogous_hex_1 = hsl_to_hex(analogous_hue_1, s, lt)
    analogous_hex_2 = hsl_to_hex(analogous_hue_2, s, lt)
    analogous_info = [
        _build_color_info(analogous_hex_1, "analogous", background_color),
        _build_color_info(analogous_hex_2, "analogous", background_color),
    ]

    light_hex = lighten_hex(primary_hex, 1.3)
    light_info = _build_color_info(light_hex, "light", background_color)

    dark_hex = darken_hex(primary_hex, 0.6)
    dark_info = _build_color_info(dark_hex, "dark", background_color)

    text_color = recommend_text_color(primary_hex)

    all_colors = [
        primary_info,
        secondary_info,
        accent_info,
        complementary_info,
        *analogous_info,
        light_info,
        dark_info,
    ]

    return {
        "primary": primary_info,
        "secondary": secondary_info,
        "accent": accent_info,
        "complementary": complementary_info,
        "analogous": analogous_info,
        "light_variant": light_info,
        "dark_variant": dark_info,
        "recommended_text_color": text_color,
        "colors": all_colors,
    }
