"""Unit tests for Pydantic LLM output validation models."""

import pytest

from src.domains.review.services.llm_output_models import (
    CategoryScore,
    DesignReviewOutput,
    FeatureReviewOutput,
    FeatureSuggestionsOutput,
    Proposal,
    validate_llm_output,
)


@pytest.mark.unit
class TestCategoryScore:
    def test_defaults(self) -> None:
        cs = CategoryScore()
        # An absent category score is UNMEASURED (None), not the worst possible
        # measurement (0.0). This assertion previously pinned the defect.
        assert cs.score is None
        assert cs.observations == []
        assert cs.notes == ""

    def test_string_score_coerced(self) -> None:
        cs = CategoryScore.model_validate({"score": "7"})
        assert cs.score == 7.0

    def test_extra_fields_preserved(self) -> None:
        cs = CategoryScore.model_validate({"score": 8.0, "custom_field": "extra"})
        dumped = cs.model_dump()
        assert dumped["custom_field"] == "extra"


@pytest.mark.unit
class TestDesignReviewOutput:
    def test_valid_data(self) -> None:
        data = {
            "overall_score": 8.5,
            "categories": {"layout": {"score": 9.0, "observations": ["Clean"]}},
            "strengths": ["Good colors"],
            "weaknesses": ["Slow loading"],
            "recommendations": ["Add caching"],
        }
        model = DesignReviewOutput.model_validate(data)
        assert model.overall_score == 8.5
        assert len(model.strengths) == 1

    def test_string_score_coerced(self) -> None:
        data = {"overall_score": "7"}
        model = DesignReviewOutput.model_validate(data)
        assert model.overall_score == 7.0

    def test_missing_fields_get_defaults(self) -> None:
        model = DesignReviewOutput.model_validate({})
        # Unmeasured, not an F. Previously pinned at 0.0.
        assert model.overall_score is None
        assert model.categories == {}
        assert model.strengths == []
        assert model.weaknesses == []
        assert model.recommendations == []

    def test_extra_fields_preserved(self) -> None:
        data = {"overall_score": 7.0, "extra_key": "value"}
        model = DesignReviewOutput.model_validate(data)
        dumped = model.model_dump()
        assert dumped["extra_key"] == "value"


@pytest.mark.unit
class TestFeatureReviewOutput:
    def test_valid_data(self) -> None:
        data = {
            "vertical": "e-commerce",
            "feature_completeness_score": 6.5,
            "present_features": {"search": True},
            "missing_features": ["chat"],
        }
        model = FeatureReviewOutput.model_validate(data)
        assert model.vertical == "e-commerce"
        assert model.feature_completeness_score == 6.5

    def test_string_completeness_score_coerced(self) -> None:
        data = {"feature_completeness_score": "8"}
        model = FeatureReviewOutput.model_validate(data)
        assert model.feature_completeness_score == 8.0


@pytest.mark.unit
class TestProposal:
    def test_defaults(self) -> None:
        p = Proposal()
        assert p.id == 0
        assert p.title == ""

    def test_string_id(self) -> None:
        p = Proposal(id="abc")
        assert p.id == "abc"

    def test_int_id(self) -> None:
        p = Proposal(id=5)
        assert p.id == 5


@pytest.mark.unit
class TestFeatureSuggestionsOutput:
    def test_valid_data(self) -> None:
        data = {
            "total_proposals": 2,
            "proposals": [
                {"id": 1, "title": "Search", "category": "quick_win"},
                {"id": 2, "title": "Dark mode", "category": "nice_to_have"},
            ],
        }
        model = FeatureSuggestionsOutput.model_validate(data)
        assert model.total_proposals == 2
        assert len(model.proposals) == 2


@pytest.mark.unit
class TestValidateLlmOutput:
    def test_known_type_returns_coerced_dict(self) -> None:
        data = {"overall_score": "7", "strengths": ["Good"]}
        result = validate_llm_output("design_review", data)
        assert result["overall_score"] == 7.0
        assert isinstance(result, dict)

    def test_unknown_type_returns_raw_dict(self) -> None:
        data = {"custom": "data"}
        result = validate_llm_output("unknown_type", data)
        assert result is data

    def test_preserves_extra_fields(self) -> None:
        data = {"overall_score": 8.0, "extra": "value"}
        result = validate_llm_output("design_review", data)
        assert result["extra"] == "value"

    def test_defaults_for_missing_fields(self) -> None:
        result = validate_llm_output("design_review", {})
        assert result["overall_score"] is None
        assert result["strengths"] == []
        assert result["weaknesses"] == []

    def test_feature_suggestions_coercion(self) -> None:
        data = {
            "total_proposals": "3",
            "proposals": [{"id": 1, "title": "Test"}],
        }
        result = validate_llm_output("feature_suggestions", data)
        assert result["total_proposals"] == 3
