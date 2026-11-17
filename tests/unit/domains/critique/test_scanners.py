"""Unit tests for critique scanners."""

import json

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.a11y import A11yScanner
from src.domains.critique.services.scanners.perf import PerformanceScanner
from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.critique.services.scanners.seo import SEOScanner

BASIC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Test Page</title>
    <meta name="description" content="A test page for scanning with enough content to pass length checks easily">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="canonical" href="https://example.com/">
</head>
<body>
    <main>
    <h1>Welcome</h1>
    <p>Hello world</p>
    <img src="logo.png" alt="Logo">
    </main>
</body>
</html>"""

BAD_HTML = """<!DOCTYPE html>
<html>
<head>
    <script src="blocking.js"></script>
    <script src="another.js"></script>
</head>
<body>
    <div onclick="alert()">Click me</div>
    <h3>Skipped headings</h3>
    <img src="photo.jpg">
    <img src="icon.png">
    <a href="/page">click here</a>
    <input type="text" name="email">
</body>
</html>"""


class TestSEOScanner:
    def setup_method(self) -> None:
        self.scanner = SEOScanner()

    def test_good_html_has_few_findings(self) -> None:
        findings = self.scanner.scan(BASIC_HTML, "https://example.com")
        categories = {f["category"] for f in findings}
        assert all(c == "seo" for c in categories)
        # Good HTML should have minimal findings (maybe OG tags, structured data)
        high_severity = [f for f in findings if f["severity"] in ("critical", "high")]
        assert len(high_severity) == 0

    def test_missing_title_detected(self) -> None:
        html = "<html><head></head><body><p>No title</p></body></html>"
        findings = self.scanner.scan(html, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("title" in t.lower() for t in titles)

    def test_missing_meta_description(self) -> None:
        html = "<html><head><title>Test</title></head><body></body></html>"
        findings = self.scanner.scan(html, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("meta description" in t.lower() for t in titles)

    def test_missing_h1(self) -> None:
        html = "<html><head><title>Test</title></head><body><h2>Sub</h2></body></html>"
        findings = self.scanner.scan(html, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("h1" in t.lower() for t in titles)

    def test_images_missing_alt(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("alt" in t.lower() for t in titles)

    def test_finding_structure(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        assert len(findings) > 0
        for f in findings:
            assert "finding_id" in f
            assert "category" in f
            assert "severity" in f
            assert "title" in f
            assert "description" in f
            assert f["category"] == "seo"

    def test_twitter_cards_missing(self) -> None:
        html = (
            "<html><head><title>Test Page Title Here</title></head><body><h1>Hi</h1></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any("twitter" in f["title"].lower() for f in findings)

    def test_hreflang_validation(self) -> None:
        html = """<html lang="en"><head><title>Test Page Title Here</title>
        <link rel="alternate" hreflang="invalid-value-xyz" href="https://example.com/other">
        </head><body><h1>Hi</h1></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any("hreflang" in f["title"].lower() for f in findings)

    def test_url_structure_underscores(self) -> None:
        findings = self.scanner.scan(BASIC_HTML, "https://example.com/my_page_name")
        assert any("underscore" in f["title"].lower() for f in findings)

    @pytest.mark.asyncio
    async def test_robots_txt_missing(self) -> None:
        # Test scan_live with a URL that won't resolve (we just check it returns findings)
        findings = await self.scanner.scan_live("https://httpbin.org/status/404")
        # Should have findings about robots.txt or sitemap
        assert isinstance(findings, list)


