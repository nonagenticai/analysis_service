"""Extended tests for nuclei_adapter — covers scan execution, is_available with binary, error paths."""

import json
from unittest.mock import MagicMock, patch

from src.domains.critique.services.scanners.tool_adapters.nuclei_adapter import NucleiAdapter


class TestNucleiIsAvailable:
    """Test NucleiAdapter.is_available."""

    async def test_is_available_enabled_with_binary(self):
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.shutil"
            ) as mock_sh,
        ):
            ms.return_value.external_tools.nuclei_enabled = True
            ms.return_value.external_tools.nuclei_path = "nuclei"
            mock_sh.which.return_value = "/usr/local/bin/nuclei"

            adapter = NucleiAdapter()
            result = await adapter.is_available()
            assert result is True


class TestNucleiScan:
    """Test NucleiAdapter.scan method."""

    async def test_scan_success(self):
        """Nuclei scan returns parsed findings."""
        adapter = NucleiAdapter()

        entry = {
            "template-id": "cve-2023-12345",
            "matched-at": "https://example.com/api",
            "info": {
                "name": "CVE-2023-12345",
                "severity": "high",
                "description": "A vulnerability found",
                "remediation": "Update to latest version",
                "classification": {"cve-id": "CVE-2023-12345"},
            },
        }
        stdout_data = json.dumps(entry).encode()

        mock_proc = MagicMock()
        mock_proc.returncode = 1  # nuclei returns 1 when findings exist
        mock_proc.communicate = MagicMock(return_value=(stdout_data, b""))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(stdout_data, b"")),
        ):
            ms.return_value.external_tools.nuclei_timeout = 60
            ms.return_value.external_tools.nuclei_path = "nuclei"

            findings = await adapter.scan("https://example.com")

        assert len(findings) == 1
        assert findings[0]["check_id"] == "nuclei-cve-2023-12345"
        assert findings[0]["severity"] == "high"

    async def test_scan_no_findings(self):
        """Nuclei scan with clean output."""
        adapter = NucleiAdapter()

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = MagicMock(return_value=(b"", b""))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(b"", b"")),
        ):
            ms.return_value.external_tools.nuclei_timeout = 60
            ms.return_value.external_tools.nuclei_path = "nuclei"

            findings = await adapter.scan("https://example.com")

        assert findings == []

    async def test_scan_timeout(self):
        """Nuclei scan returns empty list on timeout."""
        adapter = NucleiAdapter()

        mock_proc = MagicMock()
        mock_proc.communicate = MagicMock()

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=TimeoutError("nuclei timed out")),
        ):
            ms.return_value.external_tools.nuclei_timeout = 1
            ms.return_value.external_tools.nuclei_path = "nuclei"

            findings = await adapter.scan("https://example.com")

        assert findings == []

    async def test_scan_os_error(self):
        """Nuclei scan returns empty list when binary not found."""
        adapter = NucleiAdapter()

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=FileNotFoundError("nuclei not found"),
            ),
        ):
            ms.return_value.external_tools.nuclei_timeout = 60
            ms.return_value.external_tools.nuclei_path = "nuclei"

            findings = await adapter.scan("https://example.com")

        assert findings == []

    async def test_scan_nonstandard_exit_code(self):
        """Nuclei scan handles non-standard exit codes (not 0 or 1)."""
        adapter = NucleiAdapter()

        mock_proc = MagicMock()
        mock_proc.returncode = 2  # unexpected error exit
        mock_proc.communicate = MagicMock(return_value=(b"", b"nuclei error"))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(b"", b"nuclei error")),
        ):
            ms.return_value.external_tools.nuclei_timeout = 60
            ms.return_value.external_tools.nuclei_path = "nuclei"

            findings = await adapter.scan("https://example.com")

        # Should still return (possibly empty) results
        assert isinstance(findings, list)


class TestNucleiParseJsonlEdgeCases:
    """Additional edge cases for _parse_jsonl."""

    def test_parse_jsonl_missing_info(self):
        """Entry with missing info section."""
        adapter = NucleiAdapter()
        entry = {"template-id": "test-template"}
        result = adapter._parse_jsonl(json.dumps(entry), "https://example.com")
        assert len(result) == 1
        assert result[0]["severity"] == "info"  # unknown severity defaults to info

    def test_parse_jsonl_multiple_entries(self):
        """Multiple JSONL entries parsed correctly."""
        adapter = NucleiAdapter()
        entries = [
            {
                "template-id": "cve-001",
                "info": {"name": "CVE 1", "severity": "high", "classification": {}},
            },
            {
                "template-id": "cve-002",
                "info": {"name": "CVE 2", "severity": "medium", "classification": {}},
            },
        ]
        raw = "\n".join(json.dumps(e) for e in entries)
        result = adapter._parse_jsonl(raw, "https://example.com")
        assert len(result) == 2
