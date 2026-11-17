"""Jinja2-based prompt builder for LLM interactions."""

from __future__ import annotations

import logging
from pathlib import Path

import jinja2

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


class PromptBuilder:
    """Build LLM prompts from Jinja2 templates."""

    def __init__(self, template_dir: Path | str | None = None) -> None:
        tdir = Path(template_dir) if template_dir else _DEFAULT_TEMPLATE_DIR
        # Autoescape disabled: templates render plain-text LLM prompts, not HTML.
        self.env = jinja2.Environment(  # nosec B701
            loader=jinja2.FileSystemLoader(str(tdir)),
            autoescape=False,  # noqa: S701 — LLM prompts, not user-facing HTML
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build_site_prompt(
        self,
        pages: list[dict],
        user_context: dict | None = None,
        max_pages: int = 30,
    ) -> str:
        """Render the site analysis prompt template.

        Args:
            pages: List of analyzed page dicts.
            user_context: Optional user-provided context dict.
            max_pages: Maximum number of pages to include in the prompt.

        Returns:
            Rendered prompt string.
        """
        template = self.env.get_template("analysis/site_analysis.md")
        truncated_pages = pages[:max_pages]

        context_block = None
        if user_context:
            context_block = {
                "description": user_context.get("description", ""),
                "site_type": user_context.get("site_type", ""),
                "target_audience": user_context.get("target_audience", ""),
            }
            # Only include if at least one field is set
            if not any(context_block.values()):
                context_block = None

        return template.render(
            pages=truncated_pages,
            context_block=context_block,
        )

    def build_review_prompt(
        self,
        review_type: str,
        pages: list[dict],
        site_analysis: dict | None = None,
        narrative: dict | None = None,
        user_context: dict | None = None,
    ) -> str:
        """Render a review prompt template.

        Args:
            review_type: One of design, usability, feature, content_quality.
            pages: List of analyzed page dicts.
            site_analysis: Optional site analysis dict.
            narrative: Optional narrative extraction dict.
            user_context: Optional user-provided context dict.

        Returns:
            Rendered prompt string.
        """
        template = self.env.get_template(f"reviews/{review_type}_review.md")

        sa = site_analysis or {}
        n = narrative or {}

        return template.render(
            site_description=sa.get("description", "No description available"),
            target_audience=str(sa.get("target_audience", [])),
            tone=sa.get("tone", ""),
            visual_identity=str(sa.get("visual_identity", {})),
            selling_points=str(sa.get("selling_points", [])),
            use_cases=str(sa.get("use_cases", [])),
            narrative_hooks=str(n.get("hooks", [])),
            pages=self._format_pages_for_review(pages),
        )

    def _format_pages_for_review(self, pages: list[dict], max_pages: int = 30) -> str:
        """Format page data for review templates.

        Args:
            pages: List of analyzed page dicts.
            max_pages: Maximum number of pages to include.

        Returns:
            Formatted markdown string of page summaries.
        """
        lines = []
        for p in pages[:max_pages]:
            title = p.get("title", p.get("url", "Unknown"))
            ptype = p.get("page_type", "unknown")
            wc = p.get("word_count", 0)
            headings = p.get("headings", [])[:5]
            lines.append(f"- **{title}** ({ptype}): {wc} words, headings: {', '.join(headings)}")
        return "\n".join(lines)
