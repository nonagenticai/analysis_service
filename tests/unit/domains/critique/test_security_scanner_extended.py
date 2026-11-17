"""Extended tests for security_scanner — covers async scan paths and uncovered methods.

Why the HTTP doubles here are transports, not mock clients
----------------------------------------------------------
Four sinks in this package (``scan``'s entry fetch, ``_probe_exposed_file``,
``_check_cors``, ``_check_directory_listing``) go through
``http_guard.guarded_get``, and that function's sink is now ``client.stream``
rather than ``client.get`` — it has to be, because a byte cap enforced after
``client.get`` returns is a cap on bytes the pod has already paid for.

An ``AsyncMock`` client cannot model that: ``client.stream(...)`` on a mock
returns a coroutine, and ``async with`` over a coroutine raises, which every one
of these checks then absorbs through its fail-soft ``except`` tuple. The tests
would not error — they would silently assert "no findings" against a scanner
that never ran. So the doubles here are real ``httpx.AsyncClient`` instances over
an ``httpx.MockTransport``, exactly as ``test_security_scanner_ssrf.py`` and
``tests/unit/domains/brand/test_brand_body_caps.py`` do. Hermetic, and it
exercises the real streaming path.

``_check_redirect_chain`` still calls ``client.get`` directly, so the tests in
``TestRedirectChain`` keep their mock clients — changing them would assert
something the production code does not do.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domains.critique.services.scanners.security_scanner import SecurityScanner, _is_ssrf_safe

_Handler = Callable[[httpx.Request], httpx.Response]


class _Recorder:
    """A ``MockTransport`` handler that logs the requests it is asked to make.

    Attributes:
        requests: Every request the transport received, in order.
    """

    def __init__(self, handler: _Handler) -> None:
        """Wrap *handler* with request recording.

        Args:
            handler: The responder producing the canned response.
        """
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record *request*, then delegate.

        Args:
            request: The intercepted outgoing request.

        Returns:
            Whatever the wrapped handler answers.
        """
        self.requests.append(request)
        return self._handler(request)

    @property
    def urls(self) -> list[str]:
        """Return the fetched URLs as strings, for readable assertions."""
        return [str(request.url) for request in self.requests]


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: _Handler) -> None:
    """Route every ``httpx.AsyncClient`` the scanner builds through *handler*.

    The scanner modules do ``import httpx`` and construct their clients inline,
    so the seam is the class on the shared module object. ``functools.partial``
    is built *before* the rebind, which is what stops the replacement from
    naming itself; it also preserves the constructor kwargs production passes
    (``timeout``, ``follow_redirects``, ``verify``).

    Args:
        monkeypatch: Undoes the rebind on teardown.
        handler: Mock-transport responder to install.
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


def _raises(exc: Exception) -> _Handler:
    """Build a handler that fails every request with *exc*.

    Args:
        exc: The transport-level exception to raise.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _handle(_request: httpx.Request) -> httpx.Response:
        raise exc

    return _handle


