"""Tests for NarrativeExtractor."""

from src.domains.analyze.services.narrative_extractor import NarrativeExtractor


class TestNarrativeExtractor:
    def setup_method(self):
        self.extractor = NarrativeExtractor()

    def test_extract_hooks_from_headings(self):
        """Should extract headings between 10 and 80 chars as hooks."""
        pages = [
            {
                "headings": ["Short", "A Compelling Headline That Works", "Another Good One Here"],
                "content_sections": [],
                "has_forms": False,
                "has_media": False,
            }
        ]
        result = self.extractor.extract(pages)
        assert "A Compelling Headline That Works" in result["hooks"]
        assert "Short" not in result["hooks"]

    def test_extract_feature_highlights(self):
        """Should extract features section headings."""
        pages = [
            {
                "headings": [],
                "content_sections": [
                    {"section_type": "features", "heading": "Fast Processing", "text_preview": ""}
                ],
                "has_forms": False,
                "has_media": False,
            }
        ]
        result = self.extractor.extract(pages)
        assert "Fast Processing" in result["feature_highlights"]

    def test_extract_key_quotes(self):
        """Should extract testimonial text as key quotes."""
        pages = [
            {
                "headings": [],
                "content_sections": [
                    {
                        "section_type": "testimonials",
                        "heading": "",
                        "text_preview": "This product changed our workflow completely.",
                    }
                ],
                "has_forms": False,
                "has_media": False,
            }
        ]
        result = self.extractor.extract(pages)
        assert len(result["key_quotes"]) == 1
        assert "changed our workflow" in result["key_quotes"][0]

    def test_empty_pages(self):
        """Should handle empty page list."""
        result = self.extractor.extract([])
        assert result["hooks"] == []
        assert result["total_pages"] == 0

    def test_pages_with_forms_count(self):
        """Should count pages with forms."""
        pages = [
            {"headings": [], "content_sections": [], "has_forms": True, "has_media": False},
            {"headings": [], "content_sections": [], "has_forms": False, "has_media": True},
            {"headings": [], "content_sections": [], "has_forms": True, "has_media": True},
        ]
        result = self.extractor.extract(pages)
        assert result["pages_with_forms"] == 2
        assert result["pages_with_media"] == 2

    def test_feature_from_text_preview(self):
        """Features without heading should use text_preview first sentence."""
        pages = [
            {
                "headings": [],
                "content_sections": [
                    {
                        "section_type": "features",
                        "heading": "",
                        "text_preview": "Lightning fast processing. Built for scale.",
                    }
                ],
                "has_forms": False,
                "has_media": False,
            }
        ]
        result = self.extractor.extract(pages)
        assert "Lightning fast processing" in result["feature_highlights"]
