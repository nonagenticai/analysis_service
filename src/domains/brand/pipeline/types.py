"""Pipeline result types for structured error reporting."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StageIssue:
    """A warning or error from a pipeline stage."""

    stage: str = ""
    severity: str = ""  # "warning" or "error"
    message: str = ""


@dataclass
class StageResult:
    """Result from executing a single pipeline stage."""

    stage_name: str = ""
    success: bool = True
    duration_ms: int = 0
    warnings: list[StageIssue] = field(default_factory=list)
    errors: list[StageIssue] = field(default_factory=list)

    def add_warning(self, message: str) -> None:
        """Record a non-fatal warning."""
        self.warnings.append(StageIssue(stage=self.stage_name, severity="warning", message=message))

    def add_error(self, message: str, *, exc: Exception | None = None) -> None:
        """Record an error (marks stage as failed)."""
        self.success = False
        msg = f"{message}: {exc}" if exc else message
        self.errors.append(StageIssue(stage=self.stage_name, severity="error", message=msg))
