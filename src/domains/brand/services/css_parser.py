"""CSS brand parser.

Extracts brand colors and typography from external CSS stylesheets.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from src.domains.brand.schemas import FontInfo
from src.domains.brand.security import (
    BrandResponseTooLargeError,
    BrandSsrfBlockedError,
    get_guarded,
)

logger = logging.getLogger(__name__)

# Byte cap for a single stylesheet. A stylesheet href is lifted out of an
# attacker-influenced page and up to ten are fetched concurrently, so without a
# cap one hostile origin can make the pod hold ten unbounded bodies. Real-world
# CSS bundles are tens to low hundreds of KB even unminified; 2 MB is generous
# headroom for the worst legitimate case and still bounds the hostile one.
_MAX_CSS_BYTES = 2 * 1024 * 1024

# CSS custom property patterns for brand colors
_COLOR_VAR_PATTERN = re.compile(
    r"--(?:primary|brand|main|accent|theme)[-_]?color\s*:\s*(#[0-9a-fA-F]{3,8})",
    re.IGNORECASE,
)

# Named color properties
_COLOR_PROP_PATTERN = re.compile(
    r"(?:background-color|color|border-color)\s*:\s*(#[0-9a-fA-F]{3,8})",
    re.IGNORECASE,
)

# Google Fonts @import pattern
_GOOGLE_FONTS_IMPORT = re.compile(
    r'@import\s+url\(["\']?(https://fonts\.googleapis\.com/css2?\?[^"\')\s]+)["\']?\)',
    re.IGNORECASE,
)

# Google Fonts family extraction from URL (name only, stops at colon/ampersand)
_GOOGLE_FONTS_FAMILY = re.compile(
    r"family=([^&:;]+)",
    re.IGNORECASE,
)

# Google Fonts family extraction including weight specs (e.g. family=Roboto:wght@400;700)
_GOOGLE_FONTS_FAMILY_FULL = re.compile(
    r"family=([^&]+)",
    re.IGNORECASE,
)

# Font-family declaration pattern
_FONT_FAMILY_PATTERN = re.compile(
    r"font-family\s*:\s*([^;}{]+)",
    re.IGNORECASE,
)

# Font-weight declaration pattern
_FONT_WEIGHT_PATTERN = re.compile(
    r"font-weight\s*:\s*([^;}{]+)",
    re.IGNORECASE,
)

# Heading selector pattern
_HEADING_SELECTOR = re.compile(r"\b(?:h[1-6])\b", re.IGNORECASE)

# Body/paragraph selector pattern
_BODY_SELECTOR = re.compile(r"\b(?:body|^p\b|\.text|\.body)", re.IGNORECASE)

# Near-white colors to skip (too close to white)
_NEAR_WHITE_THRESHOLD = 240  # RGB each component > this = skip
# Near-black colors to skip
_NEAR_BLACK_THRESHOLD = 15  # RGB each component < this = skip


def _is_generic_color(hex_color: str) -> bool:
    """Check if a color is near-white or near-black (not useful as brand color)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except ValueError, IndexError:
        return True
    if r > _NEAR_WHITE_THRESHOLD and g > _NEAR_WHITE_THRESHOLD and b > _NEAR_WHITE_THRESHOLD:
        return True
    return bool(
        r < _NEAR_BLACK_THRESHOLD and g < _NEAR_BLACK_THRESHOLD and b < _NEAR_BLACK_THRESHOLD
    )


def _clean_font_name(name: str) -> str:
    """Clean a font family name, removing quotes and extra whitespace."""
    return name.strip().strip("'\"").strip()


def _categorize_font(name: str) -> str:
    """Guess CSS font category from name."""
    lower = name.lower()
    if any(kw in lower for kw in ("mono", "code", "courier", "consolas")):
        return "monospace"
    if (
        any(kw in lower for kw in ("serif", "georgia", "times", "garamond", "palatino"))
        and "sans" not in lower
    ):
        return "serif"
    if any(kw in lower for kw in ("script", "cursive", "handwriting", "dancing", "pacifico")):
        return "handwriting"
    if any(kw in lower for kw in ("display", "impact", "lobster", "playfair")):
        return "display"
    return "sans-serif"


# Body/html background-color pattern
_BODY_BG_PATTERN = re.compile(
    r"(?:^|\})\s*(?:body|html)\s*\{[^}]*?background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})",
    re.IGNORECASE | re.MULTILINE,
)


