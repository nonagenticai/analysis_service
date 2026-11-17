"""Prompt template loader for review generation.

Loads Jinja2 templates from src/templates/reviews/ at startup,
caches them, and provides render(review_type, context) method.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "reviews"


class PromptTemplateLoader:
    """Loads and renders Jinja2 prompt templates for review generation."""

    def __init__(self, templates_dir: Path | str | None = None) -> None:
        self._templates_dir = Path(templates_dir) if templates_dir else _TEMPLATES_DIR
        if not self._templates_dir.exists():
            logger.warning("Templates directory not found: %s", self._templates_dir)
        # Autoescape disabled: templates render LLM prompts, not browser HTML; autoescape would corrupt text
        self._env = Environment(  # nosec B701
            loader=FileSystemLoader(str(self._templates_dir)),
            autoescape=False,  # noqa: S701 — LLM prompts, not user-facing HTML
            keep_trailing_newline=True,
        )
        logger.info("Prompt templates loaded from %s", self._templates_dir)

    def render(self, review_type: str, context: dict) -> str:
        """Render a prompt template for the given review type.

        Args:
            review_type: e.g. "design_review", "usability_review"
            context: Template variables dict.

        Returns:
            Rendered prompt string.

        Raises:
            TemplateNotFound: If template file doesn't exist.
        """
        template_name = f"{review_type}.md"
        try:
            template = self._env.get_template(template_name)
            return template.render(**context)
        except TemplateNotFound:
            logger.error("Template not found: %s", template_name)
            raise

    def has_template(self, review_type: str) -> bool:
        """Check if a template exists for the given review type."""
        template_name = f"{review_type}.md"
        try:
            self._env.get_template(template_name)
            return True
        except TemplateNotFound:
            return False

    def list_templates(self) -> list[str]:
        """List available template names (without .md extension)."""
        return [p.stem for p in self._templates_dir.glob("*.md") if p.is_file()]
