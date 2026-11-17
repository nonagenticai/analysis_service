"""Pydantic models for context inference, enrichment, and validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class FeaturePriority(BaseModel):
    """A prioritized feature for analysis focus."""

    feature: str
    priority: Literal["high", "medium", "low", "skip"] = "medium"
    notes: str = ""


class NarrationGuidance(BaseModel):
    """Guidance for narration style and content."""

    tone: str = ""
    key_messages: list[str] = []
    avoid_topics: list[str] = []
    target_length_minutes: float = 0.0


class VisualPreferences(BaseModel):
    """Visual style preferences for output."""

    highlight_color: str = ""
    theme: str = "default"

    @field_validator("highlight_color", mode="before")
    @classmethod
    def validate_hex_color(cls, v: str) -> str:
        import re

        if v and not re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", v):
            raise ValueError(
                f"highlight_color must be a valid CSS hex color (e.g. #ff0000, #f00), got '{v}'"
            )
        return v


class UserContextInput(BaseModel):
    """User-provided context input for analysis."""

    description: str = ""
    site_type: str = ""
    target_audience: str = ""
    goals: list[str] = []
    focus_pages: list[str] = []
    exclude_pages: list[str] = []
    feature_priorities: list[FeaturePriority] = []
    narration_guidance: NarrationGuidance = Field(default_factory=NarrationGuidance)
    visual_preferences: VisualPreferences = Field(default_factory=VisualPreferences)
    critique_focus: list[str] = []


class InferredContext(BaseModel):
    """Auto-inferred context from crawl/analysis data."""

    description: str = ""
    site_type: str = ""
    target_audience: str = ""
    goals: list[str] = []
    focus_pages: list[str] = []
    exclude_pages: list[str] = []
    feature_priorities: list[FeaturePriority] = []
    narration_guidance: NarrationGuidance = Field(default_factory=NarrationGuidance)
    auto_generated: bool = True


class EnrichedContext(BaseModel):
    """Merged user + inferred context."""

    description: str = ""
    site_type: str = ""
    target_audience: str = ""
    goals: list[str] = []
    focus_pages: list[str] = []
    exclude_pages: list[str] = []
    feature_priorities: list[FeaturePriority] = []
    narration_guidance: NarrationGuidance = Field(default_factory=NarrationGuidance)
    visual_preferences: VisualPreferences = Field(default_factory=VisualPreferences)
    critique_focus: list[str] = []
    user_set_fields: list[str] = []


class ValidationIssue(BaseModel):
    """A single validation issue with severity level."""

    level: Literal["error", "warning"]
    check: str
    message: str


class ContextValidationResult(BaseModel):
    """Result of context validation with a list of issues."""

    issues: list[ValidationIssue] = []

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == "warning" for i in self.issues)

    @property
    def is_clean(self) -> bool:
        return len(self.issues) == 0