def extract_background_color(css_text: str) -> str | None:
    """Extract body/html background-color from CSS text.

    Returns the hex color string if found, otherwise None.
    """
    match = _BODY_BG_PATTERN.search(css_text)
    if match:
        return match.group(1).lower()
    return None


def extract_background_color_from_html(soup: Any) -> str | None:
    """Extract body background-color from inline <style> blocks in HTML.

    Args:
        soup: BeautifulSoup parsed HTML document.

    Returns the hex color string if found, otherwise None.
    """
    for style in soup.find_all("style"):
        css = style.string or ""
        bg = extract_background_color(css)
        if bg:
            return bg
    return None


class CSSBrandParser:
    """Extracts brand colors and typography from CSS."""

    async def extract_from_url(
        self, css_urls: list[str], base_url: str
    ) -> tuple[list[str], list[FontInfo], str | None]:
        """Fetch CSS URLs and extract colors, fonts, and background color.

        Args:
            css_urls: List of CSS stylesheet URLs to fetch.
            base_url: Base URL for resolving relative paths.

        Returns:
            Tuple of (colors list, fonts list, background_color or None).
        """
        import asyncio
        import time as _time

        if not css_urls:
            return [], [], None

        fetch_start = _time.monotonic()
        limited_urls = css_urls[:10]
        logger.debug("Fetching %d CSS stylesheets for brand extraction", len(limited_urls))
        tasks = [self._fetch_css(url) for url in limited_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        fetch_ms = int((_time.monotonic() - fetch_start) * 1000)
        success_count = sum(1 for r in results if isinstance(r, str) and r)
        logger.debug(
            "CSS fetch completed, total=%d, success=%d, duration_ms=%d",
            len(limited_urls),
            success_count,
            fetch_ms,
        )

        combined_css = ""
        for result in results:
            if isinstance(result, str):
                combined_css += "\n" + result

        colors = self._extract_colors_from_css(combined_css)
        fonts = self._extract_fonts_from_css(combined_css)
        bg_color = extract_background_color(combined_css)

        # Also detect Google Fonts from link URLs
        google_fonts = self._detect_google_fonts_with_weights(combined_css, css_urls)
        # Add Google Fonts not already in fonts list
        existing_families = {f.family.lower() for f in fonts}
        for gf_name, gf_weights in google_fonts.items():
            if gf_name.lower() not in existing_families:
                fonts.append(
                    FontInfo(
                        family=gf_name,
                        category=_categorize_font(gf_name),
                        role="",
                        weights=gf_weights,
                        is_google_font=True,
                        source_url=f"https://fonts.googleapis.com/css2?family={gf_name.replace(' ', '+')}",
                    )
                )

        return colors, fonts, bg_color

    async def _fetch_css(self, url: str) -> str:
        """Fetch a single CSS file, SSRF-gated per hop. Empty string on failure.

        ``url`` is a stylesheet ``href`` lifted out of the page the tenant asked
        us to render, so it is attacker-controlled even when the page itself is
        public. A block is fail-soft for the pipeline (this is a best-effort
        signal) but fail-closed for the request: nothing is fetched.

        The body is capped at ``_MAX_CSS_BYTES`` *while streaming*; an oversized
        stylesheet is a degraded signal, not an error worth failing the run for,
        so it takes the same fail-soft ``""`` as a timeout.
        """
        try:
            async with httpx.AsyncClient(
                timeout=3.0,
                follow_redirects=False,
                headers={"User-Agent": "BrandExtractorService/1.0"},
            ) as client:
                response = await get_guarded(client, url, max_bytes=_MAX_CSS_BYTES)
                response.raise_for_status()
                return response.text
        except BrandSsrfBlockedError as e:
            logger.warning("Blocked CSS fetch: %s", e)
            return ""
        except BrandResponseTooLargeError as e:
            logger.warning("Oversized CSS fetch from %s: %s", url, e)
            return ""
        except (OSError, httpx.HTTPError) as e:
            logger.debug("Failed to fetch CSS from %s: %s", url, e)
            return ""

    def _extract_colors_from_css(self, css_text: str) -> list[str]:
        """Extract brand colors from CSS custom properties and declarations.

        Prioritizes CSS custom properties (--primary-color, --brand-color, etc.)
        then falls back to common color declarations. Deduplicates and filters
        generic near-white/near-black colors.
        """
        colors: list[str] = []
        seen: set[str] = set()

        # Priority 1: CSS custom properties for brand colors
        for match in _COLOR_VAR_PATTERN.finditer(css_text):
            color = match.group(1).lower()
            if color not in seen and not _is_generic_color(color):
                colors.append(color)
                seen.add(color)

        # Priority 2: General color declarations (only if we found none above)
        if not colors:
            for match in _COLOR_PROP_PATTERN.finditer(css_text):
                color = match.group(1).lower()
                if color not in seen and not _is_generic_color(color):
                    colors.append(color)
                    seen.add(color)
                    if len(colors) >= 5:  # Limit general colors
                        break

        return colors

    _GENERIC_FONTS = frozenset(
        {
            "serif",
            "sans-serif",
            "monospace",
            "cursive",
            "fantasy",
            "system-ui",
            "ui-serif",
            "ui-sans-serif",
            "ui-monospace",
            "inherit",
            "initial",
            "unset",
        }
    )

    @staticmethod
    def _selector_role(selector: str) -> str:
        if _HEADING_SELECTOR.search(selector):
            return "heading"
        if _BODY_SELECTOR.search(selector):
            return "body"
        return ""

    def _process_font_block(
        self,
        selector: str,
        declarations: str,
        fonts: list[FontInfo],
        seen_families: set[str],
        family_weights: dict[str, list[str]],
    ) -> None:
        """Process a single ``selector { declarations }`` block, mutating outputs."""
        font_match = _FONT_FAMILY_PATTERN.search(declarations)
        if not font_match:
            return
        weight_match = _FONT_WEIGHT_PATTERN.search(declarations)
        weight_value = weight_match.group(1).strip() if weight_match else None

        for family_part in font_match.group(1).split(","):
            family = _clean_font_name(family_part)
            if not family or family.lower() in self._GENERIC_FONTS:
                continue
            family_key = family.lower()
            if weight_value:
                weights_list = family_weights.setdefault(family_key, [])
                if weight_value not in weights_list:
                    weights_list.append(weight_value)
            if family_key in seen_families:
                break
            seen_families.add(family_key)
            fonts.append(
                FontInfo(
                    family=family,
                    category=_categorize_font(family),
                    role=self._selector_role(selector),
                    weights=[weight_value] if weight_value else [],
                    is_google_font=False,
                )
            )
            break  # Only first relevant font from each declaration

    def _extract_fonts_from_css(self, css_text: str) -> list[FontInfo]:
        """Extract font information from CSS declarations.

        Identifies heading fonts (h1-h6 selectors) and body fonts (body/p selectors).
        Also captures font-weight values.
        """
        fonts: list[FontInfo] = []
        seen_families: set[str] = set()
        family_weights: dict[str, list[str]] = {}

        for selector, declarations in re.findall(r"([^{}]+)\{([^{}]+)\}", css_text):
            self._process_font_block(
                selector.strip(), declarations, fonts, seen_families, family_weights
            )

        # Merge accumulated weights into fonts
        for font in fonts:
            family_key = font.family.lower()
            for w in family_weights.get(family_key, []):
                if w not in font.weights:
                    font.weights.append(w)

        return fonts

    def _detect_google_fonts(self, css_text: str, link_urls: list[str]) -> set[str]:
        """Detect Google Font families from @import rules and link URLs."""
        return set(self._detect_google_fonts_with_weights(css_text, link_urls).keys())

    @staticmethod
    def _parse_google_family_spec(raw: str) -> tuple[str, list[str]]:
        """Parse a Google Fonts ``family=`` value into ``(name, weights)``."""
        if ":" not in raw:
            return raw, []
        name_part, spec = raw.split(":", 1)
        weight_match = re.search(r"(?:wght@)?(\d[\d;]+)", spec)
        weights = (
            [w for w in weight_match.group(1).split(";") if w.isdigit()] if weight_match else []
        )
        return name_part.strip(), weights

    def _detect_google_fonts_with_weights(
        self, css_text: str, link_urls: list[str]
    ) -> dict[str, list[str]]:
        """Detect Google Font families and weights from @import rules and link URLs.

        Returns dict mapping family name to list of weight strings.
        """
        families: dict[str, list[str]] = {}

        all_urls: list[str] = [m.group(1) for m in _GOOGLE_FONTS_IMPORT.finditer(css_text)]
        all_urls.extend(url for url in link_urls if "fonts.googleapis.com" in url)

        for url in all_urls:
            for fam_match in _GOOGLE_FONTS_FAMILY_FULL.finditer(url):
                raw = fam_match.group(1).replace("+", " ").strip()
                name, weights = self._parse_google_family_spec(raw)
                if not name:
                    continue
                bucket = families.setdefault(name, [])
                for w in weights:
                    if w not in bucket:
                        bucket.append(w)

        return families
