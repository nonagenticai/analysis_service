"""Tests for src.services.prompt_loader -- Jinja2 template loading."""

import tempfile
from pathlib import Path

import pytest
from jinja2 import TemplateNotFound

from src.domains.review.services.prompt_loader import PromptTemplateLoader


class TestPromptTemplateLoader:
    def test_init_with_missing_directory(self):
        """Should warn but not crash with nonexistent directory."""
        loader = PromptTemplateLoader(templates_dir="/nonexistent/path")
        assert loader is not None

    def test_has_template_false_when_missing(self):
        """has_template should return False for nonexistent templates."""
        loader = PromptTemplateLoader(templates_dir="/nonexistent/path")
        assert loader.has_template("design_review") is False

    def test_has_template_true_when_exists(self):
        """has_template should return True when template file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "design_review.md"
            template_path.write_text("Hello {{ site_description }}")

            loader = PromptTemplateLoader(templates_dir=tmpdir)
            assert loader.has_template("design_review") is True

    def test_render_template(self):
        """render should substitute template variables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "test_review.md"
            template_path.write_text("Review for: {{ site_description }}")

            loader = PromptTemplateLoader(templates_dir=tmpdir)
            result = loader.render("test_review", {"site_description": "Example.com"})
            assert "Review for: Example.com" in result

    def test_render_missing_template_raises(self):
        """render should raise TemplateNotFound for missing templates."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = PromptTemplateLoader(templates_dir=tmpdir)
            with pytest.raises(TemplateNotFound):
                loader.render("nonexistent_review", {})

    def test_list_templates(self):
        """list_templates should return template names without .md extension."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "design_review.md").write_text("test")
            (Path(tmpdir) / "usability_review.md").write_text("test")
            (Path(tmpdir) / "not_a_template.txt").write_text("test")

            loader = PromptTemplateLoader(templates_dir=tmpdir)
            templates = loader.list_templates()
            assert "design_review" in templates
            assert "usability_review" in templates
            assert "not_a_template" not in templates

    def test_list_templates_empty_dir(self):
        """list_templates should return empty list for empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = PromptTemplateLoader(templates_dir=tmpdir)
            assert loader.list_templates() == []

    def test_render_with_complex_context(self):
        """Template should handle complex context variables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            template_path = Path(tmpdir) / "complex_review.md"
            template_path.write_text("Pages: {{ pages }}\nGoals: {{ business_goals }}")

            loader = PromptTemplateLoader(templates_dir=tmpdir)
            result = loader.render(
                "complex_review",
                {"pages": "- Page 1\n- Page 2", "business_goals": "- Goal 1"},
            )
            assert "Page 1" in result
            assert "Goal 1" in result
