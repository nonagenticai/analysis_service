"""Unit tests for page classifier."""

from src.domains.analyze.schemas import PageType
from src.domains.analyze.services.page_classifier import PageClassifier


class TestPageClassifier:
    def setup_method(self) -> None:
        self.classifier = PageClassifier()

    def test_homepage_root(self) -> None:
        assert self.classifier.classify("https://example.com/") == PageType.HOMEPAGE

    def test_homepage_index(self) -> None:
        assert self.classifier.classify("https://example.com/index.html") == PageType.HOMEPAGE

    def test_pricing(self) -> None:
        assert self.classifier.classify("https://example.com/pricing") == PageType.PRICING

    def test_blog_index(self) -> None:
        assert self.classifier.classify("https://example.com/blog/") == PageType.BLOG_INDEX

    def test_blog_post(self) -> None:
        assert self.classifier.classify("https://example.com/blog/my-post") == PageType.BLOG_POST

    def test_products_listing(self) -> None:
        assert self.classifier.classify("https://example.com/products/") == PageType.PRODUCT_LISTING

    def test_product_detail(self) -> None:
        assert self.classifier.classify("https://example.com/products/widget") == PageType.PRODUCT

    def test_docs(self) -> None:
        assert (
            self.classifier.classify("https://example.com/docs/getting-started")
            == PageType.DOCUMENTATION
        )

    def test_about(self) -> None:
        assert self.classifier.classify("https://example.com/about") == PageType.ABOUT

    def test_contact(self) -> None:
        assert self.classifier.classify("https://example.com/contact") == PageType.CONTACT

    def test_faq(self) -> None:
        assert self.classifier.classify("https://example.com/faq") == PageType.FAQ

    def test_login(self) -> None:
        assert self.classifier.classify("https://example.com/login") == PageType.LOGIN

    def test_dashboard(self) -> None:
        assert self.classifier.classify("https://example.com/dashboard") == PageType.DASHBOARD

    def test_gallery(self) -> None:
        assert self.classifier.classify("https://example.com/gallery") == PageType.GALLERY

    def test_form_signup(self) -> None:
        assert self.classifier.classify("https://example.com/signup") == PageType.FORM

    def test_landing_page(self) -> None:
        assert self.classifier.classify("https://example.com/lp/summer") == PageType.LANDING

    def test_subdomain_blog(self) -> None:
        assert self.classifier.classify("https://blog.example.com/") == PageType.BLOG_INDEX

    def test_subdomain_docs(self) -> None:
        assert self.classifier.classify("https://docs.example.com/") == PageType.DOCUMENTATION

    def test_subdomain_app(self) -> None:
        assert self.classifier.classify("https://app.example.com/") == PageType.DASHBOARD

    def test_unknown_page(self) -> None:
        assert self.classifier.classify("https://example.com/random-page-xyz") == PageType.UNKNOWN

    def test_structural_form_detection(self) -> None:
        result = self.classifier.classify(
            "https://example.com/random",
            has_forms=True,
            interactive_elements=[{"name": "signup_email"}],
        )
        assert result == PageType.FORM

    def test_content_landing_detection(self) -> None:
        result = self.classifier.classify(
            "https://example.com/some-page",
            content_sections=[
                {"section_type": "hero"},
                {"section_type": "features"},
            ],
        )
        assert result == PageType.LANDING

    def test_spa_hash_routing(self) -> None:
        result = self.classifier.classify("https://example.com/#/dashboard")
        assert result == PageType.DASHBOARD

    def test_solutions_page(self) -> None:
        assert (
            self.classifier.classify("https://example.com/solutions/enterprise") == PageType.PRODUCT
        )

    def test_case_studies(self) -> None:
        assert self.classifier.classify("https://example.com/case-studies") == PageType.LANDING
