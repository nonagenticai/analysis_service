"""Unit tests for context enricher."""

from src.domains.analyze.services.context.enricher import ContextEnricher
from src.domains.analyze.services.context.types import NarrationGuidance, UserContextInput


class TestContextEnricher:
    def test_enrich_empty_context(self) -> None:
        enricher = ContextEnricher()
        assert enricher.enrich("description", "auto desc") is True
        ctx = enricher.get_context()
        assert ctx.description == "auto desc"

    def test_enrich_skips_user_set(self) -> None:
        user = UserContextInput(description="user desc")
        enricher = ContextEnricher(user)
        assert enricher.enrich("description", "auto desc") is False
        ctx = enricher.get_context()
        assert ctx.description == "user desc"

    def test_enrich_auto_field(self) -> None:
        user = UserContextInput(description="user desc")
        enricher = ContextEnricher(user)
        assert enricher.enrich("site_type", "saas") is True
        ctx = enricher.get_context()
        assert ctx.site_type == "saas"

    def test_user_set_fields_tracked(self) -> None:
        user = UserContextInput(description="test", site_type="blog")
        enricher = ContextEnricher(user)
        assert "description" in enricher.user_set_fields
        assert "site_type" in enricher.user_set_fields
        assert "goals" not in enricher.user_set_fields

    def test_narration_guidance_user_set(self) -> None:
        user = UserContextInput(narration_guidance=NarrationGuidance(tone="casual"))
        enricher = ContextEnricher(user)
        assert "narration_guidance" in enricher.user_set_fields

    def test_none_context(self) -> None:
        enricher = ContextEnricher(None)
        assert enricher.user_set_fields == set()
        assert enricher.enrich("description", "test") is True

    def test_enriched_context_has_user_set_fields(self) -> None:
        user = UserContextInput(description="test")
        enricher = ContextEnricher(user)
        ctx = enricher.get_context()
        assert "description" in ctx.user_set_fields

    def test_multiple_enrichments(self) -> None:
        enricher = ContextEnricher()
        enricher.enrich("description", "desc")
        enricher.enrich("site_type", "saas")
        ctx = enricher.get_context()
        assert ctx.description == "desc"
        assert ctx.site_type == "saas"
