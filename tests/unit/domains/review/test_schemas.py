"""Tests for src.api.schemas -- Pydantic request/response models."""

from src.domains.review.schemas import (
    AnalysisInput,
    BatchReviewRequest,
    PageInput,
    ReviewFinding,
    ReviewRequest,
    ReviewSummary,
    UserContext,
)


class TestReviewFinding:
    def test_defaults(self):
        f = ReviewFinding(review_type="design_review")
        assert f.finding_id == ""
        assert f.evidence == []
        assert f.remediation_effort == "unknown"
        assert f.source_data == {}

    def test_with_all_fields(self):
        f = ReviewFinding(
            review_type="design_review",
            category="layout",
            severity="high",
            description="Layout issues",
            score=5.0,
            finding_id="abc123",
            evidence=["obs1", "obs2"],
            remediation_effort="high",
            source_data={"score": 5},
        )
        assert f.finding_id == "abc123"
        assert len(f.evidence) == 2


class TestReviewSummary:
    def test_score_100_and_penalty(self):
        s = ReviewSummary(
            job_id="test",
            aggregate_score=7.5,
            score_100=75.0,
            penalty_score=80.0,
        )
        assert s.score_100 == 75.0
        assert s.penalty_score == 80.0

    def test_defaults(self):
        s = ReviewSummary(job_id="test")
        assert s.score_100 is None
        assert s.penalty_score is None


class TestUserContext:
    def test_defaults(self):
        ctx = UserContext()
        assert ctx.critique_focus == []
        assert ctx.tone == "professional"

    def test_custom(self):
        ctx = UserContext(
            critique_focus=["design_review"],
            focus_areas=["layout"],
            goals=["Improve UX"],
        )
        assert len(ctx.critique_focus) == 1


class TestReviewRequest:
    def test_default_review_types(self):
        req = ReviewRequest(analysis=AnalysisInput())
        assert len(req.review_types) == 5

    def test_with_callback_url(self):
        req = ReviewRequest(
            analysis=AnalysisInput(),
            callback_url="https://example.com/webhook",
        )
        assert req.callback_url == "https://example.com/webhook"


class TestBatchReviewRequest:
    def test_batch_with_callback(self):
        req = BatchReviewRequest(
            reviews=[ReviewRequest(analysis=AnalysisInput())],
            callback_url="https://example.com/batch",
        )
        assert len(req.reviews) == 1
        assert req.callback_url is not None


class TestPageInput:
    def test_defaults(self):
        page = PageInput(url="https://example.com")
        assert page.word_count == 0
        assert page.has_forms is False
        assert page.has_media is False
