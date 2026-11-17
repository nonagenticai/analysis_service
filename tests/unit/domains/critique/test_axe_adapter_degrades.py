"""AxeAdapter.scan must turn real Playwright exceptions into a typed FAILURE.

It must neither kill the run (Playwright exceptions escaping) NOR report the
failure as a clean page (``return []``). Since 2026-10-18 it raises
``AdapterScanError``, which ``ToolAdapterRegistry`` isolates per adapter and
records as ``status="failed"``. The live defect: nonagentic.ai's CSP blocked the
injected axe ``<script>``, ``Page.evaluate`` rejected with a bare ``Event``, and
the audit then reported 0 accessibility violations.

``scan`` drives a live browser, and Playwright's ``Error`` /``TimeoutError``
derive from ``Exception`` only. The shared-name trap is the whole point:
Playwright's ``TimeoutError`` is a DIFFERENT CLASS from the builtin, so the
builtin (an ``OSError``) in the ``except`` tuple never caught a navigation
timeout. These tests therefore inject the REAL ``playwright.async_api``
classes -- injecting the builtin would pass against the broken adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.domains.critique.services.scanners.tool_adapters import axe_adapter
from src.domains.critique.services.scanners.tool_adapters.axe_adapter import (
    _AXE_INJECT_AND_RUN,
    AxeAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.protocol import AdapterScanError
from src.domains.critique.services.scanners.tool_adapters.registry import ToolAdapterRegistry

AxeResults = dict[str, object]

_VIOLATIONS: AxeResults = {
    "violations": [
        {
            "id": "image-alt",
            "impact": "critical",
            "help": "Images must have alt text",
            "description": "Ensures img elements have alt text",
            "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/image-alt",
            "tags": ["wcag2a", "wcag111"],
            "nodes": [{"target": ["img.hero"]}],
        }
    ]
}


@dataclass(frozen=True)
class _ExternalTools:
    axe_timeout: int = 30
    axe_enabled: bool = True


@dataclass(frozen=True)
class _Settings:
    external_tools: _ExternalTools = field(default_factory=_ExternalTools)


class _FakePage:
    def __init__(
        self,
        results: object,
        goto_error: BaseException | None,
        evaluate_error: BaseException | None = None,
    ) -> None:
        self._results = results
        self._goto_error = goto_error
        self._evaluate_error = evaluate_error

    def set_default_timeout(self, timeout: float) -> None:
        return None

    # ASYNC109: this is a test double mirroring Playwright's real Page.goto
    # signature, which takes `timeout` -- axe_adapter calls it as
    # `page.goto(url, wait_until="load", timeout=timeout_ms)`. Renaming the
    # parameter or swapping in `asyncio.timeout` would break the call the
    # double exists to receive.
    async def goto(
        self,
        url: str,
        wait_until: str = "load",
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors Page.goto, see above
    ) -> None:
        if self._goto_error is not None:
            raise self._goto_error

    async def evaluate(self, expression: str) -> object:
        if self._evaluate_error is not None:
            raise self._evaluate_error
        return self._results


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context
        self.closed = False
        self.context_kwargs: dict[str, object] = {}

    async def new_context(self, **kwargs: object) -> _FakeContext:
        # Accepts **kwargs because the real `Browser.new_context` does -- it
        # takes ~30 keyword options. A double NARROWER than the method it
        # stands in for cannot fail the way production fails, and this one
        # failed in the opposite direction: adding any context option to the
        # adapter made it raise `TypeError`, which the adapter's degrade path
        # swallowed, so a correct production change surfaced here as an
        # unrelated "0 findings" failure. Widened so the double constrains the
        # adapter's behaviour rather than its argument list.
        self.context_kwargs = dict(kwargs)
        return self._context

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser, launch_error: BaseException | None) -> None:
        self._browser = browser
        self._launch_error = launch_error

    async def launch(self, headless: bool = True) -> _FakeBrowser:
        if self._launch_error is not None:
            raise self._launch_error
        return self._browser


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium


class _FakePlaywrightCM:
    def __init__(self, pw: _FakePlaywright) -> None:
        self._pw = pw

    async def __aenter__(self) -> _FakePlaywright:
        return self._pw

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    launch_error: BaseException | None = None,
    goto_error: BaseException | None = None,
    evaluate_error: BaseException | None = None,
    results: object = None,
) -> _FakeBrowser:
    """Patch ``async_playwright`` on the REAL module ``scan`` lazily imports from.

    Replacing ``sys.modules["playwright.async_api"]`` wholesale would also
    replace ``Error``/``TimeoutError`` with mocks -- i.e. destroy the very
    classes under test. Only the entry point is swapped; the exception classes
    the adapter imports stay real.
    """
    page = _FakePage(
        results if results is not None else {"violations": []}, goto_error, evaluate_error
    )
    browser = _FakeBrowser(_FakeContext(page))
    chromium = _FakeChromium(browser, launch_error)
    cm = _FakePlaywrightCM(_FakePlaywright(chromium))

    import playwright.async_api as real_pw_api

    monkeypatch.setattr(real_pw_api, "async_playwright", lambda: cm)
    monkeypatch.setattr(axe_adapter, "get_settings", _Settings)

    async def _no_guard(context: object) -> None:
        return None

    monkeypatch.setattr(axe_adapter, "install_ssrf_guard", _no_guard)
    return browser


def test_playwright_timeout_is_not_the_builtin_and_not_an_oserror() -> None:
    """The hierarchy that made the old except-tuple a no-op."""
    assert PlaywrightTimeoutError is not TimeoutError  # builtin
    assert not issubclass(PlaywrightTimeoutError, OSError)
    assert not issubclass(PlaywrightError, OSError)
    assert not issubclass(PlaywrightError, (ValueError, TypeError, KeyError, RuntimeError))
    assert issubclass(PlaywrightTimeoutError, PlaywrightError)


async def test_scan_raises_typed_failure_on_playwright_error_from_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A browser-launch failure becomes AdapterScanError -- not [] and not a raw Playwright error."""
    _install(monkeypatch, launch_error=PlaywrightError("Executable doesn't exist"))

    with pytest.raises(AdapterScanError, match="Executable doesn't exist"):
        await AxeAdapter().scan("https://example.com")


