"""Unit tests for context validator."""

from src.domains.analyze.services.context.types import (
    FeaturePriority,
    NarrationGuidance,
    UserContextInput,
)
from src.domains.analyze.services.context.validator import ContextValidator, resolve_page_pattern


class TestResolvePagePattern:
    def test_exact_match(self) -> None:
        urls = ["https://example.com/about", "https://example.com/pricing"]
        assert resolve_page_pattern("https://example.com/about", urls) == [
            "https://example.com/about"
        ]

    def test_substring_match(self) -> None:
        urls = ["https://example.com/about-us", "https://example.com/pricing"]
        result = resolve_page_pattern("about", urls)
        assert "https://example.com/about-us" in result

    def test_page_type_match(self) -> None:
        urls = ["https://example.com/buy", "https://example.com/plans"]
        page_types = {
            "https://example.com/buy": "pricing",
            "https://example.com/plans": "pricing",
        }
        result = resolve_page_pattern("pricing", urls, page_types)
        assert len(result) == 2

    def test_glob_match(self) -> None:
        urls = ["https://example.com/blog/post-1", "https://example.com/blog/post-2"]
        result = resolve_page_pattern("*/blog/*", urls)
        assert len(result) == 2

    def test_no_match(self) -> None:
        urls = ["https://example.com/about"]
        assert resolve_page_pattern("nonexistent", urls) == []


class TestContextValidator:
    def test_clean_context(self) -> None:
        ctx = UserContextInput()
        result = ContextValidator.validate(ctx)
        assert result.is_clean

    def test_focus_page_warning(self) -> None:
        ctx = UserContextInput(focus_pages=["https://example.com/missing"])
        result = ContextValidator.validate(ctx, discovered_urls=["https://example.com/about"])
        assert result.has_warnings
        assert result.issues[0].check == "focus_page_existence"

    def test_focus_exclude_conflict(self) -> None:
        ctx = UserContextInput(
            focus_pages=["https://example.com/about"],
            exclude_pages=["https://example.com/about"],
        )
        result = ContextValidator.validate(ctx, discovered_urls=["https://example.com/about"])
        assert result.has_errors
        assert result.issues[-1].check == "focus_exclude_conflict"

    def test_feature_priority_orphan(self) -> None:
        ctx = UserContextInput(feature_priorities=[FeaturePriority(feature="Magic Wand")])
        result = ContextValidator.validate(ctx, features_found=["Dashboard", "Reports"])
        assert any(i.check == "feature_priority_orphan" for i in result.issues)

    def test_duration_too_short(self) -> None:
        ctx = UserContextInput(
            narration_guidance=NarrationGuidance(target_length_minutes=0.1),
            focus_pages=["a", "b", "c"],
        )
        result = ContextValidator.validate(ctx)
        assert any(i.check == "duration_feasibility" for i in result.issues)
