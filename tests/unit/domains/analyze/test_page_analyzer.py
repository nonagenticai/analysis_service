"""Unit tests for page analyzer."""

from src.domains.analyze.services.page_analyzer import PageAnalyzer


class TestPageAnalyzer:
    def setup_method(self) -> None:
        self.analyzer = PageAnalyzer()

    def test_analyze_basic_html(self) -> None:
        html = """
        <html>
        <head><title>Test Page</title></head>
        <body>
            <nav><a href="/">Home</a><a href="/about">About</a></nav>
            <h1>Welcome</h1>
            <section class="hero"><h2>Hero Section</h2><p>Some text here</p></section>
            <section class="features"><h2>Features</h2><p>Feature list</p></section>
        </body>
        </html>
        """
        result = self.analyzer.analyze(
            page_id="p1",
            url="https://example.com/",
            title="Test Page",
            html=html,
        )
        assert result["page_id"] == "p1"
        assert result["url"] == "https://example.com/"
        assert result["title"] == "Test Page"
        assert result["page_type"] == "homepage"
        assert len(result["headings"]) >= 1
        assert result["headings"][0] == "Welcome"

    def test_analyze_extracts_navigation(self) -> None:
        html = """
        <html><body>
            <nav><a href="/">Home</a><a href="/about">About</a><a href="/contact">Contact</a></nav>
        </body></html>
        """
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        assert len(result["navigation"]) >= 1
        assert result["navigation"][0]["nav_type"] == "menu"
        assert "Home" in result["navigation"][0]["items"]

    def test_analyze_detects_forms(self) -> None:
        html = """
        <html><body>
            <form action="/submit">
                <input type="text" name="email">
                <button type="submit">Submit</button>
            </form>
        </body></html>
        """
        result = self.analyzer.analyze("p1", "https://example.com/signup", "Signup", html)
        assert result["has_forms"] is True

    def test_analyze_detects_media(self) -> None:
        html = '<html><body><img src="photo.jpg" alt="Photo"></body></html>'
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        assert result["has_media"] is True

    def test_analyze_empty_html(self) -> None:
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", "")
        assert result["has_forms"] is False
        assert result["has_media"] is False
        assert result["interactive_elements"] == []

    def test_analyze_extracts_meta_description(self) -> None:
        html = '<html><head><meta name="description" content="A great site"></head><body></body></html>'
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        assert result["meta_description"] == "A great site"

    def test_analyze_sections(self) -> None:
        html = """
        <html><body>
            <section class="hero"><h2>Big Hero</h2><p>Welcome text</p></section>
            <section class="testimonials"><h2>Reviews</h2><p>Great product!</p></section>
        </body></html>
        """
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        section_types = [s["section_type"] for s in result["content_sections"]]
        assert "hero" in section_types
        assert "testimonials" in section_types

    def test_peripheral_forms_ignored(self) -> None:
        html = """
        <html><body>
            <h1>Main Content</h1>
            <footer>
                <form action="/newsletter"><input type="email"><button>Subscribe</button></form>
            </footer>
        </body></html>
        """
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        assert result["has_forms"] is False

    def test_tracking_iframes_ignored(self) -> None:
        html = '<html><body><iframe src="https://analytics.google.com/tracking"></iframe></body></html>'
        result = self.analyzer.analyze("p1", "https://example.com/", "Test", html)
        assert result["has_media"] is False

    def test_extract_headings_from_text_when_no_html(self) -> None:
        result = self.analyzer.analyze(
            page_id="p1",
            url="https://example.com",
            title="Example",
            html="",
            text=(
                "RaiseMyTickets | AI-Powered Enterprise Support Platform\n"
                "Platform Features\n"
                "Everything You Need to Transform Support\n"
                "AI-First Intelligence\n"
                "Natural language processing handles 40% of tickets without human intervention.\n"
                "True Integration\n"
                "Bi-directional real-time sync with Jira, Zendesk, ServiceNow.\n"
                "How RaiseMyTickets Works\n"
                "STEP 01 Customer Contact\n"
                "STEP 02 AI Gathers Info\n"
            ),
        )
        assert len(result["headings"]) >= 3
        assert "Platform Features" in result["headings"]

    def test_extract_sections_from_text_classifies_types(self) -> None:
        result = self.analyzer.analyze(
            page_id="p1",
            url="https://example.com",
            title="Example",
            html="",
            text=(
                "Platform Features\n"
                "Everything You Need to Transform Support\n"
                "AI-First Intelligence handles tickets automatically.\n"
                "Get Started Today\n"
                "Sign up for a free trial and see results in minutes.\n"
            ),
        )
        types = [s["section_type"] for s in result["content_sections"]]
        assert "features" in types
        assert "cta" in types

    def test_analyze_with_html_still_uses_html_extraction(self) -> None:
        """Ensure HTML path is not broken by text fallback."""
        result = self.analyzer.analyze(
            page_id="p1",
            url="https://example.com",
            title="Example",
            html="<html><body><h1>Main Title</h1><p>Content</p></body></html>",
            text="Main Title\nContent",
        )
        assert "Main Title" in result["headings"]


class TestNarrativeExtractor:
    def test_extract_hooks(self) -> None:
        from src.domains.analyze.services.narrative_extractor import NarrativeExtractor

        extractor = NarrativeExtractor()
        pages = [
            {
                "headings": ["Short", "This is a compelling heading for the page"],
                "content_sections": [
                    {"section_type": "features", "heading": "Amazing Feature"},
                    {
                        "section_type": "testimonials",
                        "text_preview": "Great product, highly recommend!",
                    },
                ],
                "has_forms": False,
                "has_media": True,
            }
        ]
        result = extractor.extract(pages)
        assert "This is a compelling heading for the page" in result["hooks"]
        assert "Amazing Feature" in result["feature_highlights"]
        assert "Great product, highly recommend!" in result["key_quotes"]
        assert result["total_pages"] == 1
        assert result["pages_with_media"] == 1

    def test_extract_empty(self) -> None:
        from src.domains.analyze.services.narrative_extractor import NarrativeExtractor

        extractor = NarrativeExtractor()
        result = extractor.extract([])
        assert result["hooks"] == []
        assert result["total_pages"] == 0
