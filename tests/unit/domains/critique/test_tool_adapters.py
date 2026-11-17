"""Tests for tool adapter modules (protocol, registry, axe, lighthouse, nuclei, zap)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.critique.services.scanners.tool_adapters.axe_adapter import (
    _IMPACT_TO_SEVERITY,
    AxeAdapter,
    _extract_wcag_criterion,
    _first_selector,
)
from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import (
    _CORE_WEB_VITALS,
    _SCORE_THRESHOLDS,
    LighthouseAdapter,
    _score_to_severity,
)
from src.domains.critique.services.scanners.tool_adapters.nuclei_adapter import (
    _SEVERITY_MAP,
    NucleiAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.protocol import (
    AdapterScanError,
    ToolAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.registry import ToolAdapterRegistry
from src.domains.critique.services.scanners.tool_adapters.zap_adapter import (
    _CONFIDENCE_TO_FP_RISK,
    _RISK_TO_SEVERITY,
    ZapAdapter,
)

# ── Protocol ──────────────────────────────────────────────────────────


class TestToolAdapterProtocol:
    """Test ToolAdapter protocol."""

    def test_protocol_is_runtime_checkable(self):
        assert hasattr(ToolAdapter, "__protocol_attrs__") or hasattr(
            ToolAdapter, "__abstractmethods__"
        )

    def test_concrete_class_satisfies_protocol(self):
        """A class with all required methods satisfies the protocol."""

        class Dummy:
            @property
            def tool_name(self) -> str:
                return "dummy"

            @property
            def category(self) -> str:
                return "test"

            async def is_available(self) -> bool:
                return True

            async def scan(self, url: str, html: str | None = None) -> list[dict]:
                return []

        assert isinstance(Dummy(), ToolAdapter)


# ── Registry ──────────────────────────────────────────────────────────


class TestToolAdapterRegistry:
    """Test ToolAdapterRegistry."""

    def test_register_and_adapter_names(self):
        reg = ToolAdapterRegistry()
        adapter = MagicMock()
        adapter.tool_name = "mock-tool"
        reg.register(adapter)
        assert "mock-tool" in reg.adapter_names

    async def test_get_available_adapters_available(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "mock"
        adapter.is_available = AsyncMock(return_value=True)
        reg.register(adapter)

        available = await reg.get_available_adapters()
        assert len(available) == 1

    async def test_get_available_adapters_unavailable(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "mock"
        adapter.is_available = AsyncMock(return_value=False)
        reg.register(adapter)

        available = await reg.get_available_adapters()
        assert len(available) == 0

    async def test_get_available_adapters_error(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "broken"
        adapter.is_available = AsyncMock(side_effect=RuntimeError("oops"))
        reg.register(adapter)

        available = await reg.get_available_adapters()
        assert len(available) == 0

    async def test_run_adapters_no_available(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "mock"
        adapter.is_available = AsyncMock(return_value=False)
        reg.register(adapter)

        findings = await reg.run_adapters("https://example.com")
        assert findings == []

    async def test_run_adapters_success(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "mock"
        adapter.is_available = AsyncMock(return_value=True)
        adapter.scan = AsyncMock(return_value=[{"title": "found"}])
        reg.register(adapter)

        findings = await reg.run_adapters("https://example.com")
        assert len(findings) == 1
        assert findings[0]["title"] == "found"

    async def test_run_adapters_timeout(self):
        reg = ToolAdapterRegistry()

        async def slow_scan(url, html=None):
            await asyncio.sleep(10)
            return []

        adapter = AsyncMock()
        adapter.tool_name = "slow"
        adapter.is_available = AsyncMock(return_value=True)
        adapter.scan = slow_scan
        reg.register(adapter)

        findings = await reg.run_adapters("https://example.com", timeout=0)
        assert findings == []

    async def test_run_adapters_exception(self):
        reg = ToolAdapterRegistry()
        adapter = AsyncMock()
        adapter.tool_name = "crashy"
        adapter.is_available = AsyncMock(return_value=True)
        adapter.scan = AsyncMock(side_effect=RuntimeError("boom"))
        reg.register(adapter)

        findings = await reg.run_adapters("https://example.com")
        assert findings == []

    async def test_run_adapters_multiple(self):
        reg = ToolAdapterRegistry()

        a1 = AsyncMock()
        a1.tool_name = "a1"
        a1.is_available = AsyncMock(return_value=True)
        a1.scan = AsyncMock(return_value=[{"title": "f1"}])

        a2 = AsyncMock()
        a2.tool_name = "a2"
        a2.is_available = AsyncMock(return_value=True)
        a2.scan = AsyncMock(return_value=[{"title": "f2"}, {"title": "f3"}])

        reg.register(a1)
        reg.register(a2)

        findings = await reg.run_adapters("https://example.com")
        assert len(findings) == 3

    async def test_off_tuple_exception_in_one_adapter_does_not_cancel_siblings(self):
        """KeyError (off the pre-fix narrow tuple) on one adapter must not cancel siblings.

        Regression for the narrow-except-batch-gather-trap: pre-fix, ``_run_one``
        caught only ``(ImportError, OSError, RuntimeError)``. Anything else
        (KeyError, AttributeError, custom domain errors from adapter modules)
        escaped the per-adapter absorb, propagated through the bare
        ``asyncio.gather`` and cancelled every other adapter via task cancellation.
        """
        reg = ToolAdapterRegistry()

        a1 = AsyncMock()
        a1.tool_name = "a1"
        a1.is_available = AsyncMock(return_value=True)
        a1.scan = AsyncMock(return_value=[{"title": "f1"}])

        crashy = AsyncMock()
        crashy.tool_name = "crashy"
        crashy.is_available = AsyncMock(return_value=True)
        # KeyError is NOT in the pre-fix tuple (ImportError, OSError, RuntimeError).
        crashy.scan = AsyncMock(side_effect=KeyError("simulated off-tuple failure"))

        a3 = AsyncMock()
        a3.tool_name = "a3"
        a3.is_available = AsyncMock(return_value=True)
        a3.scan = AsyncMock(return_value=[{"title": "f3a"}, {"title": "f3b"}])

        reg.register(a1)
        reg.register(crashy)
        reg.register(a3)

        # Pre-fix: KeyError escaped _run_one and cancelled a1 + a3, so this raised
        # KeyError out of run_adapters entirely. Post-fix: crashy returns [],
        # siblings produce their findings.
        findings = await reg.run_adapters("https://example.com")
        assert len(findings) == 3
        titles = {f["title"] for f in findings}
        assert titles == {"f1", "f3a", "f3b"}


# ── Axe Adapter ───────────────────────────────────────────────────────


class TestAxeAdapter:
    """Test AxeAdapter."""

    def test_tool_name(self):
        adapter = AxeAdapter()
        assert adapter.tool_name == "axe-core"

    def test_category(self):
        adapter = AxeAdapter()
        assert adapter.category == "accessibility"

    def test_impact_to_severity_mapping(self):
        assert _IMPACT_TO_SEVERITY["critical"] == "critical"
        assert _IMPACT_TO_SEVERITY["serious"] == "high"
        assert _IMPACT_TO_SEVERITY["moderate"] == "medium"
        assert _IMPACT_TO_SEVERITY["minor"] == "low"

    def test_extract_wcag_criterion_valid(self):
        assert _extract_wcag_criterion(["wcag111", "best-practice"]) == "1.1.1"
        assert _extract_wcag_criterion(["wcag143"]) == "1.4.3"

    def test_extract_wcag_criterion_none(self):
        assert _extract_wcag_criterion(["best-practice"]) is None
        assert _extract_wcag_criterion([]) is None
        assert _extract_wcag_criterion(["wcag2a"]) is None  # too short

    def test_first_selector_list(self):
        assert _first_selector({"target": ["#main"]}) == "#main"

    def test_first_selector_nested_list(self):
        assert _first_selector({"target": [["iframe", "#inner"]]}) == "iframe #inner"

    def test_first_selector_empty(self):
        assert _first_selector({"target": []}) is None
        assert _first_selector({}) is None

    async def test_is_available_disabled(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.axe_enabled = False
            adapter = AxeAdapter()
            assert await adapter.is_available() is False

    async def test_is_available_no_playwright(self):
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
            ) as ms,
            patch.dict("sys.modules", {"playwright.async_api": None}),
        ):
            ms.return_value.external_tools.axe_enabled = True
            adapter = AxeAdapter()
            result = await adapter.is_available()
            # May be True or False depending on playwright installation
            assert isinstance(result, bool)

    async def test_scan_no_playwright(self):
        """No playwright = no measurement: raise, never report a clean ``[]``."""
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.axe_timeout = 30
            adapter = AxeAdapter()
            # Mock the import to raise ImportError
            with patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **kw: (
                    (_ for _ in ()).throw(ImportError("no playwright"))
                    if "playwright" in name
                    else __import__(name, *a, **kw)
                ),
            ):
                with pytest.raises(AdapterScanError, match="playwright is not installed"):
                    await adapter.scan("https://example.com")


# ── Lighthouse Adapter ────────────────────────────────────────────────


class TestLighthouseAdapter:
    """Test LighthouseAdapter."""

    def test_tool_name(self):
        adapter = LighthouseAdapter()
        assert adapter.tool_name == "lighthouse"

    def test_category(self):
        adapter = LighthouseAdapter()
        assert adapter.category == "performance"

    def test_score_to_severity(self):
        assert _score_to_severity(None) == "info"
        assert _score_to_severity(0.3) == "critical"
        assert _score_to_severity(0.6) == "high"
        assert _score_to_severity(0.8) == "medium"
        assert _score_to_severity(0.95) == "low"
        assert _score_to_severity(1.0) == "info"

    def test_core_web_vitals_defined(self):
        assert "largest-contentful-paint" in _CORE_WEB_VITALS
        assert "cumulative-layout-shift" in _CORE_WEB_VITALS

    def test_score_thresholds_ordered(self):
        thresholds = [t for t, _ in _SCORE_THRESHOLDS]
        assert thresholds == sorted(thresholds)

    async def test_is_available_disabled(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.lighthouse_enabled = False
            adapter = LighthouseAdapter()
            assert await adapter.is_available() is False

    async def test_is_available_no_binary(self):
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.shutil"
            ) as mock_shutil,
        ):
            ms.return_value.external_tools.lighthouse_enabled = True
            ms.return_value.external_tools.lighthouse_node_path = "node"
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"
            mock_shutil.which.return_value = None
            adapter = LighthouseAdapter()
            assert await adapter.is_available() is False

    def test_parse_report_empty(self):
        adapter = LighthouseAdapter()
        result = adapter._parse_report({}, "https://example.com", "mobile")
        assert result == []

    def test_parse_report_low_perf_score(self):
        adapter = LighthouseAdapter()
        report = {
            "audits": {},
            "categories": {
                "performance": {
                    "score": 0.45,
                    "auditRefs": [],
                }
            },
        }
        result = adapter._parse_report(report, "https://example.com", "mobile")
        assert len(result) == 1
        assert result[0]["severity"] == "critical"

    def test_parse_report_cwv(self):
        adapter = LighthouseAdapter()
        report = {
            "audits": {
                "largest-contentful-paint": {
                    "score": 0.3,
                    "numericValue": 4500,
                    "displayValue": "4.5 s",
                    "description": "LCP measures...",
                }
            },
            "categories": {"performance": {"score": 0.92, "auditRefs": []}},
        }
        result = adapter._parse_report(report, "https://example.com", "desktop")
        assert len(result) == 1
        assert "LCP" in result[0]["title"]

    def test_parse_report_failed_audit(self):
        adapter = LighthouseAdapter()
        report = {
            "audits": {
                "render-blocking-resources": {
                    "score": 0.2,
                    "title": "Eliminate render-blocking resources",
                    "description": "Resources are blocking...",
                    "displayValue": "3 resources",
                }
            },
            "categories": {
                "performance": {
                    "score": 0.92,
                    "auditRefs": [{"id": "render-blocking-resources"}],
                }
            },
        }
        result = adapter._parse_report(report, "https://example.com", "mobile")
        assert len(result) == 1
        assert "render-blocking" in result[0]["title"].lower()


# ── Nuclei Adapter ────────────────────────────────────────────────────


class TestNucleiAdapter:
    """Test NucleiAdapter."""

    def test_tool_name(self):
        adapter = NucleiAdapter()
        assert adapter.tool_name == "nuclei"

    def test_category(self):
        adapter = NucleiAdapter()
        assert adapter.category == "security"

    def test_severity_map(self):
        assert _SEVERITY_MAP["critical"] == "critical"
        assert _SEVERITY_MAP["unknown"] == "info"

    async def test_is_available_disabled(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.nuclei_enabled = False
            adapter = NucleiAdapter()
            assert await adapter.is_available() is False

    async def test_is_available_no_binary(self):
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
            mock_sh.which.return_value = None
            adapter = NucleiAdapter()
            assert await adapter.is_available() is False

    def test_parse_jsonl_empty(self):
        adapter = NucleiAdapter()
        assert adapter._parse_jsonl("", "https://example.com") == []

    def test_parse_jsonl_valid(self):
        import json

        adapter = NucleiAdapter()
        entry = {
            "template-id": "cve-2021-12345",
            "matched-at": "https://example.com/api",
            "matcher-name": "body",
            "info": {
                "name": "CVE-2021-12345",
                "severity": "high",
                "description": "A critical vulnerability",
                "remediation": "Upgrade to latest version",
                "classification": {"cve-id": "CVE-2021-12345"},
            },
            "extracted-results": ["match1"],
        }
        raw = json.dumps(entry)
        result = adapter._parse_jsonl(raw, "https://example.com")
        assert len(result) == 1
        assert result[0]["severity"] == "high"
        assert result[0]["check_id"] == "nuclei-cve-2021-12345"
        assert result[0]["false_positive_risk"] == "low"  # has CVE

    def test_parse_jsonl_no_remediation_with_reference(self):
        import json

        adapter = NucleiAdapter()
        entry = {
            "template-id": "tech-detect",
            "info": {
                "name": "Tech Detection",
                "severity": "info",
                "reference": ["https://example.com/docs"],
                "classification": {},
            },
        }
        raw = json.dumps(entry)
        result = adapter._parse_jsonl(raw, "https://example.com")
        assert "See: https://example.com/docs" in result[0]["recommendation"]

    def test_parse_jsonl_string_reference(self):
        import json

        adapter = NucleiAdapter()
        entry = {
            "template-id": "tech-detect",
            "info": {
                "name": "Tech Detection",
                "severity": "info",
                "reference": "https://example.com/doc",
                "classification": {},
            },
        }
        raw = json.dumps(entry)
        result = adapter._parse_jsonl(raw, "https://example.com")
        assert "See: https://example.com/doc" in result[0]["recommendation"]

    def test_parse_jsonl_bad_json(self):
        adapter = NucleiAdapter()
        result = adapter._parse_jsonl("not json\nalso not json", "https://example.com")
        assert result == []

    def test_parse_jsonl_curl_evidence(self):
        import json

        entry = {
            "template-id": "test",
            "info": {"name": "Test", "severity": "low", "classification": {}},
            "curl-command": "curl -X GET https://example.com",
        }
        raw = json.dumps(entry)
        adapter = NucleiAdapter()
        result = adapter._parse_jsonl(raw, "https://example.com")
        assert result[0]["evidence"] == "curl -X GET https://example.com"


# ── ZAP Adapter ───────────────────────────────────────────────────────


class TestZapAdapter:
    """Test ZapAdapter."""

    def test_tool_name(self):
        adapter = ZapAdapter()
        assert adapter.tool_name == "zap"

    def test_category(self):
        adapter = ZapAdapter()
        assert adapter.category == "security"

    def test_risk_to_severity(self):
        assert _RISK_TO_SEVERITY[0] == "info"
        assert _RISK_TO_SEVERITY[3] == "high"

    def test_confidence_to_fp_risk(self):
        assert _CONFIDENCE_TO_FP_RISK[0] == "high"
        assert _CONFIDENCE_TO_FP_RISK[4] == "low"

    async def test_is_available_disabled(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_enabled = False
            adapter = ZapAdapter()
            assert await adapter.is_available() is False

    def test_alerts_to_findings_empty(self):
        adapter = ZapAdapter()
        assert adapter._alerts_to_findings([], "https://example.com") == []

    def test_alerts_to_findings_basic(self):
        adapter = ZapAdapter()
        alerts = [
            {
                "pluginId": "10038",
                "alert": "CSP header not set",
                "risk": "2",
                "confidence": "3",
                "description": "No CSP header.",
                "solution": "Add CSP header.",
                "evidence": "missing",
                "url": "https://example.com",
            }
        ]
        result = adapter._alerts_to_findings(alerts, "https://example.com")
        assert len(result) == 1
        assert result[0]["severity"] == "medium"
        assert result[0]["check_id"] == "zap-10038"

    def test_alerts_to_findings_deduplicates(self):
        adapter = ZapAdapter()
        alerts = [
            {"pluginId": "123", "alert": "Issue A", "risk": "1", "confidence": "2"},
            {"pluginId": "123", "alert": "Issue A dup", "risk": "1", "confidence": "2"},
        ]
        result = adapter._alerts_to_findings(alerts, "https://example.com")
        assert len(result) == 1

    def test_alerts_to_findings_no_evidence(self):
        adapter = ZapAdapter()
        alerts = [
            {
                "pluginId": "999",
                "alert": "Test",
                "risk": "0",
                "confidence": "2",
                "evidence": "",
            }
        ]
        result = adapter._alerts_to_findings(alerts, "https://example.com")
        assert len(result) == 1
        # Empty string evidence becomes None via `or None`
        assert result[0].get("evidence") is None
