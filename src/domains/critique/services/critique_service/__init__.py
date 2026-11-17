"""Critique service package — public API preserved via re-exports.

Decomposed from a single ``critique_service.py`` (1231 LOC) into focused
submodules; all identifiers previously importable as
``from src.domains.critique.services.critique_service import X`` remain
available at this same path.
"""

from __future__ import annotations

# Re-exports of bound names that test code patches at the package level.
# Patching ``src.domains.critique.services.critique_service.get_settings`` (etc.)
# only takes effect if the same callable is looked up here at call time.
# The phase / helpers / lifecycle modules therefore reach back through the
# package namespace for these symbols rather than importing them directly.
from src.config import get_settings  # noqa: F401
from src.domains.critique.services.audit import log_event  # noqa: F401
from src.domains.critique.services.critique_service.scoring_result import (
    ALL_CATEGORIES,
    _background_tasks,
    _ScoringResult,
)
from src.domains.critique.services.critique_service.service import CritiqueService
from src.domains.critique.services.metrics import (
    scan_duration as scan_duration_metric,  # noqa: F401
)
from src.domains.critique.services.repo_detector import detect_repo_url  # noqa: F401
from src.domains.critique.services.report_generator import generate_summary  # noqa: F401

__all__ = [
    "ALL_CATEGORIES",
    "CritiqueService",
    "_ScoringResult",
    "_background_tasks",
]
