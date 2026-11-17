"""Tests for security_scanner module."""

from unittest.mock import patch

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.security_scanner import (
    DIRECTORY_LISTING_PATHS,
    EXPOSED_FILE_CHECKS,
    PLATFORM_FILE_CHECKS,
    SSRF_BLOCKED_PREFIXES,
    UNIVERSAL_FILE_CHECKS,
    SecurityScanner,
    _detect_platform,
    _is_false_positive_file,
    _is_ssrf_safe,
)


class TestIsSsrfSafe:
    """Test _is_ssrf_safe function."""

    def test_localhost_blocked(self):
        assert _is_ssrf_safe("http://localhost/path") is False

    def test_zero_ip_blocked(self):
        assert _is_ssrf_safe("http://0.0.0.0/path") is False

    def test_private_ip_blocked(self):
        assert _is_ssrf_safe("http://192.168.1.1/") is False
        assert _is_ssrf_safe("http://10.0.0.1/") is False
        assert _is_ssrf_safe("http://172.16.0.1/") is False

    def test_loopback_ip_blocked(self):
        assert _is_ssrf_safe("http://127.0.0.1/") is False

    def test_link_local_blocked(self):
        assert _is_ssrf_safe("http://169.254.169.254/") is False

    def test_public_url_allowed(self):
        assert _is_ssrf_safe("https://example.com/path") is True

    def test_public_ip_allowed(self):
        assert _is_ssrf_safe("https://8.8.8.8/") is True

    def test_invalid_url_returns_bool(self):
        # "not-a-url" has no recognized hostname, so it passes prefix checks
        result = _is_ssrf_safe("not-a-url")
        assert isinstance(result, bool)

    def test_hostname_prefix_check(self):
        # A hostname starting with a blocked prefix
        assert _is_ssrf_safe("http://10.something.com/") is False


class TestSecurityScannerConstants:
    """Test security scanner constants."""

    def test_ssrf_blocked_prefixes(self):
        assert len(SSRF_BLOCKED_PREFIXES) > 10
        assert "127." in SSRF_BLOCKED_PREFIXES
        assert "10." in SSRF_BLOCKED_PREFIXES

    def test_exposed_file_checks(self):
        assert len(UNIVERSAL_FILE_CHECKS) == 3
        assert len(EXPOSED_FILE_CHECKS) == 5  # 3 universal + 2 platform
        for path, check_id, _severity in EXPOSED_FILE_CHECKS:
            assert isinstance(path, str)
            assert check_id.startswith("sec-")
        # Platform checks keyed correctly
        assert "wordpress" in PLATFORM_FILE_CHECKS
        assert "apache" in PLATFORM_FILE_CHECKS

    def test_directory_listing_paths(self):
        assert len(DIRECTORY_LISTING_PATHS) == 4
        assert "/images/" in DIRECTORY_LISTING_PATHS


class TestSecurityScannerSsrf:
    """Test SecurityScanner.scan SSRF blocking."""

    async def test_scan_ssrf_blocked(self):
        scanner = SecurityScanner()
        result = await scanner.scan("http://localhost/admin")
        assert len(result) == 1
        assert result[0]["check_id"] == "sec-ssrf-blocked"


