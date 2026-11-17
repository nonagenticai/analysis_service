"""Unit tests for pipeline types."""

from src.domains.brand.pipeline.types import StageIssue, StageResult


class TestStageResult:
    def test_default_values(self) -> None:
        r = StageResult(stage_name="test")
        assert r.success is True
        assert r.duration_ms == 0
        assert r.warnings == []
        assert r.errors == []

    def test_add_warning(self) -> None:
        r = StageResult(stage_name="colors")
        r.add_warning("CSS fetch timed out")
        assert len(r.warnings) == 1
        assert r.warnings[0].stage == "colors"
        assert r.warnings[0].severity == "warning"
        assert r.warnings[0].message == "CSS fetch timed out"
        assert r.success is True  # Warnings don't fail

    def test_add_error(self) -> None:
        r = StageResult(stage_name="fetch")
        r.add_error("Connection refused")
        assert len(r.errors) == 1
        assert r.errors[0].stage == "fetch"
        assert r.errors[0].severity == "error"
        assert r.success is False

    def test_add_error_with_exception(self) -> None:
        r = StageResult(stage_name="fetch")
        exc = ConnectionError("timeout")
        r.add_error("Failed to fetch", exc=exc)
        assert "Failed to fetch: timeout" in r.errors[0].message
        assert r.success is False


class TestStageIssue:
    def test_default_values(self) -> None:
        issue = StageIssue()
        assert issue.stage == ""
        assert issue.severity == ""
        assert issue.message == ""

    def test_with_values(self) -> None:
        issue = StageIssue(stage="colors", severity="warning", message="No colors found")
        assert issue.stage == "colors"
        assert issue.severity == "warning"
