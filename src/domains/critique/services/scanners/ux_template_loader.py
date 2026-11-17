"""UX prompt template loader with caching."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "ux_templates"

DIMENSIONS = [
    "navigation",
    "visual_hierarchy",
    "cta_effectiveness",
    "mobile_responsiveness",
    "form_usability",
    "error_handling",
    "trust_signals",
    "value_proposition",
]


@lru_cache(maxsize=16)
def load_template(name: str) -> str | None:
    """Load a template file by name. Returns None if not found."""
    path = TEMPLATE_DIR / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.debug("Template not found: %s", path)
        return None


def build_ux_prompt(html: str, instructions: str | None = None) -> str:
    """Build the full UX analysis prompt from templates.

    Falls back to inline DEFAULT_UX_PROMPT if templates not found.
    """
    system = load_template("system")
    if not system:
        return ""  # Signal to caller to use fallback

    dimension_prompts = []
    for i, dim in enumerate(DIMENSIONS, 1):
        template = load_template(dim)
        if template:
            dim_label = dim.replace("_", " ").title()
            dimension_prompts.append(f"{i}. {dim_label}: {template}")

    if not dimension_prompts:
        return ""

    prompt_parts = [system, "", "Analyze the following dimensions:", ""]
    prompt_parts.extend(dimension_prompts)

    if instructions:
        prompt_parts.extend(["", f"Additional instructions: {instructions}"])

    prompt_parts.extend(["", "HTML to analyze:", html])

    return "\n".join(prompt_parts)
