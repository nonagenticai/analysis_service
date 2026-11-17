"""Brand comparison service.

Compares brand identities between two domains using field matching,
Euclidean color distance, and font Jaccard similarity.
"""

from __future__ import annotations

import math

from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.color_service import hex_to_rgb


class BrandComparator:
    """Compare two brand identities and calculate similarity."""

    def compare(self, brand_a: BrandResponse, brand_b: BrandResponse) -> dict:
        """Compare two brands and return a comparison report.

        Args:
            brand_a: First brand to compare.
            brand_b: Second brand to compare.

        Returns:
            Dict with overall_similarity (0-1), field_comparisons list,
            and color_distance (Euclidean RGB).
        """
        field_comparisons = []
        match_count = 0
        total_fields = 0

        # Compare simple string fields
        for field_name in ("name", "primary_color", "secondary_color", "tagline", "brand_voice"):
            val_a = str(getattr(brand_a, field_name, "") or "")
            val_b = str(getattr(brand_b, field_name, "") or "")
            match = val_a == val_b and bool(val_a)
            field_comparisons.append(
                {
                    "field": field_name,
                    "value_a": val_a,
                    "value_b": val_b,
                    "match": match,
                }
            )
            if val_a or val_b:
                total_fields += 1
                if match:
                    match_count += 1

        # Compare logo presence
        field_comparisons.append(
            {
                "field": "has_logo",
                "value_a": str(brand_a.has_logo),
                "value_b": str(brand_b.has_logo),
                "match": brand_a.has_logo == brand_b.has_logo,
            }
        )
        total_fields += 1
        if brand_a.has_logo == brand_b.has_logo:
            match_count += 1

        # Font overlap
        fonts_a = {f.family.lower() for f in brand_a.fonts if f.family}
        fonts_b = {f.family.lower() for f in brand_b.fonts if f.family}
        font_overlap = fonts_a & fonts_b
        font_union = fonts_a | fonts_b
        font_similarity = len(font_overlap) / len(font_union) if font_union else 0.0
        field_comparisons.append(
            {
                "field": "fonts",
                "value_a": ", ".join(sorted(fonts_a)),
                "value_b": ", ".join(sorted(fonts_b)),
                "match": font_similarity > 0.5,
            }
        )
        total_fields += 1
        if font_similarity > 0.5:
            match_count += 1

        # Color distance (Euclidean RGB)
        color_distance = self._color_distance(brand_a.primary_color, brand_b.primary_color)

        # Weighted overall similarity
        field_ratio = match_count / total_fields if total_fields > 0 else 0.0
        # Color similarity: 0 distance = 1.0, 441 (max) = 0.0
        color_sim = max(0.0, 1.0 - (color_distance / 441.67))
        overall = (field_ratio * 0.6) + (color_sim * 0.2) + (font_similarity * 0.2)

        return {
            "overall_similarity": round(overall, 4),
            "field_comparisons": field_comparisons,
            "color_distance": round(color_distance, 4),
        }

    @staticmethod
    def _color_distance(hex_a: str, hex_b: str) -> float:
        """Euclidean distance between two hex colors in RGB space."""
        try:
            r1, g1, b1 = hex_to_rgb(hex_a)
        except ValueError:
            r1, g1, b1 = 0, 0, 0
        try:
            r2, g2, b2 = hex_to_rgb(hex_b)
        except ValueError:
            r2, g2, b2 = 0, 0, 0
        return math.sqrt((r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2)
