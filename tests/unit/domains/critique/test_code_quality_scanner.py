"""Tests for code_quality_scanner module — covers API scan success, timeout, and error paths."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.domains.critique.services.scanners.code_quality_scanner import (
    CGH_SEVERITY_MAP,
    CodeQualityScanner,
)


class TestCodeQualityScannerInit:
    """Test CodeQualityScanner initialization."""

    def test_is_configured_true(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")
        assert scanner.is_configured is True

    def test_is_configured_false(self):
        scanner = CodeQualityScanner(api_url="", api_key="")
        assert scanner.is_configured is False

    def test_default_from_env(self):
        with patch.dict(
            "os.environ",
            {
                "CGH_API_URL": "https://env.example.com",
                "CGH_API_KEY": "key123",  # pragma: allowlist secret
            },
        ):
            scanner = CodeQualityScanner()
            assert scanner._api_url == "https://env.example.com"
            assert scanner._api_key == "key123"  # pragma: allowlist secret


class TestCodeQualityScanNotConfigured:
    """Test scan returns info finding when not configured."""

    async def test_scan_not_configured(self):
        scanner = CodeQualityScanner(api_url="", api_key="")
        findings = await scanner.scan("https://github.com/org/repo")
        assert len(findings) == 1
        assert findings[0]["check_id"] == "cq-not-configured"


class TestCodeQualityScanSuccess:
    """Test successful CGH API scan."""

    async def test_scan_returns_findings(self):
        scanner = CodeQualityScanner(
            api_url="https://cgh.example.com",
            api_key="test-key",  # pragma: allowlist secret
            timeout=30,
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "findings": [
                {
                    "title": "Unused import",
                    "severity": "warning",
                    "message": "Module os imported but unused",
                    "rule": "unused-import",
                    "fix": "Remove the unused import",
                    "file": "src/main.py",
                    "effort": "low",
                },
                {
                    "title": "Missing docstring",
                    "severity": "info",
                    "description": "Public function lacks docstring",
                    "rule": "missing-docstring",
                    "recommendation": "Add a docstring",
                    "file": "src/utils.py",
                },
            ]
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner.scan("https://github.com/org/repo")

        assert len(findings) == 2
        assert findings[0]["severity"] == "medium"  # warning -> medium
        assert findings[0]["check_id"] == "cq-unused-import"
        assert findings[0]["source_tool"] == "code_quality_scanner"

    async def test_scan_empty_findings(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"findings": []}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner.scan("https://github.com/org/repo")

        assert findings == []

    async def test_scan_with_api_key_header(self):
        scanner = CodeQualityScanner(
            api_url="https://cgh.example.com",
            api_key="secret-key",  # pragma: allowlist secret
        )

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"findings": []}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            await scanner.scan("https://github.com/org/repo")

        # Verify auth header was passed
        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs.get("headers", call_kwargs[1].get("headers", {}))
        assert headers.get("Authorization") == "Bearer secret-key"


class TestCodeQualityScanErrors:
    """Test CGH API error handling."""

    async def test_scan_timeout(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("CGH timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner.scan("https://github.com/org/repo")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "cq-timeout"

    async def test_scan_generic_error(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("API broke"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner.scan("https://github.com/org/repo")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "cq-error"


class TestCodeQualityHttpxErrorsDoNotEscape:
    """Every httpx failure must become a finding, never an escaping raise.

    ``httpx`` errors derive from ``Exception`` only — not ``OSError`` — so the
    builtin-only tuple in ``scan`` did not catch them. ``CritiquePhases``
    gathers scanners with ``return_exceptions=True``, so an escape is silent:
    the scanner contributes zero findings and the job still finalizes and
    SCORES, reading an unreachable or erroring CGH as clean code.
    """

    @staticmethod
    def _client(side_effect: BaseException) -> AsyncMock:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    async def _scan_with(self, side_effect: BaseException) -> list[dict]:
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")
        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=self._client(side_effect),
        ):
            return await scanner.scan("https://github.com/org/repo")

    async def test_connect_error_becomes_a_finding(self):
        findings = await self._scan_with(httpx.ConnectError("CGH pod is down"))
        assert [f["check_id"] for f in findings] == ["cq-error"]

    async def test_remote_protocol_error_becomes_a_finding(self):
        findings = await self._scan_with(httpx.RemoteProtocolError("disconnected"))
        assert [f["check_id"] for f in findings] == ["cq-error"]

    async def test_http_status_error_from_raise_for_status_becomes_a_finding(self):
        """``raise_for_status()`` on a CGH 500 raises ``HTTPStatusError``.

        It is a sibling of ``RequestError`` under ``HTTPError``, not a subclass
        of it, so it needed the base name too.
        """
        request = httpx.Request("POST", "https://cgh.example.com/analyze")
        response = httpx.Response(500, request=request)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        scanner = CodeQualityScanner(api_url="https://cgh.example.com")
        with patch(
            "src.domains.critique.services.scanners.code_quality_scanner.httpx.AsyncClient",
            return_value=mock_client,
        ):
            findings = await scanner.scan("https://github.com/org/repo")

        assert [f["check_id"] for f in findings] == ["cq-error"]


class TestNormalizeCghFinding:
    """Test _normalize_cgh_finding helper."""

    def test_normalize_with_all_fields(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")
        item = {
            "title": "Test Issue",
            "severity": "error",
            "message": "Something wrong",
            "rule": "test-rule",
            "fix": "Fix it",
            "file": "src/test.py",
            "effort": "medium",
        }
        result = scanner._normalize_cgh_finding(item, "https://github.com/org/repo")
        assert result["severity"] == "high"  # error -> high
        assert result["check_id"] == "cq-test-rule"
        assert result["evidence"] == "src/test.py"

    def test_normalize_with_fallback_fields(self):
        scanner = CodeQualityScanner(api_url="https://cgh.example.com")
        item = {
            "rule": "some-check",
            "description": "A description",
            "recommendation": "A recommendation",
        }
        result = scanner._normalize_cgh_finding(item, "https://github.com/org/repo")
        assert result["title"] == "some-check"
        assert result["description"] == "A description"

    def test_cgh_severity_map(self):
        assert CGH_SEVERITY_MAP["critical"] == "critical"
        assert CGH_SEVERITY_MAP["error"] == "high"
        assert CGH_SEVERITY_MAP["warning"] == "medium"
        assert CGH_SEVERITY_MAP["info"] == "low"