class TestSecurityScannerFullScan:
    """Test SecurityScanner.scan full execution paths."""

    async def test_scan_success_with_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full scan with HTML: headers + HTML-based checks + async checks."""
        scanner = SecurityScanner()
        secure_headers = {
            "content-security-policy": "default-src 'self'",
            "strict-transport-security": "max-age=31536000; includeSubDomains",
            "x-frame-options": "DENY",
            "x-content-type-options": "nosniff",
            "referrer-policy": "strict-origin",
            "permissions-policy": "camera=()",
            "cross-origin-resource-policy": "same-origin",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-embedder-policy": "require-corp",
            "x-dns-prefetch-control": "off",
        }

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers=secure_headers, content=b"")

        _install_transport(monkeypatch, _handle)

        html = "<html><body><p>Clean page</p></body></html>"
        findings = await scanner.scan("https://example.com", html=html)

        assert isinstance(findings, list)
        # With all security headers set correctly, findings should be minimal
        # Each finding must have required fields
        for f in findings:
            assert "check_id" in f
            assert "category" in f
            assert f["category"] == "security"

    async def test_scan_connect_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan handles httpx.ConnectError gracefully."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.ConnectError("Connection refused")))

        findings = await scanner.scan("https://unreachable.example.com")

        assert len(findings) >= 1
        check_ids = [f.get("check_id") for f in findings]
        assert "sec-connection-failed" in check_ids
        conn_finding = next(f for f in findings if f.get("check_id") == "sec-connection-failed")
        assert conn_finding["category"] == "security"
        assert "unreachable.example.com" in conn_finding.get("url", "")

    async def test_scan_remote_protocol_error_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-Connect, non-Timeout httpx failure must NOT escape ``scan``.

        ``RemoteProtocolError`` ("server disconnected without sending a response")
        is routine when probing arbitrary customer origins. It derives from
        ``RequestError -> HTTPError -> Exception`` and from NO builtin, so before
        the ``httpx.HTTPError`` arm it escaped every handler in ``scan``.
        ``_phase_run_scanners`` gathers with ``return_exceptions=True``, so the
        escape was invisible: the security scanner contributed zero findings and
        the job still scored the site as if it were clean.
        """
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.RemoteProtocolError("Server disconnected")))

        findings = await scanner.scan("https://flaky.example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-scan-error" in check_ids, (
            f"RemoteProtocolError escaped scan() instead of degrading; got {check_ids}"
        )

    async def test_scan_read_error_degrades_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``httpx.ReadError`` is in the same escaped family as RemoteProtocolError."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.ReadError("connection reset")))

        findings = await scanner.scan("https://reset.example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-scan-error" in check_ids, (
            f"ReadError escaped scan() instead of degrading; got {check_ids}"
        )

    async def test_scan_timeout_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan handles httpx.TimeoutException gracefully."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.TimeoutException("Timed out")))

        findings = await scanner.scan("https://slow.example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-timeout" in check_ids
        timeout_finding = next(f for f in findings if f.get("check_id") == "sec-timeout")
        assert timeout_finding["category"] == "security"
        assert "slow.example.com" in timeout_finding.get("url", "")

    async def test_scan_generic_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan handles unexpected exceptions gracefully."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(RuntimeError("Unexpected")))

        findings = await scanner.scan("https://broken.example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-scan-error" in check_ids
        error_finding = next(f for f in findings if f.get("check_id") == "sec-scan-error")
        assert error_finding["category"] == "security"
        assert "broken.example.com" in error_finding.get("url", "")


class TestCookieFlags:
    """Test _check_cookie_flags method."""

    def test_cookie_missing_flags(self):
        scanner = SecurityScanner()

        mock_cookie = MagicMock()
        mock_cookie.name = "session"
        mock_cookie.secure = False
        mock_cookie.has_nonstandard_attr = MagicMock(return_value=False)
        mock_cookie.get_nonstandard_attr = MagicMock(return_value=None)

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.cookies = MagicMock()
        mock_response.cookies.jar = [mock_cookie]

        findings = scanner._check_cookie_flags(mock_response, "https://example.com")
        assert len(findings) == 1
        assert "Secure" in findings[0]["description"]
        assert "HttpOnly" in findings[0]["description"]
        assert "SameSite" in findings[0]["description"]

    def test_cookie_all_flags_present(self):
        scanner = SecurityScanner()

        mock_cookie = MagicMock()
        mock_cookie.name = "session"
        mock_cookie.secure = True
        mock_cookie.has_nonstandard_attr = MagicMock(return_value=True)
        mock_cookie.get_nonstandard_attr = MagicMock(return_value="Lax")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.cookies = MagicMock()
        mock_response.cookies.jar = [mock_cookie]

        findings = scanner._check_cookie_flags(mock_response, "https://example.com")
        assert len(findings) == 0


class TestTlsCertExpiry:
    """Test TLS certificate expiry checks."""

    def test_tls_outdated_version(self):
        """TLS 1.0 triggers outdated TLS finding."""
        scanner = SecurityScanner()

        mock_ssock = MagicMock()
        mock_ssock.version.return_value = "TLSv1"
        mock_ssock.getpeercert.return_value = None
        mock_ssock.__enter__ = MagicMock(return_value=mock_ssock)
        mock_ssock.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock

        with (
            patch("ssl.create_default_context", return_value=mock_ctx),
            patch("socket.create_connection", return_value=MagicMock()),
        ):
            findings = scanner._check_tls("https://example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-tls-outdated" in check_ids
        tls_finding = next(f for f in findings if f.get("check_id") == "sec-tls-outdated")
        assert tls_finding["category"] == "security"

    def test_tls_expired_certificate(self):
        """Expired certificate triggers critical finding."""
        scanner = SecurityScanner()

        mock_ssock = MagicMock()
        mock_ssock.version.return_value = "TLSv1.3"
        mock_ssock.getpeercert.return_value = {
            "notAfter": "Jan  1 00:00:00 2020 GMT",
        }
        mock_ssock.__enter__ = MagicMock(return_value=mock_ssock)
        mock_ssock.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock

        with (
            patch("ssl.create_default_context", return_value=mock_ctx),
            patch("socket.create_connection", return_value=MagicMock()),
        ):
            findings = scanner._check_tls("https://example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-tls-expired" in check_ids
        expired_finding = next(f for f in findings if f.get("check_id") == "sec-tls-expired")
        assert expired_finding["category"] == "security"
        assert expired_finding["severity"] == "critical"

    def test_tls_expiring_soon(self):
        """Certificate expiring in 5 days triggers high finding."""
        from datetime import datetime, timedelta

        scanner = SecurityScanner()
        future_date = datetime.now(UTC) + timedelta(days=5, hours=12)
        not_after_str = future_date.strftime("%b %d %H:%M:%S %Y GMT")

        mock_ssock = MagicMock()
        mock_ssock.version.return_value = "TLSv1.3"
        mock_ssock.getpeercert.return_value = {"notAfter": not_after_str}
        mock_ssock.__enter__ = MagicMock(return_value=mock_ssock)
        mock_ssock.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_ssock

        with (
            patch("ssl.create_default_context", return_value=mock_ctx),
            patch("socket.create_connection", return_value=MagicMock()),
        ):
            findings = scanner._check_tls("https://example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-tls-expiring-soon" in check_ids
        expiring_finding = next(f for f in findings if f.get("check_id") == "sec-tls-expiring-soon")
        assert expiring_finding["category"] == "security"
        assert expiring_finding["severity"] == "high"

    def test_tls_expiring_within_21_days(self):
        """Certificate expiring in 15 days (renewal overdue) triggers medium finding."""
        findings = SecurityScanner._tls_expiry_findings("https://example.com", "x", 15)
        assert [(f["check_id"], f["severity"]) for f in findings] == [
            ("sec-tls-expiring", "medium")
        ]

    @pytest.mark.parametrize("days", [22, 60, 65, 89])
    def test_tls_with_normal_renewal_headroom_is_not_a_finding(self, days: int):
        """65 days left is the normal state of an auto-renewed cert, not a MEDIUM."""
        assert SecurityScanner._tls_expiry_findings("https://example.com", "x", days) == []


class TestExposedFiles:
    """Test _check_exposed_files method."""

    async def test_exposed_file_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An exposed .env file triggers critical finding."""
        scanner = SecurityScanner()

        def _handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.env":
                return httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    content=b"DB_PASSWORD=secret",  # pragma: allowlist secret
                )
            return httpx.Response(404)

        _install_transport(monkeypatch, _handle)

        findings = await scanner._check_exposed_files("https://example.com/page")

        assert len(findings) == 1
        assert findings[0]["check_id"].startswith("sec-exposed-")
        assert findings[0]["url"] == "https://example.com/.env"

    async def test_no_exposed_files(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No exposed files when all return 404."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, lambda _request: httpx.Response(404))

        findings = await scanner._check_exposed_files("https://example.com")

        assert len(findings) == 0

    async def test_exposed_files_request_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gracefully handle request errors in exposed file checks."""
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.ConnectError("fail")))

        findings = await scanner._check_exposed_files("https://example.com")

        assert isinstance(findings, list)
        # Connection errors should be handled gracefully -- no findings, no crash
        assert len(findings) == 0

    async def test_exposed_file_false_positive_spa(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """200 with HTML content-type for .env is filtered as false positive."""
        scanner = SecurityScanner()

        def _handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><body>Welcome to our SPA</body></html>",
            )

        _install_transport(monkeypatch, _handle)

        findings = await scanner._check_exposed_files("https://example.com")

        assert len(findings) == 0

    async def test_wp_config_not_checked_on_non_wordpress(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """wp-config.php should not be checked when site is not WordPress.

        Read off the transport rather than off a call recorder: the sink builds
        the probe URL with ``urljoin`` and hands it to ``guarded_get``, so the
        only place the *dereferenced* string is observable is the wire.
        """
        scanner = SecurityScanner()
        recorder = _Recorder(lambda _request: httpx.Response(404))
        _install_transport(monkeypatch, recorder)

        # No html/headers → no platform detected → wp-config not checked
        await scanner._check_exposed_files("https://example.com")

        assert recorder.urls, "the check probed nothing at all — the fixture is inert"
        assert not any("wp-config" in url for url in recorder.urls)


def _responds(status: int, headers: dict[str, str] | None = None, body: bytes = b"") -> _Handler:
    """Build a handler answering every request identically.

    Args:
        status: Status code to answer with.
        headers: Optional response headers.
        body: Optional response body.

    Returns:
        A ``MockTransport``-compatible handler.
    """

    def _handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, content=body)

    return _handle


class TestCors:
    """Test _check_cors method."""

    async def test_cors_reflects_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(
            monkeypatch, _responds(200, {"access-control-allow-origin": "https://evil.example.com"})
        )

        findings = await scanner._check_cors("https://example.com")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-cors-reflected"
        assert findings[0]["category"] == "security"

    async def test_cors_wildcard_with_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(
            monkeypatch,
            _responds(
                200,
                {
                    "access-control-allow-origin": "*",
                    "access-control-allow-credentials": "true",
                },
            ),
        )

        findings = await scanner._check_cors("https://example.com")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-cors-wildcard-credentials"
        assert findings[0]["category"] == "security"

    async def test_cors_wildcard_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _responds(200, {"access-control-allow-origin": "*"}))

        findings = await scanner._check_cors("https://example.com")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-cors-wildcard"
        assert findings[0]["category"] == "security"

    async def test_cors_no_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _responds(200))

        findings = await scanner._check_cors("https://example.com")

        assert len(findings) == 0

    async def test_cors_request_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _raises(httpx.ConnectError("fail")))

        findings = await scanner._check_cors("https://example.com")

        assert findings == []

    async def test_cors_probe_origin_reaches_the_wire(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe is worthless without its ``Origin``, and only the transport
        can prove the header survived ``guarded_get``'s streaming rewrite."""
        scanner = SecurityScanner()
        recorder = _Recorder(_responds(200))
        _install_transport(monkeypatch, recorder)

        await scanner._check_cors("https://example.com")

        assert recorder.requests[0].headers["origin"] == "https://evil.example.com"


class TestDirectoryListing:
    """Test _check_directory_listing method."""

    async def test_directory_listing_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(
            monkeypatch, _responds(200, body=b"<html><title>Index of /images/</title></html>")
        )

        findings = await scanner._check_directory_listing("https://example.com")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "sec-directory-listing"

    async def test_directory_listing_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scanner = SecurityScanner()
        _install_transport(monkeypatch, _responds(404, body=b"Not Found"))

        findings = await scanner._check_directory_listing("https://example.com")

        assert len(findings) == 0

    async def test_directory_listing_200_but_no_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = SecurityScanner()
        _install_transport(
            monkeypatch, _responds(200, body=b"<html><body>Regular page</body></html>")
        )

        findings = await scanner._check_directory_listing("https://example.com")

        assert len(findings) == 0


class TestRedirectChain:
    """Test _check_redirect_chain method."""

    async def test_no_redirects(self):
        scanner = SecurityScanner()

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.security_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner._check_redirect_chain("https://example.com")

        assert len(findings) == 0

    async def test_https_to_http_downgrade(self):
        scanner = SecurityScanner()

        redirect_resp = MagicMock()
        redirect_resp.status_code = 301
        redirect_resp.headers = {"location": "http://example.com/insecure"}

        final_resp = MagicMock()
        final_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[redirect_resp, final_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.security_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner._check_redirect_chain("https://example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-redirect-to-http" in check_ids

    async def test_long_redirect_chain(self):
        scanner = SecurityScanner()

        # Create 5 redirect responses, then a final 200
        responses = []
        for i in range(5):
            resp = MagicMock()
            resp.status_code = 302
            resp.headers = {"location": f"https://example.com/redirect{i + 1}"}
            responses.append(resp)

        final = MagicMock()
        final.status_code = 200
        responses.append(final)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=responses)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.security_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner._check_redirect_chain("https://example.com")

        check_ids = [f.get("check_id") for f in findings]
        assert "sec-redirect-chain-long" in check_ids

    async def test_redirect_chain_ssrf_blocked(self):
        scanner = SecurityScanner()
        findings = await scanner._check_redirect_chain("http://192.168.1.1")
        assert findings == []

    async def test_redirect_relative_location(self):
        scanner = SecurityScanner()

        redirect_resp = MagicMock()
        redirect_resp.status_code = 301
        redirect_resp.headers = {"location": "/new-path"}

        final_resp = MagicMock()
        final_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[redirect_resp, final_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.security_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner._check_redirect_chain("https://example.com")

        assert isinstance(findings, list)
        # A single redirect with relative location is not problematic
        # No HTTPS→HTTP downgrade, no long chain
        check_ids = [f.get("check_id") for f in findings]
        assert "sec-redirect-to-http" not in check_ids
        assert "sec-redirect-chain-long" not in check_ids


class TestIsSsrfSafeEdgeCases:
    """Additional edge cases for _is_ssrf_safe."""

    def test_exception_returns_false(self):
        """When parsing fails entirely, return False."""
        # Use a truly malformed URL that causes an exception
        with patch(
            "src.domains.critique.services.scanners.security_scanner.urlparse",
            side_effect=OSError("bad"),
        ):
            result = _is_ssrf_safe("http://example.com")
        assert result is False

    def test_ipv6_private_blocked(self):
        """IPv6 loopback address should be blocked."""
        assert _is_ssrf_safe("http://[::1]/") is False
