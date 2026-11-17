"""Additional tests for context inferrer to cover uncovered lines."""

from __future__ import annotations

from src.domains.analyze.services.context.inferrer import (
    ContextInferrer,
    _infer_page_type_from_url,
    _infer_site_type,
)


class TestInferPageTypeFromUrl:
    """Cover lines 135-161: _infer_page_type_from_url() all branches."""

    def test_homepage_root(self) -> None:
        assert _infer_page_type_from_url("https://example.com/") == "homepage"

    def test_homepage_empty_path(self) -> None:
        assert _infer_page_type_from_url("https://example.com") == "homepage"

    def test_pricing(self) -> None:
        assert _infer_page_type_from_url("https://example.com/pricing") == "pricing"

    def test_pricing_nested(self) -> None:
        assert _infer_page_type_from_url("https://example.com/plans/pricing") == "pricing"

    def test_blog_index_with_trailing_slash(self) -> None:
        assert _infer_page_type_from_url("https://example.com/blog/") == "blog_index"

    def test_blog_index_no_trailing_slash(self) -> None:
        assert _infer_page_type_from_url("https://example.com/blog") == "blog_index"

    def test_product(self) -> None:
        assert _infer_page_type_from_url("https://example.com/product/widget") == "product"

    def test_documentation_docs(self) -> None:
        assert _infer_page_type_from_url("https://example.com/docs/api") == "documentation"

    def test_documentation_doc(self) -> None:
        assert _infer_page_type_from_url("https://example.com/doc/guide") == "documentation"

    def test_about(self) -> None:
        assert _infer_page_type_from_url("https://example.com/about") == "about"

    def test_contact(self) -> None:
        assert _infer_page_type_from_url("https://example.com/contact") == "contact"

    def test_faq(self) -> None:
        assert _infer_page_type_from_url("https://example.com/faq") == "faq"

    def test_login(self) -> None:
        assert _infer_page_type_from_url("https://example.com/login") == "login"

    def test_signin(self) -> None:
        assert _infer_page_type_from_url("https://example.com/signin") == "login"

    def test_dashboard(self) -> None:
        assert _infer_page_type_from_url("https://example.com/dashboard") == "dashboard"

    def test_gallery(self) -> None:
        assert _infer_page_type_from_url("https://example.com/gallery") == "gallery"

    def test_landing_fallback(self) -> None:
        """Any URL not matching known patterns returns 'landing'."""
        assert _infer_page_type_from_url("https://example.com/features") == "landing"

    def test_case_insensitive(self) -> None:
        assert _infer_page_type_from_url("https://example.com/PRICING") == "pricing"


class TestInferSiteTypeExtended:
    """Cover line 185 ('website' fallback) and other uncovered branches."""

    def test_empty_returns_empty_string(self) -> None:
        assert _infer_site_type([]) == ""

    def test_generic_pages_return_website(self) -> None:
        """Line 187: fallback returns 'website' for generic pages."""
        assert _infer_site_type(["homepage", "about", "contact"]) == "website"

    def test_single_generic_page_returns_website(self) -> None:
        assert _infer_site_type(["landing"]) == "website"

    def test_docs_heavy_site(self) -> None:
        result = _infer_site_type(["documentation", "documentation", "documentation", "homepage"])
        assert result == "docs"

    def test_pricing_only_returns_saas(self) -> None:
        """Line 184-185: pricing present without product still returns 'saas'."""
        assert _infer_site_type(["pricing", "homepage", "about"]) == "saas"

    def test_blog_posts_heavy(self) -> None:
        assert _infer_site_type(["blog_post", "blog_post", "blog_post"]) == "blog"


class TestFromPagesExclusionCoverage:
    """Cover line 59: URL not yet in exclude_candidates being added via URL pattern."""

    def test_url_pattern_exclusion_when_not_in_low_value(self) -> None:
        """Line 58-59: URL matches a pattern like /login but page_type is NOT in
        _LOW_VALUE_TYPES, so it wasn't added by the first check and gets added
        by the URL-pattern branch."""
        pages = [
            {"page_type": "landing", "url": "https://example.com/login"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/login" in result.exclude_pages

    def test_url_pattern_admin_exclusion(self) -> None:
        pages = [
            {"page_type": "homepage", "url": "https://example.com/admin/panel"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/admin/panel" in result.exclude_pages

    def test_url_pattern_signin_exclusion(self) -> None:
        pages = [
            {"page_type": "about", "url": "https://example.com/signin"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/signin" in result.exclude_pages

    def test_url_pattern_500_exclusion(self) -> None:
        pages = [
            {"page_type": "landing", "url": "https://example.com/500"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/500" in result.exclude_pages

    def test_url_pattern_error_exclusion(self) -> None:
        pages = [
            {"page_type": "homepage", "url": "https://example.com/error"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert "https://example.com/error" in result.exclude_pages

    def test_duplicate_exclusion_not_added_twice(self) -> None:
        """When page_type is in _LOW_VALUE_TYPES AND URL matches a pattern,
        the URL should appear only once (the second branch skips it)."""
        pages = [
            {"page_type": "login", "url": "https://example.com/login"},
        ]
        result = ContextInferrer.from_pages(pages)
        assert result.exclude_pages.count("https://example.com/login") == 1


class TestFromSiteAnalysisCoverage:
    """Cover lines 86 and 94: non-list audience, selling_points."""

    def test_non_list_audience_string(self) -> None:
        """Line 86: target_audience as a plain string instead of a list."""
        site = {"target_audience": "enterprise developers"}
        result = ContextInferrer.from_site_analysis(site)
        assert result.target_audience == "enterprise developers"

    def test_non_list_audience_numeric(self) -> None:
        """Line 86: target_audience as a non-list, non-string value."""
        site = {"target_audience": 42}
        result = ContextInferrer.from_site_analysis(site)
        assert result.target_audience == "42"

    def test_selling_points_present(self) -> None:
        """Line 94: selling_points present creates FeaturePriority entries."""
        site = {"selling_points": ["fast", "reliable", "affordable"]}
        result = ContextInferrer.from_site_analysis(site)

        assert len(result.feature_priorities) == 3
        features = [fp.feature for fp in result.feature_priorities]
        assert "fast" in features
        assert "reliable" in features
        assert "affordable" in features
        # All selling_points get medium priority
        for fp in result.feature_priorities:
            assert fp.priority == "medium"

    def test_selling_points_empty_list_no_priorities(self) -> None:
        site = {"selling_points": []}
        result = ContextInferrer.from_site_analysis(site)
        assert result.feature_priorities == []

    def test_combined_audience_and_selling_points(self) -> None:
        """Test both non-list audience and selling_points together."""
        site = {
            "target_audience": "small businesses",
            "selling_points": ["easy setup", "low cost"],
            "description": "Business tool",
        }
        result = ContextInferrer.from_site_analysis(site)
        assert result.target_audience == "small businesses"
        assert len(result.feature_priorities) == 2
        assert result.description == "Business tool"