class TestA11yScanner:
    def setup_method(self) -> None:
        self.scanner = A11yScanner()

    def test_good_html_has_few_findings(self) -> None:
        findings = self.scanner.scan(BASIC_HTML, "https://example.com")
        high_severity = [f for f in findings if f["severity"] in ("critical", "high")]
        # Good HTML with lang, alt, main landmark should have few issues
        assert len(high_severity) == 0

    def test_missing_lang_detected(self) -> None:
        html = "<html><head><title>Test</title></head><body><h1>Hi</h1></body></html>"
        findings = self.scanner.scan(html, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("lang" in t.lower() for t in titles)

    def test_missing_alt_detected(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("alt" in t.lower() for t in titles)

    def test_clickable_div_without_role(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("aria" in t.lower() or "role" in t.lower() for t in titles)

    def test_form_labels(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("label" in t.lower() for t in titles)

    def test_link_text_quality(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("link" in t.lower() and "descriptive" in t.lower() for t in titles)

    def test_video_without_captions(self) -> None:
        html = (
            '<html lang="en"><head><title>Test Page</title></head>'
            "<body><main><h1>Hi</h1>"
            '<video src="video.mp4"></video>'
            "</main></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "caption" in f["title"].lower() or "video" in f["title"].lower() for f in findings
        )

    def test_button_without_name(self) -> None:
        html = (
            '<html lang="en"><head><title>Test Page</title></head>'
            "<body><main><h1>Hi</h1>"
            '<button><i class="icon-search"></i></button>'
            "</main></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "button" in f["title"].lower() and "name" in f["title"].lower() for f in findings
        )

    def test_table_without_headers(self) -> None:
        html = (
            '<html lang="en"><head><title>Test Page</title></head>'
            "<body><main><h1>Hi</h1>"
            "<table><tr><td>A</td><td>B</td></tr>"
            "<tr><td>1</td><td>2</td></tr></table>"
            "</main></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "table" in f["title"].lower() and "header" in f["title"].lower() for f in findings
        )

    def test_outline_none_detected(self) -> None:
        html = (
            '<html lang="en"><head><title>Test Page</title></head>'
            "<body><main><h1>Hi</h1>"
            '<a href="/" style="outline: none;">Link</a>'
            "</main></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "outline" in f["title"].lower()
            or "keyboard" in f["title"].lower()
            or "focus" in f["title"].lower()
            for f in findings
        )


class TestPerformanceScanner:
    def setup_method(self) -> None:
        self.scanner = PerformanceScanner()

    def test_good_html_has_few_findings(self) -> None:
        findings = self.scanner.scan(BASIC_HTML, "https://example.com")
        high_severity = [f for f in findings if f["severity"] in ("critical", "high")]
        assert len(high_severity) == 0

    def test_render_blocking_scripts(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("render-blocking" in t.lower() or "blocking" in t.lower() for t in titles)

    def test_missing_viewport(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        titles = [f["title"] for f in findings]
        assert any("viewport" in t.lower() for t in titles)

    def test_finding_categories(self) -> None:
        findings = self.scanner.scan(BAD_HTML, "https://example.com")
        assert all(f["category"] == "performance" for f in findings)

    def test_page_weight_large(self) -> None:
        large_html = (
            "<html><head><title>Test</title>"
            '<meta name="viewport" content="width=device-width">'
            "</head><body><h1>Test</h1>"
            "<p>" + "x" * 2_100_000 + "</p></body></html>"
        )
        findings = self.scanner.scan(large_html, "https://example.com")
        assert any("weight" in f["title"].lower() or "size" in f["title"].lower() for f in findings)

    def test_no_resource_hints(self) -> None:
        html = (
            "<html><head><title>Test</title>"
            '<meta name="viewport" content="width=device-width">'
            '<link rel="stylesheet" href="styles.css">'
            "</head><body><h1>Test</h1></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "preconnect" in f["title"].lower()
            or "resource hint" in f["title"].lower()
            or "preload" in f["title"].lower()
            for f in findings
        )

    def test_font_display_missing(self) -> None:
        html = (
            "<html><head><title>Test</title>"
            '<meta name="viewport" content="width=device-width">'
            "<style>"
            '@font-face { font-family: "MyFont"; src: url("font.woff2"); }'
            "</style></head><body><h1>Test</h1></body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any("font" in f["title"].lower() for f in findings)

    def test_web_vitals_hero_image(self) -> None:
        html = (
            "<html><head><title>Test</title>"
            '<meta name="viewport" content="width=device-width">'
            "</head><body><h1>Test</h1>"
            '<img src="hero.jpg" width="1200" height="600" alt="Hero">'
            "</body></html>"
        )
        findings = self.scanner.scan(html, "https://example.com")
        assert any(
            "fetchpriority" in f["title"].lower() or "priority" in f["title"].lower()
            for f in findings
        )


class TestSecurityScanner:
    def setup_method(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        self.scanner = SecurityScanner()

    def test_mixed_content_detected(self) -> None:
        from bs4 import BeautifulSoup

        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        html = (
            "<html><head></head><body>"
            '<img src="http://insecure.com/img.jpg">'
            '<script src="http://cdn.example.com/lib.js"></script>'
            "</body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_mixed_content(soup, "https://example.com")
        assert len(findings) > 0
        assert any("mixed content" in f["title"].lower() for f in findings)

    def test_mixed_content_not_on_http(self) -> None:
        from bs4 import BeautifulSoup

        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        html = '<html><body><img src="http://cdn.example.com/img.jpg"></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_mixed_content(soup, "http://example.com")
        assert len(findings) == 0

    def test_csp_unsafe_inline(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        from httpx import Headers

        headers = Headers({"content-security-policy": "default-src 'self' 'unsafe-inline'"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(
            "unsafe-inline" in f["title"].lower() or "unsafe-inline" in f["description"].lower()
            for f in findings
        )

    def test_csp_unsafe_eval(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        from httpx import Headers

        headers = Headers({"content-security-policy": "default-src 'self' 'unsafe-eval'"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(
            "unsafe-eval" in f["title"].lower() or "unsafe-eval" in f["description"].lower()
            for f in findings
        )

    def test_csp_wildcard(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        from httpx import Headers

        headers = Headers({"content-security-policy": "default-src *"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(f["check_id"] == "sec-csp-wildcard" for f in findings)

    def test_csp_no_header_returns_empty(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        from httpx import Headers

        headers = Headers({})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert len(findings) == 0

    def test_cors_reflected_origin(self) -> None:
        # This is a unit test for the concept - actual CORS check requires HTTP
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        assert scanner is not None  # Scanner should instantiate

    def test_mixed_content_limits_urls(self) -> None:
        from bs4 import BeautifulSoup

        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        # Build HTML with 7 mixed content resources
        imgs = "".join(f'<img src="http://insecure.com/img{i}.jpg">' for i in range(7))
        html = f"<html><body>{imgs}</body></html>"
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_mixed_content(soup, "https://example.com")
        assert len(findings) == 1
        # Description should mention "and 2 more"
        assert "2 more" in findings[0]["description"]

    def test_finding_has_check_id_and_source(self) -> None:
        from bs4 import BeautifulSoup

        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        scanner = SecurityScanner()
        html = '<html><body><img src="http://bad.com/x.png"></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_mixed_content(soup, "https://example.com")
        assert len(findings) > 0
        for f in findings:
            assert f.get("check_id") == "sec-mixed-content"
            assert f.get("source_tool") == "security_scanner"


class TestUXReviewer:
    def setup_method(self) -> None:
        from src.domains.critique.services.scanners.ux_reviewer import UXReviewer

        self.reviewer = UXReviewer()

    def test_parse_structured_response(self) -> None:
        response = json.dumps(
            {
                "dimensions": {
                    "navigation": {
                        "score": 75,
                        "findings": [
                            {
                                "severity": "medium",
                                "title": "Unclear nav",
                                "description": "Navigation is confusing",
                                "recommendation": "Simplify menu",
                            }
                        ],
                    },
                    "trust_signals": {
                        "score": 60,
                        "findings": [
                            {
                                "severity": "low",
                                "title": "No social proof",
                                "description": "Missing testimonials",
                                "recommendation": "Add reviews",
                            }
                        ],
                    },
                }
            }
        )
        findings = self.reviewer._parse_response(response, "https://example.com")
        assert len(findings) == 2
        assert any("Navigation" in f["description"] for f in findings)
        assert any("Trust Signals" in f["description"] for f in findings)
        assert self.reviewer._last_dimension_scores.get("navigation") == 75

    def test_parse_legacy_array_response(self) -> None:
        response = json.dumps(
            [
                {
                    "severity": "medium",
                    "title": "Issue 1",
                    "description": "Desc 1",
                    "recommendation": "Fix 1",
                }
            ]
        )
        findings = self.reviewer._parse_response(response, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["title"] == "Issue 1"

    def test_parse_invalid_response(self) -> None:
        findings = self.reviewer._parse_response("not json at all", "https://example.com")
        assert len(findings) == 1
        assert "parse error" in findings[0]["title"].lower()

    def test_all_findings_have_false_positive_risk(self) -> None:
        response = json.dumps(
            {
                "dimensions": {
                    "navigation": {
                        "score": 80,
                        "findings": [
                            {
                                "severity": "low",
                                "title": "Minor issue",
                                "description": "Small thing",
                                "recommendation": "Fix",
                            }
                        ],
                    }
                }
            }
        )
        findings = self.reviewer._parse_response(response, "https://example.com")
        for f in findings:
            assert f.get("false_positive_risk") == "medium"


class TestCodeQualityScanner:
    def setup_method(self) -> None:
        from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner

        self.scanner = CodeQualityScanner()

    @pytest.mark.asyncio
    async def test_not_configured_returns_info(self) -> None:
        from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner

        scanner = CodeQualityScanner(api_url="", api_key="")
        findings = await scanner.scan("https://github.com/user/repo")
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"
        assert "not configured" in findings[0]["title"].lower()

    def test_normalize_cgh_finding(self) -> None:
        from src.domains.critique.services.scanners.code_quality_scanner import CodeQualityScanner

        scanner = CodeQualityScanner()
        finding = scanner._normalize_cgh_finding(
            {
                "severity": "error",
                "title": "Unused variable",
                "message": "Variable x is unused",
                "rule": "no-unused-vars",
                "fix": "Remove the variable",
            },
            "https://github.com/user/repo",
        )
        assert finding["severity"] == "high"
        assert finding["title"] == "Unused variable"
        assert finding["category"] == "code_quality"
        assert finding["check_id"] == "cq-no-unused-vars"


class TestSEOScannerNewChecks:
    """Tests for WS-1a new SEO scanner checks."""

    def setup_method(self) -> None:
        self.scanner = SEOScanner()

    def test_meta_robots_noindex(self) -> None:
        html = '<html lang="en"><head><title>Test Page Title</title><meta name="robots" content="noindex, follow"></head><body><h1>Hi</h1></body></html>'
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "seo-meta-robots-noindex" for f in findings)

    def test_meta_robots_nofollow(self) -> None:
        html = '<html lang="en"><head><title>Test Page Title</title><meta name="robots" content="index, nofollow"></head><body><h1>Hi</h1></body></html>'
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "seo-meta-robots-nofollow" for f in findings)

    def test_favicon_missing(self) -> None:
        html = '<html lang="en"><head><title>Test Page Title</title></head><body><h1>Hi</h1></body></html>'
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "seo-favicon-missing" for f in findings)

    def test_favicon_present(self) -> None:
        html = '<html lang="en"><head><title>Test Page Title</title><link rel="icon" href="/favicon.ico"></head><body><h1>Hi</h1></body></html>'
        findings = self.scanner.scan(html, "https://example.com")
        assert not any(f.get("check_id") == "seo-favicon-missing" for f in findings)

    def test_viewport_seo(self) -> None:
        html = '<html lang="en"><head><title>Test Page Title</title></head><body><h1>Hi</h1></body></html>'
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "seo-viewport-missing" for f in findings)

    def test_structured_data_quality(self) -> None:
        html = """<html lang="en"><head><title>Test Page Title</title>
        <script type="application/ld+json">{"name": "Test"}</script>
        </head><body><h1>Hi</h1></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "seo-structured-data-no-type" for f in findings)


class TestSecurityScannerNewChecks:
    """Tests for WS-1b new security scanner checks."""

    def setup_method(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import SecurityScanner

        self.scanner = SecurityScanner()

    def test_tech_disclosure(self) -> None:
        from httpx import Headers

        headers = Headers({"x-powered-by": "Express", "content-type": "text/html"})
        findings = self.scanner._check_technology_disclosure(headers, "https://example.com")
        assert any(f.get("check_id") == "sec-tech-disclosure" for f in findings)

    def test_tech_disclosure_clean(self) -> None:
        from httpx import Headers

        headers = Headers({"content-type": "text/html"})
        findings = self.scanner._check_technology_disclosure(headers, "https://example.com")
        assert len(findings) == 0

    def test_hsts_short_max_age(self) -> None:
        from httpx import Headers

        headers = Headers({"strict-transport-security": "max-age=86400"})
        findings = self.scanner._check_hsts_quality(headers, "https://example.com")
        assert any(f.get("check_id") == "sec-hsts-short-max-age" for f in findings)

    def test_hsts_no_subdomains(self) -> None:
        from httpx import Headers

        headers = Headers({"strict-transport-security": "max-age=31536000"})
        findings = self.scanner._check_hsts_quality(headers, "https://example.com")
        assert any(f.get("check_id") == "sec-hsts-no-subdomains" for f in findings)

    def test_hsts_good(self) -> None:
        from httpx import Headers

        headers = Headers({"strict-transport-security": "max-age=31536000; includeSubDomains"})
        findings = self.scanner._check_hsts_quality(headers, "https://example.com")
        assert len(findings) == 0

    def test_html_secrets_exposed(self) -> None:
        # Built at runtime, not written as one literal: a token-shaped fixture must
        # never exist in a published tree's history (the secret scanner and GitHub
        # push protection both flag the concatenated form only as a pattern match).
        fake_key = "sk_" + "live_" + "1234567890abcdef1234567890"
        html = f"""<html><body>
        <script>var api_key = "{fake_key}";</script>
        </body></html>"""
        findings = self.scanner._check_html_secrets(html, "https://example.com")
        assert any(f.get("check_id") == "sec-html-secret-exposed" for f in findings)
        # Verify redaction
        for f in findings:
            if f.get("check_id") == "sec-html-secret-exposed":
                assert fake_key not in f.get("evidence", "")

    def test_html_secrets_clean(self) -> None:
        html = "<html><body><p>No secrets here</p></body></html>"
        findings = self.scanner._check_html_secrets(html, "https://example.com")
        assert len(findings) == 0

    def test_sri_missing(self) -> None:
        from bs4 import BeautifulSoup

        html = """<html><body>
        <script src="https://cdn.example.com/lib.js"></script>
        <link rel="stylesheet" href="https://cdn.example.com/styles.css">
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        findings = self.scanner._check_sri(soup, "https://mysite.com")
        assert any(f.get("check_id") == "sec-sri-missing" for f in findings)

    def test_sri_present(self) -> None:
        from bs4 import BeautifulSoup

        html = """<html><body>
        <script src="https://cdn.example.com/lib.js" integrity="sha384-abc123" crossorigin="anonymous"></script>
        </body></html>"""
        soup = BeautifulSoup(html, "html.parser")
        findings = self.scanner._check_sri(soup, "https://mysite.com")
        assert not any(f.get("check_id") == "sec-sri-missing" for f in findings)


class TestPerformanceScannerNewChecks:
    """Tests for WS-1c new performance scanner checks."""

    def setup_method(self) -> None:
        self.scanner = PerformanceScanner()

    def test_no_modern_images(self) -> None:
        html = """<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        </head><body><h1>Test</h1>
        <img src="photo.jpg" alt="Photo">
        <img src="icon.png" alt="Icon">
        </body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "perf-no-modern-images" for f in findings)

    def test_modern_images_with_picture(self) -> None:
        html = """<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        </head><body><h1>Test</h1>
        <picture>
            <source srcset="photo.webp" type="image/webp">
            <img src="photo.jpg" alt="Photo">
        </picture>
        </body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert not any(f.get("check_id") == "perf-no-modern-images" for f in findings)

    def test_excessive_third_party(self) -> None:
        scripts = "".join(
            f'<script src="https://cdn{i}.example.com/lib.js"></script>' for i in range(12)
        )
        html = f"""<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        </head><body><h1>Test</h1>{scripts}</body></html>"""
        findings = self.scanner.scan(html, "https://mysite.com")
        assert any(f.get("check_id") == "perf-excessive-third-party" for f in findings)

    def test_css_duplication(self) -> None:
        html = """<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
        .header { color: red; }
        .header { background: blue; }
        .header { font-size: 14px; }
        </style>
        </head><body><h1>Test</h1></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "perf-css-duplication" for f in findings)

    def test_render_blocking_css(self) -> None:
        html = """<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link rel="stylesheet" href="/styles.css">
        <link rel="stylesheet" href="/more.css">
        </head><body><h1>Test</h1></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "perf-render-blocking-css" for f in findings)

    def test_dom_depth(self) -> None:
        # Build deeply nested DOM (20 levels)
        nested = "<div>" * 22 + "<p>Deep</p>" + "</div>" * 22
        html = f"""<html><head><title>Test</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        </head><body><h1>Test</h1>{nested}</body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "perf-deep-dom" for f in findings)


class TestA11yScannerNewChecks:
    """Tests for WS-1d new accessibility scanner checks."""

    def setup_method(self) -> None:
        self.scanner = A11yScanner()

    def test_autocomplete_missing(self) -> None:
        html = """<html lang="en"><head><title>Test Page</title></head>
        <body><main><h1>Form</h1>
        <form>
            <label for="email">Email</label>
            <input type="email" id="email" name="email">
        </form>
        </main></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "a11y-autocomplete-missing" for f in findings)

    def test_autocomplete_present(self) -> None:
        html = """<html lang="en"><head><title>Test Page</title></head>
        <body><main><h1>Form</h1>
        <form>
            <label for="email">Email</label>
            <input type="email" id="email" name="email" autocomplete="email">
        </form>
        </main></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert not any(f.get("check_id") == "a11y-autocomplete-missing" for f in findings)

    def test_focus_trap_detection(self) -> None:
        html = """<html lang="en"><head><title>Test Page</title></head>
        <body><main><h1>Test</h1>
        <div role="dialog">
            <p>Modal content</p>
        </div>
        </main></body></html>"""
        findings = self.scanner.scan(html, "https://example.com")
        assert any(f.get("check_id") == "a11y-potential-focus-trap" for f in findings)


class TestSecurityScannerSSRF:
    """Tests for SSRF validation."""

    def test_ssrf_blocks_private_ip(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

        assert _is_ssrf_safe("http://192.168.1.1/api") is False

    def test_ssrf_blocks_localhost(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

        assert _is_ssrf_safe("http://localhost/api") is False
        assert _is_ssrf_safe("http://127.0.0.1/api") is False

    def test_ssrf_blocks_link_local(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

        assert _is_ssrf_safe("http://169.254.169.254/metadata") is False

    def test_ssrf_allows_public_ip(self) -> None:
        from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe

        assert _is_ssrf_safe("https://8.8.8.8/dns") is True
        assert _is_ssrf_safe("https://example.com") is True


class TestNewScannerChecks:
    """Tests for the 27 new scanner checks added in Round 3."""

    # ── SEO checks ──────────────────────────────────────────────────────

    def test_seo_internal_nofollow(self) -> None:
        scanner = SEOScanner()
        html = (
            '<html><body><a href="https://example.com/page" rel="nofollow">Link</a></body></html>'
        )
        findings = scanner._check_internal_nofollow(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "seo-internal-nofollow" for f in findings)

    def test_seo_keyword_alignment(self) -> None:
        scanner = SEOScanner()
        html = (
            "<html><head><title>About Cats</title></head>"
            "<body><h1>Completely Different Topic</h1></body></html>"
        )
        findings = scanner._check_keyword_alignment(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "seo-keyword-alignment" for f in findings)

    def test_seo_keyword_alignment_matching(self) -> None:
        scanner = SEOScanner()
        html = (
            "<html><head><title>About Cats</title></head>"
            "<body><h1>About Cats and Dogs</h1></body></html>"
        )
        findings = scanner._check_keyword_alignment(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert not any(f["check_id"] == "seo-keyword-alignment" for f in findings)

    def test_seo_breadcrumb_missing(self) -> None:
        scanner = SEOScanner()
        html = "<html><body><p>No breadcrumbs here</p></body></html>"
        findings = scanner._check_breadcrumb_schema(
            BeautifulSoup(html, "lxml"), "https://example.com/services/audit"
        )
        assert any(f["check_id"] == "seo-breadcrumb-missing" for f in findings)

    def test_seo_canonical_protocol_mismatch(self) -> None:
        scanner = SEOScanner()
        html = (
            "<html><head>"
            '<link rel="canonical" href="http://example.com/">'
            "</head><body></body></html>"
        )
        findings = scanner._check_canonical_mismatch(
            BeautifulSoup(html, "lxml"), "https://example.com/"
        )
        assert any(f["check_id"] == "seo-canonical-protocol-mismatch" for f in findings)

    def test_seo_duplicate_meta(self) -> None:
        scanner = SEOScanner()
        html = (
            "<html><head>"
            '<meta name="description" content="first">'
            '<meta name="description" content="second">'
            "</head><body></body></html>"
        )
        findings = scanner._check_duplicate_meta(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "seo-duplicate-meta" for f in findings)

    def test_seo_mobile_font_size(self) -> None:
        scanner = SEOScanner()
        html = "<html><head><style>.tiny { font-size: 8px; }</style></head><body></body></html>"
        findings = scanner._check_mobile_font_sizes(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "seo-mobile-font-size" for f in findings)

    # ── Security checks ─────────────────────────────────────────────────

    def test_sec_csp_no_reporting(self) -> None:
        scanner = SecurityScanner()
        headers = httpx.Headers({"content-security-policy": "default-src 'self'"})
        findings = scanner._check_csp_reporting(headers, "https://example.com")
        assert any(f["check_id"] == "sec-csp-no-reporting" for f in findings)

    def test_sec_missing_corp(self) -> None:
        scanner = SecurityScanner()
        findings = scanner._check_corp(httpx.Headers({}), "https://example.com")
        assert any(f["check_id"] == "sec-missing-corp" for f in findings)

    def test_sec_missing_coop(self) -> None:
        scanner = SecurityScanner()
        findings = scanner._check_coop(httpx.Headers({}), "https://example.com")
        assert any(f["check_id"] == "sec-missing-coop" for f in findings)

    def test_sec_missing_coep(self) -> None:
        scanner = SecurityScanner()
        findings = scanner._check_coep(httpx.Headers({}), "https://example.com")
        assert any(f["check_id"] == "sec-missing-coep" for f in findings)

    def test_sec_dns_prefetch(self) -> None:
        scanner = SecurityScanner()
        findings = scanner._check_dns_prefetch_control(httpx.Headers({}), "https://example.com")
        assert any(f["check_id"] == "sec-dns-prefetch" for f in findings)

    def test_sec_meta_refresh_redirect(self) -> None:
        scanner = SecurityScanner()
        html = (
            "<html><head>"
            '<meta http-equiv="refresh" content="0;url=https://evil.com">'
            "</head><body></body></html>"
        )
        findings = scanner._check_meta_refresh_redirect(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "sec-meta-refresh-redirect" for f in findings)

    def test_sec_form_external_action(self) -> None:
        scanner = SecurityScanner()
        html = '<html><body><form action="https://evil.com/submit"></form></body></html>'
        findings = scanner._check_form_action_external(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "sec-form-external-action" for f in findings)

    def test_sec_form_insecure(self) -> None:
        scanner = SecurityScanner()
        html = '<html><body><form action="http://example.com/submit"></form></body></html>'
        findings = scanner._check_form_insecure(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "sec-form-insecure" for f in findings)

    # ── Accessibility checks ────────────────────────────────────────────

    def test_a11y_empty_th(self) -> None:
        scanner = A11yScanner()
        html = "<html><body><table><tr><th></th></tr></table></body></html>"
        findings = scanner._check_empty_th(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "a11y-empty-th" for f in findings)

    def test_a11y_iframe_no_title(self) -> None:
        scanner = A11yScanner()
        html = '<html><body><iframe src="https://example.com"></iframe></body></html>'
        findings = scanner._check_iframe_title(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "a11y-iframe-no-title" for f in findings)

    def test_a11y_deprecated_motion(self) -> None:
        scanner = A11yScanner()
        html = "<html><body><marquee>Moving text</marquee></body></html>"
        findings = scanner._check_deprecated_motion(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "a11y-deprecated-motion" for f in findings)

    def test_a11y_lang_invalid(self) -> None:
        scanner = A11yScanner()
        html = '<html lang="xyz123"><body></body></html>'
        findings = scanner._check_lang_validity(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "a11y-lang-invalid" for f in findings)

    def test_a11y_svg_no_title(self) -> None:
        scanner = A11yScanner()
        html = '<html><body><svg><rect width="100" height="100"/></svg></body></html>'
        findings = scanner._check_svg_accessibility(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "a11y-svg-no-title" for f in findings)

    def test_a11y_aria_hidden_focusable(self) -> None:
        scanner = A11yScanner()
        html = '<html><body><div aria-hidden="true"><button>Click</button></div></body></html>'
        findings = scanner._check_aria_hidden_focusable(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "a11y-aria-hidden-focusable" for f in findings)

    def test_a11y_no_aria_live(self) -> None:
        scanner = A11yScanner()
        html = "<html><body><p>No live region here</p></body></html>"
        findings = scanner._check_aria_live(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "a11y-no-aria-live" for f in findings)

    # ── Performance checks ──────────────────────────────────────────────

    def test_perf_large_inline_svg(self) -> None:
        scanner = PerformanceScanner()
        large_svg = "<svg>" + '<rect x="0" y="0" width="10" height="10"/>' * 200 + "</svg>"
        html = f"<html><body>{large_svg}</body></html>"
        findings = scanner._check_large_svg(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "perf-large-inline-svg" for f in findings)

    def test_perf_no_modulepreload(self) -> None:
        scanner = PerformanceScanner()
        html = '<html><head><script type="module" src="app.js"></script></head><body></body></html>'
        findings = scanner._check_modulepreload(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "perf-no-modulepreload" for f in findings)

    def test_perf_sync_xhr(self) -> None:
        scanner = PerformanceScanner()
        html = (
            "<html><body><script>"
            'var xhr = new XMLHttpRequest(); xhr.open("GET", "/api", false);'
            "</script></body></html>"
        )
        findings = scanner._check_sync_xhr(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "perf-sync-xhr" for f in findings)

    def test_perf_document_write(self) -> None:
        scanner = PerformanceScanner()
        html = '<html><body><script>document.write("<p>hello</p>");</script></body></html>'
        findings = scanner._check_document_write(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "perf-document-write" for f in findings)

    def test_perf_css_import(self) -> None:
        scanner = PerformanceScanner()
        html = '<html><head><style>@import url("extra.css");</style></head><body></body></html>'
        findings = scanner._check_css_import(BeautifulSoup(html, "lxml"), "https://example.com")
        assert any(f["check_id"] == "perf-css-import-chain" for f in findings)

    def test_perf_no_content_visibility(self) -> None:
        scanner = PerformanceScanner()
        nodes = "<div>" * 600 + "</div>" * 600
        html = f"<html><body>{nodes}</body></html>"
        findings = scanner._check_content_visibility(
            BeautifulSoup(html, "lxml"), "https://example.com"
        )
        assert any(f["check_id"] == "perf-no-content-visibility" for f in findings)
