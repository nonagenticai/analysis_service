"""FetchStage: render page with Playwright and produce BeautifulSoup."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from bs4 import BeautifulSoup

from src.config import get_settings
from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.security import ensure_public_url_resolved
from src.shared.security.browser_gate import browser_slot
from src.shared.security.playwright_guard import install_ssrf_guard

log = logging.getLogger(__name__)


class _Closeable(Protocol):
    """Anything Playwright hands us that must be closed: Page, Context, Browser."""

    async def close(self) -> None: ...


async def _close_quietly(*closeables: _Closeable) -> None:
    """Close each argument in order, never raising on a teardown-only failure.

    One coroutine so a single ``asyncio.shield`` covers every await — see the
    call sites for why the gaps between them must not be cancellable.
    """
    for closeable in closeables:
        try:
            await closeable.close()
        except Exception as exc:  # noqa: BLE001 — teardown must not mask the cause
            log.debug("brand fetch: teardown close failed: %s", exc)


JS_EXTRACT_TEXT = """
() => {
    const clone = document.body.cloneNode(true);
    for (const el of clone.querySelectorAll('script, style, nav, footer, noscript')) {
        el.remove();
    }
    return clone.innerText.trim();
}
"""

JS_EXTRACT_CSS_CUSTOM_PROPERTIES = """
() => {
    const props = {};
    for (const sheet of document.styleSheets) {
        try {
            for (const rule of sheet.cssRules) {
                if (rule.selectorText === ':root' || rule.selectorText === ':root, :host') {
                    for (let i = 0; i < rule.style.length; i++) {
                        const name = rule.style[i];
                        if (name.startsWith('--')) {
                            props[name] = rule.style.getPropertyValue(name).trim();
                        }
                    }
                }
            }
        } catch (e) {
            // Cross-origin stylesheet, skip
        }
    }
    return props;
}
"""


class FetchStage(BaseStage):
    """Render page with Playwright, capture screenshot, extract text and styles."""

    name = "fetch"

    def should_run(self, ctx: PipelineContext) -> bool:
        return bool(ctx.url)

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        from playwright.async_api import async_playwright

        # SSRF gate. ``ctx.url`` is tenant-supplied (/brand/from-url,
        # /brand/batch-extract, and the ``https://{domain}`` synthesised by
        # /brands/compare), and a headless Chromium that will screenshot whatever
        # it is pointed at is the most useful SSRF sink in the service. Raising
        # here fails the ``fetch`` stage, which ``PipelineRunner`` treats as
        # fatal -- so no downstream stage gets a page to harvest URLs from.
        # Residual: Playwright re-resolves the name after this check, so a
        # DNS-rebind TOCTOU remains (see ``src/domains/brand/security.py``).
        #
        # This check is still REQUIRED and is not made redundant by the route
        # guard installed below: ``file://`` / ``chrome://`` are loaded by
        # Chromium without going through the network stack, so a route handler
        # is never consulted for them. It is also the only check that can refuse
        # before paying for a browser launch. The two are complementary.
        #
        # Resolving form: the structural screen is blind to a public name whose
        # ``A`` record points into the estate, which is the whole bypass this
        # gate exists to stop.
        await ensure_public_url_resolved(ctx.url)

        pw_settings = get_settings().playwright
        self.logger.info("Rendering URL with Playwright, url=%s", ctx.url)

        # Browser-concurrency gate. Held across the browser's whole lifetime,
        # not just the launch call -- releasing at launch would let N browsers
        # be resident while only one held a slot. Every entry point into this
        # pipeline funnels through here, so brand has no ungated path to a
        # Chromium; the gate itself is pod-wide and shared with critique's
        # ``AxeAdapter``, which launches its own browser in the same process
        # (see ``src/shared/security/browser_gate.py``).
        async with browser_slot(), async_playwright() as pw:
            browser = await pw.chromium.launch(headless=pw_settings.headless)
            try:
                # An explicit context (rather than ``browser.new_page()``, which
                # makes one implicitly) is what the guard attaches to. Context
                # level -- not page level -- is required: it covers every page,
                # popup, frame and worker in the context, which is where the
                # sub-resource and redirect requests actually originate.
                context = await browser.new_context(
                    viewport={
                        "width": pw_settings.viewport_width,
                        "height": pw_settings.viewport_height,
                    }
                )
                await install_ssrf_guard(context)
                page = await context.new_page()
                try:
                    await page.goto(
                        ctx.url,
                        wait_until=pw_settings.wait_until,
                        timeout=pw_settings.timeout_ms,
                    )

                    ctx.screenshot_bytes = await page.screenshot(full_page=True)
                    ctx.html = await page.content()
                    ctx.soup = BeautifulSoup(ctx.html, "lxml")

                    ctx.page_text = await page.evaluate(JS_EXTRACT_TEXT)
                    ctx.computed_styles = await page.evaluate(JS_EXTRACT_CSS_CUSTOM_PROPERTIES)

                    self.logger.info(
                        "Page rendered, url=%s, html_size=%d, screenshot_size=%d, "
                        "text_len=%d, css_vars=%d",
                        ctx.url,
                        len(ctx.html),
                        len(ctx.screenshot_bytes),
                        len(ctx.page_text),
                        len(ctx.computed_styles),
                    )
                finally:
                    # SHIELDED, and as ONE coroutine. Every await here is a
                    # cancellation point, and this `finally` is reached ON the
                    # cancellation path — a stage timeout (`wait_for`) or a run
                    # cancel. Unshielded, a cancel delivered at `page.close()`
                    # abandoned `context.close()` AND the outer `browser.close()`
                    # below, leaving a headless Chromium alive on the pod while
                    # `browser_slot()` reported the slot free. The slots then leak
                    # one per cancelled extraction until nothing can acquire one.
                    #
                    # Shielding each close separately would leave the gaps between
                    # them cancellable — the same hole, one line narrower.
                    await asyncio.shield(_close_quietly(page, context))
            finally:
                await asyncio.shield(_close_quietly(browser))
