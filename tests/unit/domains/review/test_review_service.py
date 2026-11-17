"""Unit tests for review service."""

import json

import pytest

from src.domains.review.schemas import AnalysisInput, NarrativeInput, PageInput, SiteAnalysisInput
from src.domains.review.services.llm_output_models import validate_llm_output
from src.domains.review.services.review_service import ReviewService


class TestValidateLlmOutput:
    def test_valid_design_review(self) -> None:
        data = {
            "overall_score": 8.5,
            "categories": {"layout": 9},
            "strengths": ["Clean design"],
            "weaknesses": ["Slow loading"],
            "recommendations": ["Add caching"],
        }
        result = validate_llm_output("design_review", data)
        assert result["overall_score"] == 8.5
        assert "strengths" in result

    def test_missing_key_gets_default(self) -> None:
        data = {"overall_score": 8.5}
        result = validate_llm_output("design_review", data)
        # Pydantic coercion fills in defaults
        assert result["categories"] == {}
        assert result["strengths"] == []

    def test_string_score_coerced(self) -> None:
        data = {
            "overall_score": "7",
            "categories": {},
            "strengths": [],
            "weaknesses": [],
            "recommendations": [],
        }
        result = validate_llm_output("design_review", data)
        assert result["overall_score"] == 7.0

    def test_unknown_review_type_returns_raw(self) -> None:
        data = {"anything": True}
        result = validate_llm_output("nonexistent_type", data)
        assert result is data

    def test_valid_feature_suggestions(self) -> None:
        data = {
            "total_proposals": 3,
            "proposals": [{"title": "Add search"}],
        }
        result = validate_llm_output("feature_suggestions", data)
        assert result["total_proposals"] == 3


class TestReviewServiceParsing:
    def setup_method(self) -> None:
        self.service = ReviewService()

    def test_parse_valid_json(self) -> None:
        raw = json.dumps(
            {
                "overall_score": 7,
                "categories": {"layout": 8},
                "strengths": ["Good colors"],
                "weaknesses": ["No mobile"],
                "recommendations": ["Add responsive"],
            }
        )
        result = self.service._parse_and_validate("design_review", raw)
        assert result["overall_score"] == 7
        assert "strengths" in result

    def test_parse_json_with_markdown(self) -> None:
        raw = '```json\n{"overall_score": 7, "categories": {}, "strengths": [], "weaknesses": [], "recommendations": []}\n```'
        result = self.service._parse_and_validate("design_review", raw)
        assert result["overall_score"] == 7

    def test_parse_no_json(self) -> None:
        with pytest.raises(ValueError, match="No JSON object"):
            self.service._parse_and_validate("design_review", "This is just text")

    def test_normalize_key_aliases(self) -> None:
        raw = json.dumps(
            {
                "total": 2,
                "suggestions": [{"title": "Idea 1"}, {"title": "Idea 2"}],
            }
        )
        result = self.service._parse_and_validate("feature_suggestions", raw)
        assert result["total_proposals"] == 2
        assert len(result["proposals"]) == 2

    def test_auto_populate_total(self) -> None:
        raw = json.dumps(
            {
                "proposals": [{"title": "A"}, {"title": "B"}],
            }
        )
        result = self.service._parse_and_validate("feature_suggestions", raw)
        assert result["total_proposals"] == 2


