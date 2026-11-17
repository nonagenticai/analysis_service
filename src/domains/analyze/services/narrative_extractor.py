"""Extract narrative material from analyzed pages."""

from __future__ import annotations

MAX_HOOKS = 10
MAX_KEY_QUOTES = 5
MAX_FEATURE_HIGHLIGHTS = 10
MIN_HEADING_LENGTH = 10
MAX_HEADING_LENGTH = 80
MAX_FEATURE_PREVIEW_LENGTH = 100
MAX_QUOTE_PREVIEW_LENGTH = 150


class NarrativeExtractor:
    """Extract narrative hooks, key quotes, and feature highlights from page analyses."""

    def extract(self, pages: list[dict]) -> dict:
        """Extract narrative hooks, key quotes, and feature highlights.

        Args:
            pages: List of analyzed page dicts with headings and sections.

        Returns:
            Dict with hooks, key_quotes, feature_highlights, and page stats.
        """
        hooks: list[str] = []
        key_quotes: list[str] = []
        feature_highlights: list[str] = []

        for page in pages:
            for heading in page.get("headings", []):
                if MIN_HEADING_LENGTH < len(heading) < MAX_HEADING_LENGTH:
                    hooks.append(heading)

            for section in page.get("content_sections", []):
                section_type = section.get("section_type", "")
                if section_type == "features":
                    if section.get("heading"):
                        feature_highlights.append(section["heading"])
                    elif section.get("text_preview"):
                        first_sentence = section["text_preview"].split(".")[0].strip()
                        if first_sentence:
                            feature_highlights.append(first_sentence[:MAX_FEATURE_PREVIEW_LENGTH])
                if section_type == "testimonials" and section.get("text_preview"):
                    key_quotes.append(section["text_preview"][:MAX_QUOTE_PREVIEW_LENGTH])

        return {
            "hooks": hooks[:MAX_HOOKS],
            "key_quotes": key_quotes[:MAX_KEY_QUOTES],
            "feature_highlights": feature_highlights[:MAX_FEATURE_HIGHLIGHTS],
            "total_pages": len(pages),
            "pages_with_forms": sum(1 for p in pages if p.get("has_forms")),
            "pages_with_media": sum(1 for p in pages if p.get("has_media")),
        }
