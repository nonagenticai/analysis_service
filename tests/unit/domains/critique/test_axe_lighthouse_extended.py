"""Extended tests for axe_adapter and lighthouse_adapter — covers scan execution paths."""

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import playwright.async_api as _real_playwright
import pytest

from src.domains.critique.services.scanners.tool_adapters.axe_adapter import AxeAdapter
from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import (
    LighthouseAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.protocol import AdapterScanError


def _setup_playwright_mock(axe_results=None, side_effect=None):
    """Create a mock playwright module with async_playwright context manager."""
    mock_page = AsyncMock()
    mock_page.set_default_timeout = MagicMock()
    mock_page.goto = AsyncMock()
    if axe_results is not None:
        mock_page.evaluate = AsyncMock(return_value=axe_results)
    else:
        mock_page.evaluate = AsyncMock(return_value={"violations": []})

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_pw = AsyncMock()
    mock_pw.chromium = AsyncMock()
    mock_pw.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_cm = AsyncMock()
    if side_effect:
        mock_pw_cm.__aenter__ = AsyncMock(side_effect=side_effect)
    else:
        mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_pw)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=False)

    mock_async_playwright = MagicMock(return_value=mock_pw_cm)

    # Create a fake playwright.async_api module
    mock_module = MagicMock()
    mock_module.async_playwright = mock_async_playwright
    # Carry the REAL Error/TimeoutError through the swap. `scan` imports them
    # from this module to catch them, and a MagicMock attribute is not a class,
    # so `except (..., PlaywrightError)` would raise "catching classes that do
    # not inherit from BaseException". Swapping a module wholesale silently
    # removes whatever the code under test imports from it besides the one
    # symbol you meant to fake.
    mock_module.Error = _real_playwright.Error
    mock_module.TimeoutError = _real_playwright.TimeoutError
    return mock_module


class TestAxeAdapterScan:
    """Test AxeAdapter.scan with mocked Playwright."""

    async def test_scan_with_violations(self):
        """Scan returns findings for axe violations."""
        adapter = AxeAdapter()

        axe_results = {
            "violations": [
                {
                    "id": "color-contrast",
                    "impact": "serious",
                    "help": "Elements must have sufficient color contrast",
                    "description": "Ensures color contrast ratio meets WCAG 2 AA",
                    "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/color-contrast",
                    "tags": ["wcag2aa", "wcag143"],
                    "nodes": [
                        {"target": ["#low-contrast-text"]},
                        {"target": ["p.dim"]},
                    ],
                },
                {
                    "id": "image-alt",
                    "impact": "critical",
                    "help": "Images must have alt text",
                    "description": "Ensures img elements have alt text",
                    "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/image-alt",
                    "tags": ["wcag2a", "wcag111"],
                    "nodes": [{"target": ["img.hero"]}],
                },
            ]
        }

        mock_pw_module = _setup_playwright_mock(axe_results=axe_results)

        saved = sys.modules.get("playwright.async_api")
        sys.modules["playwright.async_api"] = mock_pw_module

        try:
            with patch(
                "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
            ) as ms:
                ms.return_value.external_tools.axe_timeout = 30
                findings = await adapter.scan("https://example.com")
        finally:
            if saved is not None:
                sys.modules["playwright.async_api"] = saved
            else:
                sys.modules.pop("playwright.async_api", None)

        assert len(findings) == 2
        assert findings[0]["check_id"] == "axe-color-contrast"
        assert findings[0]["severity"] == "high"  # serious -> high
        assert findings[0]["wcag_criterion"] == "1.4.3"
        assert findings[1]["check_id"] == "axe-image-alt"
        assert findings[1]["severity"] == "critical"

    async def test_scan_exception_raises_typed_failure(self):
        """A crash is a FAILED scan (AdapterScanError), never a clean ``[]``."""
        adapter = AxeAdapter()

        mock_pw_module = _setup_playwright_mock(side_effect=RuntimeError("Browser crashed"))

        saved = sys.modules.get("playwright.async_api")
        sys.modules["playwright.async_api"] = mock_pw_module

        try:
            with patch(
                "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
            ) as ms:
                ms.return_value.external_tools.axe_timeout = 30
                with pytest.raises(AdapterScanError, match="Browser crashed"):
                    await adapter.scan("https://example.com")
        finally:
            if saved is not None:
                sys.modules["playwright.async_api"] = saved
            else:
                sys.modules.pop("playwright.async_api", None)

    async def test_scan_no_violations(self):
        """Scan returns empty list when axe finds no violations."""
        adapter = AxeAdapter()

        mock_pw_module = _setup_playwright_mock(axe_results={"violations": []})

        saved = sys.modules.get("playwright.async_api")
        sys.modules["playwright.async_api"] = mock_pw_module

        try:
            with patch(
                "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
            ) as ms:
                ms.return_value.external_tools.axe_timeout = 30
                findings = await adapter.scan("https://example.com")
        finally:
            if saved is not None:
                sys.modules["playwright.async_api"] = saved
            else:
                sys.modules.pop("playwright.async_api", None)

        assert findings == []

    async def test_is_available_enabled_with_playwright(self):
        """is_available returns True when enabled and playwright importable."""
        mock_pw_module = MagicMock()
        mock_playwright_pkg = MagicMock()
        mock_playwright_pkg.async_api = mock_pw_module

        saved_pkg = sys.modules.get("playwright")
        saved_api = sys.modules.get("playwright.async_api")
        sys.modules["playwright"] = mock_playwright_pkg
        sys.modules["playwright.async_api"] = mock_pw_module

        try:
            with patch(
                "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
            ) as ms:
                ms.return_value.external_tools.axe_enabled = True
                adapter = AxeAdapter()
                result = await adapter.is_available()
                assert result is True
        finally:
            if saved_pkg is not None:
                sys.modules["playwright"] = saved_pkg
            else:
                sys.modules.pop("playwright", None)
            if saved_api is not None:
                sys.modules["playwright.async_api"] = saved_api
            else:
                sys.modules.pop("playwright.async_api", None)


