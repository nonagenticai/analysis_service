"""Tests for PromptBuilder."""

import pytest

from src.domains.analyze.services.prompt_builder import PromptBuilder


@pytest.fixture
def builder():
    """Create a PromptBuilder with default templates."""
    return PromptBuilder()


@pytest.fixture
def sample_pages():
    return [
        {
            "title": "Homepage",
            "url": "https://example.com/",
            "page_type": "homepage",
            "word_count": 150,
            "interactive_elements": [{"tag": "button"}],
            "headings": ["Welcome", "Features", "About"],
        },
        {
            "title": "Pricing",
            "url": "https://example.com/pricing",
            "page_type": "pricing",
            "word_count": 80,
            "interactive_elements": [],
            "headings": ["Plans"],
        },
    ]


class TestPromptBuilder:
    def test_renders_basic_prompt(self, builder, sample_pages):
        """Template renders with pages and no user context."""
        result = builder.build_site_prompt(sample_pages)
        assert "Homepage" in result
        assert "Pricing" in result
        assert "JSON response" in result
        assert "User Context" not in result

    def test_renders_with_user_context(self, builder, sample_pages):
        """Template includes user context when provided."""
        ctx = {
            "description": "A SaaS product",
            "site_type": "saas",
            "target_audience": "developers",
        }
        result = builder.build_site_prompt(sample_pages, user_context=ctx)
        assert "User Context" in result
        assert "A SaaS product" in result
        assert "saas" in result
        assert "developers" in result

    def test_context_without_fields_excluded(self, builder, sample_pages):
        """Empty context dict is not rendered."""
        ctx = {"description": "", "site_type": "", "target_audience": ""}
        result = builder.build_site_prompt(sample_pages, user_context=ctx)
        assert "User Context" not in result

    def test_page_truncation(self, builder):
        """Only max_pages pages are included."""
        pages = [
            {
                "title": f"Page {i}",
                "url": f"https://example.com/p{i}",
                "page_type": "unknown",
                "word_count": 10,
                "interactive_elements": [],
                "headings": [],
            }
            for i in range(50)
        ]
        result = builder.build_site_prompt(pages, max_pages=5)
        assert "Page 0" in result
        assert "Page 4" in result
        assert "Page 5" not in result

    def test_missing_optional_fields(self, builder):
        """Pages with missing optional fields render gracefully."""
        pages = [{"url": "https://example.com/bare"}]
        result = builder.build_site_prompt(pages)
        assert "example.com/bare" in result

    def test_headings_truncated(self, builder):
        """Only first 20 headings shown per page."""
        pages = [
            {
                "title": "Test",
                "url": "https://example.com/",
                "page_type": "homepage",
                "word_count": 100,
                "interactive_elements": [],
                "headings": [f"H{i}" for i in range(25)],
            }
        ]
        result = builder.build_site_prompt(pages)
        assert "H19" in result
        assert "H20" not in result

    def test_custom_template_dir(self, tmp_path):
        """PromptBuilder works with custom template directory."""
        tpl_dir = tmp_path / "analysis"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "site_analysis.md").write_text("Custom: {{ pages | length }} pages")
        builder = PromptBuilder(template_dir=tmp_path)
        result = builder.build_site_prompt([{"url": "https://a.com"}])
        assert "Custom: 1 pages" in result
