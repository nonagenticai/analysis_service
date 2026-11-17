"""Unit tests for context inferrer."""

from src.domains.analyze.services.context.inferrer import ContextInferrer, _infer_site_type


class TestContextInferrer:
    def test_from_pages_empty(self) -> None:
        result = ContextInferrer.from_pages([])
        assert result.site_type == ""
        assert result.focus_pages == []

    def test_from_pages_homepage_focused(self) -> None:
        pages = [
            {"page_type": "homepage", "url": "https://example.com/"},
            {"page_type": "about", "url": "https://example.com/about"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/" in result.focus_pages

    def test_from_pages_excludes_login(self) -> None:
        pages = [
            {"page_type": "login", "url": "https://example.com/login"},
            {"page_type": "homepage", "url": "https://example.com/"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/login" in result.exclude_pages

    def test_from_pages_excludes_error_urls(self) -> None:
        pages = [{"page_type": "unknown", "url": "https://example.com/404"}]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/404" in result.exclude_pages

    def test_from_pages_saas_detection(self) -> None:
        pages = [
            {"page_type": "pricing", "url": "https://example.com/pricing"},
            {"page_type": "product", "url": "https://example.com/product/1"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert result.site_type == "saas"

    def test_from_site_analysis_description(self) -> None:
        site = {"description": "A great SaaS tool", "target_audience": ["developers"]}
        result = ContextInferrer.from_site_analysis(site)
        assert result.description == "A great SaaS tool"
        assert result.target_audience == "developers"

    def test_from_site_analysis_goals(self) -> None:
        site = {"use_cases": ["project management", "time tracking"]}
        result = ContextInferrer.from_site_analysis(site)
        assert result.goals == ["project management", "time tracking"]

    def test_from_site_analysis_tone(self) -> None:
        site = {"tone": "professional"}
        result = ContextInferrer.from_site_analysis(site)
        assert result.narration_guidance.tone == "professional"

    def test_from_site_analysis_carries_site_type(self) -> None:
        from src.domains.analyze.services.context.types import InferredContext

        prior = InferredContext(site_type="saas")
        site = {"description": "test"}
        result = ContextInferrer.from_site_analysis(site, prior=prior)
        assert result.site_type == "saas"

    def test_merge_user_wins(self) -> None:
        from src.domains.analyze.services.context.types import InferredContext

        user = {"description": "user desc", "site_type": "ecommerce"}
        auto = InferredContext(description="auto desc", site_type="saas")
        merged = ContextInferrer.merge(user, auto)
        assert merged["description"] == "user desc"
        assert merged["site_type"] == "ecommerce"

    def test_merge_none_user(self) -> None:
        from src.domains.analyze.services.context.types import InferredContext

        auto = InferredContext(description="auto desc")
        merged = ContextInferrer.merge(None, auto)
        assert merged["description"] == "auto desc"

    def test_merge_auto_fills_gaps(self) -> None:
        from src.domains.analyze.services.context.types import InferredContext

        user = {"description": "user desc"}
        auto = InferredContext(site_type="blog", goals=["read articles"])
        merged = ContextInferrer.merge(user, auto)
        assert merged["site_type"] == "blog"


class TestInferSiteType:
    def test_empty(self) -> None:
        assert _infer_site_type([]) == ""

    def test_ecommerce(self) -> None:
        assert _infer_site_type(["product", "product", "product"]) == "ecommerce"

    def test_blog(self) -> None:
        assert _infer_site_type(["blog_index", "homepage"]) == "blog"

    def test_docs(self) -> None:
        assert _infer_site_type(["documentation", "documentation"]) == "docs"

    def test_webapp(self) -> None:
        assert _infer_site_type(["dashboard", "homepage"]) == "webapp"

    def test_website_fallback(self) -> None:
        assert _infer_site_type(["homepage", "about", "contact"]) == "website"
