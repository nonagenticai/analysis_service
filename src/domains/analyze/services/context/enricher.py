"""Track user-set vs auto-set fields and enrich context progressively."""

from __future__ import annotations

import logging
from typing import Any

from src.domains.analyze.services.context.types import (
    EnrichedContext,
    UserContextInput,
)

logger = logging.getLogger(__name__)

# Fields with these default values are considered "not set"
_DEFAULTS: dict[str, Any] = {
    "description": "",
    "site_type": "",
    "target_audience": "",
    "goals": [],
    "focus_pages": [],
    "exclude_pages": [],
    "feature_priorities": [],
    "critique_focus": [],
}


class ContextEnricher:
    """Tracks which context fields are user-set vs auto-set.

    User-provided values are immutable -- only auto-generated fields
    can be overwritten by enrichment.

    Attributes:
        _context: The current context state.
        _user_set: Set of field names explicitly provided by the user.
    """

    def __init__(self, user_context: UserContextInput | None = None) -> None:
        self._context = user_context or UserContextInput()
        self._user_set: set[str] = set()

        if user_context is not None:
            ctx_data = user_context.model_dump()
            for field_name, default_val in _DEFAULTS.items():
                val = ctx_data.get(field_name)
                if val != default_val:
                    self._user_set.add(field_name)

            ng = user_context.narration_guidance
            if ng.tone or ng.key_messages or ng.avoid_topics or ng.target_length_minutes > 0:
                self._user_set.add("narration_guidance")

            vp = user_context.visual_preferences
            if vp.highlight_color or vp.theme != "default":
                self._user_set.add("visual_preferences")

    def enrich(self, field_name: str, value: Any) -> bool:
        """Set a field only if the user did not provide it.

        Args:
            field_name: The context field name to enrich.
            value: The auto-generated value to set.

        Returns:
            True if enrichment was applied, False if user-set.
        """
        if field_name in self._user_set:
            logger.debug("Skipping enrichment of user-set field '%s'", field_name)
            return False

        ctx_data = self._context.model_dump()
        ctx_data[field_name] = value
        self._context = UserContextInput(**ctx_data)
        return True

    def get_context(self) -> EnrichedContext:
        """Return the current enriched context."""
        ctx_data = self._context.model_dump()
        return EnrichedContext(
            **{k: v for k, v in ctx_data.items()},
            user_set_fields=sorted(self._user_set),
        )

    @property
    def user_set_fields(self) -> set[str]:
        return self._user_set.copy()