class TestReviewServicePromptBuilding:
    def setup_method(self) -> None:
        self.service = ReviewService()

    def test_build_prompt_basic(self) -> None:
        analysis = AnalysisInput(
            pages=[PageInput(url="https://example.com", title="Example", page_type="homepage")],
            site=SiteAnalysisInput(description="A test website"),
        )
        prompt = self.service._build_prompt("design_review", analysis)
        assert "A test website" in prompt
        assert "Example" in prompt
        assert "design review" in prompt

    def test_build_prompt_with_narrative(self) -> None:
        analysis = AnalysisInput(
            pages=[PageInput(url="https://example.com")],
            site=SiteAnalysisInput(description="Test"),
            narrative=NarrativeInput(hooks=["Compelling hook"], key_quotes=["Great product!"]),
        )
        prompt = self.service._build_prompt("design_review", analysis)
        assert "Compelling hook" in prompt
        assert "Great product!" in prompt

    def test_build_prompt_with_prior_reviews(self) -> None:
        analysis = AnalysisInput(
            pages=[PageInput(url="https://example.com")],
            site=SiteAnalysisInput(description="Test"),
        )
        prior = {
            "design_review": {
                "overall_score": 7,
                "strengths": ["Good layout"],
                "weaknesses": ["Slow"],
            }
        }
        prompt = self.service._build_prompt("feature_suggestions", analysis, prior_reviews=prior)
        assert "Prior Review Findings" in prompt
        assert "Good layout" in prompt

    def test_build_prompt_with_message(self) -> None:
        analysis = AnalysisInput(
            pages=[PageInput(url="https://example.com")],
            site=SiteAnalysisInput(description="Test"),
        )
        prompt = self.service._build_prompt("design_review", analysis, message="Focus on mobile")
        assert "Focus on mobile" in prompt


class TestContentSectionsFallback:
    """Test that content_sections are used when p.text is empty."""

    def setup_method(self) -> None:
        self.service = ReviewService()

    def test_content_sections_fallback_when_text_empty(self) -> None:
        """When p.text is empty but content_sections has data, excerpts are built from sections."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    page_type="homepage",
                    text="",
                    content_sections=[
                        {"heading": "About Us", "text_preview": "We are a company."},
                        {"heading": "Services", "text_preview": "We offer many services."},
                    ],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "About Us" in context["page_content"]
        assert "We are a company." in context["page_content"]
        assert "Services" in context["page_content"]

    def test_text_preferred_over_content_sections(self) -> None:
        """When p.text has data, content_sections are ignored."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    text="Actual page text here",
                    content_sections=[
                        {"heading": "Fallback", "text_preview": "Should not appear"},
                    ],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "Actual page text here" in context["page_content"]
        assert "Should not appear" not in context["page_content"]

    def test_empty_content_sections_no_fallback(self) -> None:
        """When both text and content_sections are empty, page_content is empty."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    text="",
                    content_sections=[],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert context["page_content"] == ""


class TestVisualDataFallback:
    """Test that headings and interactive_elements are used when css/font data is missing."""

    def setup_method(self) -> None:
        self.service = ReviewService()

    def test_headings_fallback_when_no_heading_structure(self) -> None:
        """When heading_structure is empty but headings has data, headings are included."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    headings=["Welcome", "About", "Contact"],
                    heading_structure=[],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "Headings:" in context["visual_design_data"]
        assert "Welcome" in context["visual_design_data"]

    def test_interactive_elements_buttons_and_links(self) -> None:
        """Buttons and links from interactive_elements are included in visual data."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    interactive_elements=[
                        {"tag": "button", "text": "Sign Up"},
                        {"tag": "a", "text": "Home"},
                        {"tag": "input", "type": "text"},
                    ],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "Buttons/CTAs: Sign Up" in context["visual_design_data"]
        assert "Navigation links: Home" in context["visual_design_data"]

    def test_heading_structure_preferred_over_headings(self) -> None:
        """When heading_structure is present, headings fallback is not used."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    headings=["Welcome"],
                    heading_structure=[{"level": "H1"}, {"level": "H2"}],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "Heading hierarchy:" in context["visual_design_data"]
        assert "Headings:" not in context["visual_design_data"]

    def test_no_visual_data_at_all(self) -> None:
        """When no visual data is available, fallback message is shown."""
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert context["visual_design_data"] == "No visual data extracted."


class TestPageListEnrichment:
    """Test that page list includes content_sections and headings counts."""

    def setup_method(self) -> None:
        self.service = ReviewService()

    def test_page_list_includes_section_and_heading_counts(self) -> None:
        analysis = AnalysisInput(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    page_type="homepage",
                    content_sections=[{"heading": "A"}, {"heading": "B"}],
                    headings=["H1", "H2", "H3"],
                )
            ],
            site=SiteAnalysisInput(description="Test"),
        )
        context = self.service._build_template_context("design_review", analysis)
        assert "2 content sections" in context["pages"]
        assert "3 headings" in context["pages"]