class TestCheckSecurityHeaders:
    """Test _check_security_headers method."""

    def test_missing_all_headers(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_security_headers(headers, "https://example.com")
        assert len(findings) == 6  # CSP, HSTS, XFO, XCTO, Referrer, Permissions

    def test_all_headers_present(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers(
            {
                "content-security-policy": "default-src 'self'",
                "strict-transport-security": "max-age=31536000",
                "x-frame-options": "DENY",
                "x-content-type-options": "nosniff",
                "referrer-policy": "strict-origin",
                "permissions-policy": "camera=()",
            }
        )
        findings = scanner._check_security_headers(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckServerHeader:
    """Test _check_server_header method."""

    def test_server_with_version(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"server": "Apache/2.4.41"})
        findings = scanner._check_server_header(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-server-disclosure"

    def test_server_without_version(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"server": "nginx"})
        findings = scanner._check_server_header(headers, "https://example.com")
        assert len(findings) == 0

    def test_no_server_header(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_server_header(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckTls:
    """Test _check_tls method."""

    def test_http_url(self):
        scanner = SecurityScanner()
        findings = scanner._check_tls("http://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-no-https"

    def test_https_url_connection_error(self):
        """TLS check fails gracefully on connection error.

        ``socket.create_connection`` is patched as well as the SSL context:
        mocking only the latter left this test opening a REAL TCP connection to
        ``example.com:443`` on every run, so it needed egress from the CI runner
        and would hang or fail on a sandboxed one. The assertion is unchanged —
        a refused connection must still degrade to a list, never raise.
        """
        scanner = SecurityScanner()
        with (
            patch("ssl.create_default_context") as mock_ctx,
            patch(
                "src.domains.critique.services.scanners.security_scanner.tls_checks.socket.create_connection",
                side_effect=ConnectionRefusedError("refused"),
            ) as mock_connect,
        ):
            mock_ctx.return_value.wrap_socket.side_effect = ConnectionRefusedError("refused")
            findings = scanner._check_tls("https://example.com")
            # Should not crash, returns empty or specific findings
            assert isinstance(findings, list)
        assert mock_connect.called, "the TLS check must have attempted a connection"


class TestCheckCspDetails:
    """Test _check_csp_details method."""

    def test_no_csp(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert len(findings) == 0

    def test_unsafe_inline(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"content-security-policy": "default-src 'self' 'unsafe-inline'"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(f["check_id"] == "sec-csp-unsafe-inline" for f in findings)

    def test_unsafe_eval(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"content-security-policy": "default-src 'self' 'unsafe-eval'"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(f["check_id"] == "sec-csp-unsafe-eval" for f in findings)

    def test_wildcard_source(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"content-security-policy": "script-src *"})
        findings = scanner._check_csp_details(headers, "https://example.com")
        assert any(f["check_id"] == "sec-csp-wildcard" for f in findings)


class TestCheckMixedContent:
    """Test _check_mixed_content method."""

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser")

    def test_https_page_with_http_resource(self):
        scanner = SecurityScanner()
        html = '<html><body><img src="http://cdn.example.com/image.jpg"></body></html>'
        findings = scanner._check_mixed_content(self._make_soup(html), "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-mixed-content"

    def test_http_page_no_mixed(self):
        scanner = SecurityScanner()
        html = '<html><body><img src="http://cdn.example.com/image.jpg"></body></html>'
        findings = scanner._check_mixed_content(self._make_soup(html), "http://example.com")
        assert len(findings) == 0

    def test_https_page_no_http_resources(self):
        scanner = SecurityScanner()
        html = '<html><body><img src="https://cdn.example.com/image.jpg"></body></html>'
        findings = scanner._check_mixed_content(self._make_soup(html), "https://example.com")
        assert len(findings) == 0


class TestCheckTechnologyDisclosure:
    """Test _check_technology_disclosure method."""

    def test_x_powered_by(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"x-powered-by": "PHP/8.1"})
        findings = scanner._check_technology_disclosure(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-tech-disclosure"

    def test_no_disclosure(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_technology_disclosure(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckHstsQuality:
    """Test _check_hsts_quality method."""

    def test_no_hsts(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_hsts_quality(headers, "https://example.com")
        assert len(findings) == 0

    def test_short_max_age(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"strict-transport-security": "max-age=3600"})
        findings = scanner._check_hsts_quality(headers, "https://example.com")
        check_ids = [f["check_id"] for f in findings]
        assert "sec-hsts-short-max-age" in check_ids

    def test_no_include_subdomains(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"strict-transport-security": "max-age=31536000"})
        findings = scanner._check_hsts_quality(headers, "https://example.com")
        check_ids = [f["check_id"] for f in findings]
        assert "sec-hsts-no-subdomains" in check_ids

    def test_full_hsts(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers(
            {"strict-transport-security": "max-age=63072000; includeSubDomains; preload"}
        )
        findings = scanner._check_hsts_quality(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckHtmlSecrets:
    """Test _check_html_secrets method."""

    def test_secret_detected(self):
        scanner = SecurityScanner()
        html = """<script>var api_key = 'AAAA1234567890BBBBCCCC';</script>"""  # pragma: allowlist secret
        findings = scanner._check_html_secrets(html, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-html-secret-exposed"

    def test_no_secrets(self):
        scanner = SecurityScanner()
        html = "<html><body>Hello World</body></html>"
        findings = scanner._check_html_secrets(html, "https://example.com")
        assert len(findings) == 0


class TestCheckSri:
    """Test _check_sri method."""

    def _make_soup(self, html: str):
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser")

    def test_missing_sri_on_external_script(self):
        scanner = SecurityScanner()
        html = '<html><head><script src="https://cdn.other.com/lib.js"></script></head></html>'
        findings = scanner._check_sri(self._make_soup(html), "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-sri-missing"

    def test_sri_present(self):
        scanner = SecurityScanner()
        html = (
            '<html><head><script src="https://cdn.other.com/lib.js" '
            'integrity="sha256-abc" crossorigin="anonymous"></script></head></html>'
        )
        findings = scanner._check_sri(self._make_soup(html), "https://example.com")
        assert len(findings) == 0

    def test_same_origin_script(self):
        scanner = SecurityScanner()
        html = '<html><head><script src="https://example.com/main.js"></script></head></html>'
        findings = scanner._check_sri(self._make_soup(html), "https://example.com")
        assert len(findings) == 0

    def test_missing_sri_on_external_stylesheet(self):
        scanner = SecurityScanner()
        html = (
            '<html><head><link rel="stylesheet" '
            'href="https://cdn.other.com/style.css"></head></html>'
        )
        findings = scanner._check_sri(self._make_soup(html), "https://example.com")
        assert len(findings) == 1


class TestCheckCspReporting:
    """Test _check_csp_reporting method."""

    def test_no_csp(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_csp_reporting(headers, "https://example.com")
        assert len(findings) == 0

    def test_csp_without_reporting(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"content-security-policy": "default-src 'self'"})
        findings = scanner._check_csp_reporting(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-csp-no-reporting"

    def test_csp_with_report_uri(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers(
            {"content-security-policy": "default-src 'self'; report-uri /csp-report"}
        )
        findings = scanner._check_csp_reporting(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckCrossOriginHeaders:
    """Test _check_corp, _check_coop, _check_coep methods."""

    def test_missing_corp(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_corp(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-missing-corp"

    def test_present_corp(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"cross-origin-resource-policy": "same-origin"})
        findings = scanner._check_corp(headers, "https://example.com")
        assert len(findings) == 0

    def test_missing_coop(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_coop(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-missing-coop"

    def test_present_coop(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"cross-origin-opener-policy": "same-origin"})
        findings = scanner._check_coop(headers, "https://example.com")
        assert len(findings) == 0

    def test_missing_coep(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_coep(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-missing-coep"

    def test_present_coep(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"cross-origin-embedder-policy": "require-corp"})
        findings = scanner._check_coep(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckDnsPrefetchControl:
    """Test _check_dns_prefetch_control method."""

    def test_missing(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({})
        findings = scanner._check_dns_prefetch_control(headers, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-dns-prefetch"

    def test_present(self):
        scanner = SecurityScanner()
        import httpx

        headers = httpx.Headers({"x-dns-prefetch-control": "off"})
        findings = scanner._check_dns_prefetch_control(headers, "https://example.com")
        assert len(findings) == 0


class TestCheckMetaRefreshRedirect:
    """Test _check_meta_refresh_redirect method."""

    def test_meta_refresh_with_url(self):
        scanner = SecurityScanner()
        html = (
            '<html><head><meta http-equiv="refresh" content="0;url=https://evil.com"></head></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_meta_refresh_redirect(soup, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-meta-refresh-redirect"

    def test_meta_refresh_no_url(self):
        scanner = SecurityScanner()
        html = '<html><head><meta http-equiv="refresh" content="30"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_meta_refresh_redirect(soup, "https://example.com")
        assert len(findings) == 0

    def test_no_meta_refresh(self):
        scanner = SecurityScanner()
        html = "<html><head></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_meta_refresh_redirect(soup, "https://example.com")
        assert len(findings) == 0


class TestCheckFormActionExternal:
    """Test _check_form_action_external method."""

    def test_external_form_action(self):
        scanner = SecurityScanner()
        html = '<html><body><form action="https://evil.com/steal"></form></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_form_action_external(soup, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-form-external-action"

    def test_same_origin_form(self):
        scanner = SecurityScanner()
        html = '<html><body><form action="https://example.com/submit"></form></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_form_action_external(soup, "https://example.com")
        assert len(findings) == 0

    def test_relative_form_action(self):
        scanner = SecurityScanner()
        html = '<html><body><form action="/submit"></form></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_form_action_external(soup, "https://example.com")
        assert len(findings) == 0

    def test_empty_url(self):
        scanner = SecurityScanner()
        soup = BeautifulSoup("<html></html>", "html.parser")
        findings = scanner._check_form_action_external(soup, "")
        assert len(findings) == 0


class TestCheckFormInsecure:
    """Test _check_form_insecure method."""

    def test_http_form_action(self):
        scanner = SecurityScanner()
        html = '<html><body><form action="http://example.com/submit"></form></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_form_insecure(soup, "https://example.com")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-form-insecure"

    def test_https_form_action(self):
        scanner = SecurityScanner()
        html = '<html><body><form action="https://example.com/submit"></form></body></html>'
        soup = BeautifulSoup(html, "html.parser")
        findings = scanner._check_form_insecure(soup, "https://example.com")
        assert len(findings) == 0


class TestDetectPlatform:
    """Test _detect_platform function."""

    def test_wordpress_from_html(self):
        html = '<html><link rel="stylesheet" href="/wp-content/themes/style.css"></html>'
        platforms = _detect_platform(html, None)
        assert "wordpress" in platforms

    def test_wordpress_from_wp_includes(self):
        html = '<html><script src="/wp-includes/js/jquery.js"></script></html>'
        platforms = _detect_platform(html, None)
        assert "wordpress" in platforms

    def test_apache_from_headers(self):
        import httpx

        headers = httpx.Headers({"server": "Apache/2.4.51"})
        platforms = _detect_platform(None, headers)
        assert "apache" in platforms

    def test_wordpress_from_powered_by_header(self):
        import httpx

        headers = httpx.Headers({"x-powered-by": "WordPress 6.4"})
        platforms = _detect_platform(None, headers)
        assert "wordpress" in platforms

    def test_no_platform_detected(self):
        html = "<html><body>Hello</body></html>"
        import httpx

        headers = httpx.Headers({"server": "nginx"})
        platforms = _detect_platform(html, headers)
        assert platforms == set()

    def test_none_inputs(self):
        platforms = _detect_platform(None, None)
        assert platforms == set()


class TestIsFalsePositiveFile:
    """Test _is_false_positive_file function."""

    def test_env_html_response_is_false_positive(self):
        assert _is_false_positive_file(".env", "text/html; charset=utf-8", "<html>Not Found</html>")

    def test_env_text_response_is_real(self):
        assert not _is_false_positive_file(".env", "text/plain", "DB_PASSWORD=secret")

    def test_git_config_valid(self):
        body = "[core]\n\trepositoryformatversion = 0\n"
        assert not _is_false_positive_file(".git/config", "text/plain", body)

    def test_git_config_html_false_positive(self):
        assert _is_false_positive_file(".git/config", "text/html", "<html>404</html>")

    def test_wp_config_html_false_positive(self):
        assert _is_false_positive_file(
            "wp-config.php", "text/html", "<html><body>Welcome to our site</body></html>"
        )

    def test_wp_config_real_php(self):
        assert not _is_false_positive_file(
            "wp-config.php", "text/html", "<?php define('DB_NAME', 'wp');"
        )

    def test_dsstore_html_false_positive(self):
        assert _is_false_positive_file(".DS_Store", "text/html", "<html>Not Found</html>")

    def test_htaccess_html_false_positive(self):
        assert _is_false_positive_file(".htaccess", "text/html", "<html>Catch all</html>")
