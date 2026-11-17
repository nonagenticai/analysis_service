"""Validate context against discovered URLs and analysis."""

from __future__ import annotations

import fnmatch
import logging

from src.domains.analyze.schemas import PageType
from src.domains.analyze.services.context.types import (
    ContextValidationResult,
    UserContextInput,
    ValidationIssue,
)

logger = logging.getLogger(__name__)


def resolve_page_pattern(
    pattern: str,
    urls: list[str],
    page_types: dict[str, str] | None = None,
) -> list[str]:
    """Match a pattern against discovered URLs.

    Supports 4 match types:
    1. Exact URL match
    2. Substring match
    3. PageType enum name match
    4. Glob pattern via fnmatch
    """
    if pattern in urls:
        return [pattern]

    substring_matches = [u for u in urls if pattern in u]
    if substring_matches:
        return substring_matches

    if page_types:
        try:
            pt = PageType(pattern.lower())
            type_matches = [url for url, ptype in page_types.items() if ptype == pt.value]
            if type_matches:
                return type_matches
        except ValueError:
            pass

    glob_matches = [u for u in urls if fnmatch.fnmatch(u, pattern)]
    if glob_matches:
        return glob_matches

    return []


class ContextValidator:
    """Validate user context against discovered site data.

    Checks focus/exclude page existence, conflicts, feature orphans,
    and narration duration feasibility.
    """

    @staticmethod
    def _check_pattern_existence(
        patterns: list[str],
        urls: list[str],
        page_types: dict[str, str] | None,
        check: str,
        msg_prefix: str,
    ) -> list[ValidationIssue]:
        """Emit a warning per pattern that matches no discovered URLs."""
        issues: list[ValidationIssue] = []
        for pattern in patterns:
            if not resolve_page_pattern(pattern, urls, page_types):
                issues.append(
                    ValidationIssue(
                        level="warning",
                        check=check,
                        message=f"{msg_prefix} '{pattern}' does not match any discovered URLs",
                    )
                )
        return issues

    @staticmethod
    def _check_focus_exclude_conflict(
        context: UserContextInput, urls: list[str], page_types: dict[str, str] | None
    ) -> list[ValidationIssue]:
        if not urls:
            return []
        focus_matched: set[str] = set()
        for pattern in context.focus_pages:
            focus_matched.update(resolve_page_pattern(pattern, urls, page_types))
        exclude_matched: set[str] = set()
        for pattern in context.exclude_pages:
            exclude_matched.update(resolve_page_pattern(pattern, urls, page_types))
        return [
            ValidationIssue(
                level="error",
                check="focus_exclude_conflict",
                message=f"URL '{url}' matches both focus_pages and exclude_pages",
            )
            for url in sorted(focus_matched & exclude_matched)
        ]

    @staticmethod
    def _check_feature_orphans(
        context: UserContextInput, features_found: list[str] | None
    ) -> list[ValidationIssue]:
        if features_found is None:
            return []
        features_lower = {f.lower() for f in features_found}
        return [
            ValidationIssue(
                level="warning",
                check="feature_priority_orphan",
                message=f"Feature priority '{fp.feature}' not found in analysis",
            )
            for fp in context.feature_priorities
            if fp.feature.lower() not in features_lower
        ]

    @staticmethod
    def _check_duration_feasibility(
        context: UserContextInput, urls: list[str]
    ) -> list[ValidationIssue]:
        target = context.narration_guidance.target_length_minutes
        if target <= 0:
            return []
        focus_count = len(context.focus_pages) if context.focus_pages else max(len(urls), 1)
        issues: list[ValidationIssue] = []
        min_feasible = focus_count * 0.5
        if target < min_feasible:
            issues.append(
                ValidationIssue(
                    level="warning",
                    check="duration_feasibility",
                    message=(
                        f"Target duration {target:.1f}min may be too short for "
                        f"{focus_count} pages (minimum ~{min_feasible:.1f}min)"
                    ),
                )
            )
        max_feasible = focus_count * 3.0
        if target > max_feasible and focus_count > 0:
            issues.append(
                ValidationIssue(
                    level="warning",
                    check="duration_feasibility",
                    message=(
                        f"Target duration {target:.1f}min may be too long for "
                        f"{focus_count} pages (maximum ~{max_feasible:.1f}min)"
                    ),
                )
            )
        return issues

    @staticmethod
    def validate(
        context: UserContextInput,
        discovered_urls: list[str] | None = None,
        page_types: dict[str, str] | None = None,
        features_found: list[str] | None = None,
    ) -> ContextValidationResult:
        """Run all validation checks against the provided context.

        Args:
            context: User-provided context to validate.
            discovered_urls: URLs found during crawl.
            page_types: Mapping of URL to page type string.
            features_found: Features detected in analysis.

        Returns:
            ContextValidationResult with any issues found.
        """
        result = ContextValidationResult()
        urls = discovered_urls or []
        result.issues.extend(
            ContextValidator._check_pattern_existence(
                context.focus_pages,
                urls,
                page_types,
                check="focus_page_existence",
                msg_prefix="Focus page pattern",
            )
        )
        result.issues.extend(
            ContextValidator._check_pattern_existence(
                context.exclude_pages,
                urls,
                page_types,
                check="exclude_page_effectiveness",
                msg_prefix="Exclude page pattern",
            )
        )
        result.issues.extend(
            ContextValidator._check_focus_exclude_conflict(context, urls, page_types)
        )
        result.issues.extend(ContextValidator._check_feature_orphans(context, features_found))
        result.issues.extend(ContextValidator._check_duration_feasibility(context, urls))
        return result