class TestLighthouseAdapterScan:
    """Test LighthouseAdapter.scan and _run_lighthouse."""

    async def test_is_available_enabled_with_binaries(self):
        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.shutil"
            ) as mock_sh,
        ):
            ms.return_value.external_tools.lighthouse_enabled = True
            ms.return_value.external_tools.lighthouse_node_path = "node"
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"
            mock_sh.which.side_effect = lambda x: f"/usr/bin/{x}"

            adapter = LighthouseAdapter()
            result = await adapter.is_available()
            assert result is True

    async def test_run_lighthouse_success(self):
        """Lighthouse run produces findings from a JSON report."""
        adapter = LighthouseAdapter()

        report = {
            "audits": {
                "largest-contentful-paint": {
                    "score": 0.4,
                    "numericValue": 5000,
                    "displayValue": "5.0 s",
                    "description": "LCP measures load time.",
                },
            },
            "categories": {
                "performance": {
                    "score": 0.50,
                    "auditRefs": [],
                }
            },
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = MagicMock(return_value=(b"", b""))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch("asyncio.wait_for", return_value=(b"", b"")),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value=json.dumps(report)),
            patch("pathlib.Path.iterdir", return_value=[]),
            patch("pathlib.Path.rmdir"),
        ):
            ms.return_value.external_tools.lighthouse_timeout = 60
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"

            findings = await adapter._run_lighthouse("https://example.com", "mobile")

        assert len(findings) >= 1

    async def test_run_lighthouse_nonzero_exit(self):
        """Lighthouse returns empty list for non-zero exit code."""
        adapter = LighthouseAdapter()

        mock_proc = MagicMock()
        mock_proc.returncode = 2
        mock_proc.communicate = MagicMock(return_value=(b"", b"error"))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(b"", b"error")),
            patch("pathlib.Path.iterdir", return_value=[]),
            patch("pathlib.Path.rmdir"),
        ):
            ms.return_value.external_tools.lighthouse_timeout = 60
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"

            findings = await adapter._run_lighthouse("https://example.com", "desktop")

        assert findings == []

    async def test_run_lighthouse_timeout(self):
        """Lighthouse returns empty list on timeout."""
        adapter = LighthouseAdapter()

        mock_proc = MagicMock()
        mock_proc.communicate = MagicMock()

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=TimeoutError("timed out")),
            patch("pathlib.Path.iterdir", return_value=[]),
            patch("pathlib.Path.rmdir"),
        ):
            ms.return_value.external_tools.lighthouse_timeout = 1
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"

            findings = await adapter._run_lighthouse("https://example.com", "mobile")

        assert findings == []

    async def test_run_lighthouse_missing_output_file(self):
        """Lighthouse returns empty list when output file is missing."""
        adapter = LighthouseAdapter()

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = MagicMock(return_value=(b"", b""))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(b"", b"")),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.iterdir", return_value=[]),
            patch("pathlib.Path.rmdir"),
        ):
            ms.return_value.external_tools.lighthouse_timeout = 60
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"

            findings = await adapter._run_lighthouse("https://example.com", "mobile")

        assert findings == []

    async def test_scan_runs_both_modes(self):
        """scan() runs lighthouse for both mobile and desktop."""
        adapter = LighthouseAdapter()

        with patch.object(adapter, "_run_lighthouse", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = [{"title": "test"}]
            findings = await adapter.scan("https://example.com")

        assert mock_run.call_count == 2
        call_args = [call.args for call in mock_run.call_args_list]
        modes = [args[1] for args in call_args]
        assert "mobile" in modes
        assert "desktop" in modes
        assert len(findings) == 2

    def test_parse_report_other_failed_audit(self):
        """Tests the 'other failed audits' branch of _parse_report."""
        adapter = LighthouseAdapter()
        report = {
            "audits": {
                "unused-css-rules": {
                    "score": 0.2,
                    "title": "Reduce unused CSS",
                    "description": "Remove unused CSS rules",
                    "displayValue": "200 KB",
                }
            },
            "categories": {
                "performance": {
                    "score": 0.92,
                    "auditRefs": [{"id": "unused-css-rules"}],
                }
            },
        }
        findings = adapter._parse_report(report, "https://example.com", "mobile")
        assert len(findings) == 1
        assert "unused" in findings[0]["title"].lower()

    def test_parse_report_cwv_cls(self):
        """CLS audit branch (no ms suffix)."""
        adapter = LighthouseAdapter()
        report = {
            "audits": {
                "cumulative-layout-shift": {
                    "score": 0.3,
                    "numericValue": 0.42,
                    "displayValue": "0.42",
                    "description": "CLS measures visual stability.",
                }
            },
            "categories": {"performance": {"score": 0.92, "auditRefs": []}},
        }
        findings = adapter._parse_report(report, "https://example.com", "desktop")
        assert len(findings) == 1
        assert "CLS" in findings[0]["title"]

    async def test_run_lighthouse_json_decode_error(self):
        """Lighthouse returns empty list on JSON decode error."""
        adapter = LighthouseAdapter()

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = MagicMock(return_value=(b"", b""))

        with (
            patch(
                "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter.get_settings"
            ) as ms,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", return_value=(b"", b"")),
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_text", return_value="not json{{{"),
            patch("pathlib.Path.iterdir", return_value=[]),
            patch("pathlib.Path.rmdir"),
        ):
            ms.return_value.external_tools.lighthouse_timeout = 60
            ms.return_value.external_tools.lighthouse_cli_path = "lighthouse"

            findings = await adapter._run_lighthouse("https://example.com", "mobile")

        assert findings == []


class TestAxeAdapterSSRFGuard:
    """The axe adapter must install the BrowserContext SSRF guard, in order.

    ``tests/unit/shared/test_playwright_sinks_guarded.py`` proves this
    structurally across the whole tree; this proves it behaviourally for the one
    sink that shipped unguarded -- the guard is handed the *real* context object
    the page is opened on, and is installed before that page exists.

    Ordering is the load-bearing half. A route handler registered after the
    first navigation does not retroactively cover it, so a guard installed
    "somewhere in the function" is not the same as a guard installed in time.
    """

    @staticmethod
    def _mock_playwright_with_order_log(calls):
        page = AsyncMock()
        page.set_default_timeout = MagicMock()
        page.goto = AsyncMock(side_effect=lambda *a, **k: calls.append("goto"))
        page.evaluate = AsyncMock(return_value={"violations": []})

        context = AsyncMock()
        context.new_page = AsyncMock(
            side_effect=lambda *a, **k: (calls.append("new_page"), page)[1]
        )

        browser = AsyncMock()
        browser.new_context = AsyncMock(
            side_effect=lambda *a, **k: (calls.append("new_context"), context)[1]
        )
        browser.close = AsyncMock()

        pw = AsyncMock()
        pw.chromium = AsyncMock()
        pw.chromium.launch = AsyncMock(return_value=browser)

        pw_cm = AsyncMock()
        pw_cm.__aenter__ = AsyncMock(return_value=pw)
        pw_cm.__aexit__ = AsyncMock(return_value=False)

        module = MagicMock()
        module.async_playwright = MagicMock(return_value=pw_cm)
        return module, context

    async def _run_scan(self, calls, guard):
        module, context = self._mock_playwright_with_order_log(calls)

        saved = sys.modules.get("playwright.async_api")
        sys.modules["playwright.async_api"] = module
        try:
            with (
                patch(
                    "src.domains.critique.services.scanners.tool_adapters.axe_adapter.get_settings"
                ) as ms,
                patch(
                    "src.domains.critique.services.scanners.tool_adapters"
                    ".axe_adapter.install_ssrf_guard",
                    guard,
                ),
            ):
                ms.return_value.external_tools.axe_timeout = 30
                await AxeAdapter().scan("https://example.com")
        finally:
            if saved is not None:
                sys.modules["playwright.async_api"] = saved
            else:
                sys.modules.pop("playwright.async_api", None)
        return context

    async def test_guard_is_installed_on_the_context_that_opens_the_page(self):
        calls = []
        # Record the receiver rather than reading ``await_args`` afterwards:
        # a mock that was never awaited has ``await_args is None``, which would
        # make the assertion fail with an AttributeError instead of the reason.
        guarded = []

        async def _guard(ctx, *args, **kwargs):
            calls.append("guard")
            guarded.append(ctx)
            return {}

        guard = AsyncMock(side_effect=_guard)

        context = await self._run_scan(calls, guard)

        guard.assert_awaited_once()
        assert guarded == [context], (
            "the guard was installed on some other object than the BrowserContext "
            f"the scan actually opens its page on: {guarded}"
        )

    async def test_guard_is_installed_before_any_page_exists(self):
        calls = []
        guard = AsyncMock(side_effect=lambda *a, **k: calls.append("guard"))

        await self._run_scan(calls, guard)

        assert calls == ["new_context", "guard", "new_page", "goto"], (
            f"wrong ordering: {calls}. The guard must sit between new_context() "
            "and new_page() -- a handler registered after the first navigation "
            "does not cover it."
        )