async def test_scan_raises_typed_failure_on_playwright_timeout_from_goto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A navigation timeout becomes AdapterScanError -- not [] and not a raw Playwright error."""
    _install(monkeypatch, goto_error=PlaywrightTimeoutError("Timeout 30000ms exceeded"))

    with pytest.raises(AdapterScanError, match="Timeout 30000ms exceeded"):
        await AxeAdapter().scan("https://example.com")


async def test_live_csp_event_failure_is_a_failed_status_not_zero_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact 2026-10-18 live shape, end to end through the registry.

    ``Page.evaluate: Event`` used to log a warning and come back as
    ``findings=0`` -- indistinguishable from a page with no a11y violations.
    """
    _install(monkeypatch, evaluate_error=PlaywrightError("Page.evaluate: Event"))
    registry = ToolAdapterRegistry()
    registry.register(AxeAdapter())

    run = await registry.run_adapters_with_status("https://example.com")

    assert run.findings == []
    status = run.statuses["axe-core"]
    assert status.status == "failed"
    assert not status.measured
    assert "Page.evaluate: Event" in (status.error or "")
    assert run.unmeasured == ["axe-core"]


async def test_result_without_violations_list_is_a_failure_not_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``{}`` / ``None`` from evaluate is no measurement -- never 0 violations."""
    _install(monkeypatch, results={"passes": []})

    with pytest.raises(AdapterScanError, match="no violations list"):
        await AxeAdapter().scan("https://example.com")


async def test_context_bypasses_target_csp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Root cause of the live failure: a ``script-src 'self'`` CSP blocks the
    injected cdnjs axe ``<script>``. Reproduced against live nonagentic.ai:
    ``bypass_csp=False`` -> ``Page.evaluate: Event``; ``True`` -> axe runs."""
    browser = _install(monkeypatch)

    await AxeAdapter().scan("https://example.com")

    assert browser.context_kwargs.get("bypass_csp") is True


def test_injected_script_rejects_with_an_error_not_a_dom_event() -> None:
    """``script.onerror = reject`` rejects with the DOM Event, which Playwright
    reports as the bare string "Event" -- no cause, no URL."""
    assert "script.onerror = reject;" not in _AXE_INJECT_AND_RUN
    assert "reject(new Error(" in _AXE_INJECT_AND_RUN


async def test_scan_still_produces_findings_on_the_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the suite must not pass on a ``scan`` that always returns []."""
    _install(monkeypatch, results=_VIOLATIONS)

    findings = await AxeAdapter().scan("https://example.com")

    assert len(findings) == 1
    assert findings[0]["check_id"] == "axe-image-alt"
    assert findings[0]["severity"] == "critical"
    assert findings[0]["wcag_criterion"] == "1.1.1"
