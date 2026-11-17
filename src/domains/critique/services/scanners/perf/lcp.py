"""LCP-related performance checks (render blocking, critical CSS, hero, fonts, hints)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.perf.base import _perf_finding


def check_render_blocking(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    head = soup.find("head")
    if not head:
        return findings

    scripts = head.find_all("script") if isinstance(head, Tag) else []
    blocking = []
    for script in scripts:
        if not isinstance(script, Tag):
            continue
        src = script.get("src")
        # ``async``/``defer`` are HTML BOOLEAN attributes: a bare ``<script async>``
        # parses to the value ``""``, which is falsy, so a truthiness test flagged
        # every correctly-async script (the stock gtag snippet) as HIGH
        # render-blocking. Presence is the only thing that matters. Module
        # scripts are deferred by default.
        if not src or script.has_attr("async") or script.has_attr("defer"):
            continue
        if str(script.get("type", "")).strip().lower() == "module":
            continue
        blocking.append(src)

    if blocking:
        findings.append(
            _perf_finding(
                severity="high",
                title=f"{len(blocking)} render-blocking scripts in <head>",
                description=(
                    f"{len(blocking)} scripts in <head> block rendering. First: {blocking[0]}"
                ),
                url=url,
                recommendation="Add 'async' or 'defer' attribute to scripts in <head>.",
                selector="head > script[src]:not([async]):not([defer])",
                check_id="perf-render-blocking-scripts",
                remediation_effort="medium",
            )
        )

    return findings


# Images treated as the initial viewport: everything in the page chrome
# (header/nav) plus the first few CONTENT images in document order (the hero).
_ABOVE_FOLD_CONTENT_IMAGES = 2
_CHROME_LANDMARKS = ("header", "nav")


def _in_landmark(img: Tag, names: tuple[str, ...]) -> bool:
    return any(img.find_parent(name) is not None for name in names)


def _lazy_loading_is_pointless(img: Tag) -> bool:
    """Vector/inline images cost a few KB; deferring them only delays paint."""
    src = str(img.get("src", "")).strip().lower()
    return not src or src.startswith("data:") or urlparse(src).path.endswith(".svg")


def check_lazy_loading(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag below-the-fold raster images that are not ``loading="lazy"``.

    The fold is estimated from DOCUMENT STRUCTURE, not a raw index: the old
    "first 3 <img> are above the fold" rule counted the header logo as one of
    the three and flagged the footer logo as a below-fold miss. Chrome images
    (header/nav) are never candidates, and SVG/data-URI images are excluded.
    """
    findings: list[dict] = []
    content_images = [
        img
        for img in soup.find_all("img")
        if isinstance(img, Tag) and not _in_landmark(img, _CHROME_LANDMARKS)
    ]
    below_fold = content_images[_ABOVE_FOLD_CONTENT_IMAGES:]
    not_lazy = [
        img
        for img in below_fold
        if str(img.get("loading", "")).lower() != "lazy" and not _lazy_loading_is_pointless(img)
    ]

    if not_lazy:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(not_lazy)} below-fold images without lazy loading",
                description=(f"{len(not_lazy)} images below the fold lack loading='lazy'."),
                url=url,
                recommendation="Add loading='lazy' to images below the initial viewport.",
                selector="img:not([loading])",
                check_id="perf-no-lazy-loading",
                evidence=", ".join(str(img.get("src", "")) for img in not_lazy[:5]),
            )
        )

    return findings


def check_resource_hints(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for preconnect, dns-prefetch, and preload resource hints."""
    findings: list[dict] = []

    all_links = soup.find_all("link")
    rels: set[str] = set()
    for link in all_links:
        if not isinstance(link, Tag):
            continue
        rel = link.get("rel") or []
        if isinstance(rel, list):
            for r in rel:
                rels.add(str(r).lower())
        else:
            rels.add(str(rel).lower())

    has_preconnect = "preconnect" in rels
    has_dns_prefetch = "dns-prefetch" in rels
    has_preload = "preload" in rels

    if not has_preconnect and not has_dns_prefetch:
        findings.append(
            _perf_finding(
                severity="low",
                title="No preconnect or dns-prefetch resource hints",
                description=(
                    "No <link rel='preconnect'> or <link rel='dns-prefetch'> "
                    "tags found. Resource hints can speed up connections to "
                    "third-party domains."
                ),
                url=url,
                recommendation=(
                    "Add <link rel='preconnect'> for critical third-party "
                    "domains (e.g. fonts, CDNs, analytics)."
                ),
                selector="head",
                check_id="perf-no-preconnect",
            )
        )

    if not has_preload:
        findings.append(
            _perf_finding(
                severity="low",
                title="No preload resource hints",
                description=(
                    "No <link rel='preload'> tags found. Preloading critical "
                    "resources can improve Largest Contentful Paint (LCP)."
                ),
                url=url,
                recommendation=(
                    "Add <link rel='preload'> for critical resources such as "
                    "fonts, hero images, or key CSS/JS files."
                ),
                selector="head",
                check_id="perf-no-preload",
            )
        )

    return findings


def check_font_loading(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check font-face declarations and font link tags for display strategy."""
    findings: list[dict] = []

    # Check @font-face in <style> blocks
    style_tags = soup.find_all("style")
    for style_tag in style_tags:
        css_text = style_tag.string or ""
        font_face_blocks = re.findall(
            r"@font-face\s*\{[^}]*\}", css_text, re.IGNORECASE | re.DOTALL
        )
        for block in font_face_blocks:
            has_font_display = re.search(r"font-display\s*:", block, re.IGNORECASE)
            if not has_font_display:
                findings.append(
                    _perf_finding(
                        severity="medium",
                        title="@font-face missing font-display property",
                        description=(
                            "A @font-face declaration lacks the font-display "
                            "property. This can cause invisible text during "
                            "font loading (FOIT)."
                        ),
                        url=url,
                        recommendation=(
                            "Add 'font-display: swap' (or 'fallback'/'optional') "
                            "to @font-face declarations."
                        ),
                        selector="style",
                        check_id="perf-font-no-display",
                    )
                )
            else:
                has_block_value = re.search(r"font-display\s*:\s*block", block, re.IGNORECASE)
                if has_block_value:
                    findings.append(
                        _perf_finding(
                            severity="low",
                            title="@font-face uses font-display: block",
                            description=(
                                "A @font-face declaration uses "
                                "'font-display: block', which causes invisible "
                                "text while the font loads."
                            ),
                            url=url,
                            recommendation=(
                                "Consider using 'font-display: swap' instead "
                                "to show fallback text while the font loads."
                            ),
                            selector="style",
                            check_id="perf-font-display-block",
                        )
                    )

    # Check <link> tags that LOAD font CSS (Google Fonts, Bunny Fonts). Only a
    # stylesheet (or a ``preload as=style``) URL can carry ``display=``; a bare
    # ``<link rel="preconnect" href="https://fonts.googleapis.com">`` has no query
    # string to add it to, so matching on "fonts" in the href flagged it.
    seen: set[str] = set()
    for link in soup.find_all("link"):
        if not isinstance(link, Tag) or not _is_font_css_link(link):
            continue
        href = str(link.get("href", ""))
        if href in seen:
            continue
        seen.add(href)
        if "display" in parse_qs(urlparse(href).query):
            continue
        findings.append(
            _perf_finding(
                severity="low",
                title="Font link missing display=swap parameter",
                description=(
                    f"Font link '{href}' does not include the "
                    "'display=swap' parameter, which may cause "
                    "invisible text during loading."
                ),
                url=url,
                recommendation=(
                    "Add '&display=swap' to the font URL to ensure "
                    "text remains visible during font loading."
                ),
                selector="link[href*='fonts']",
                check_id="perf-font-link-no-display",
            )
        )

    return findings


_FONT_CSS_HOSTS = frozenset({"fonts.googleapis.com", "fonts.bunny.net"})


def _link_rels(link: Tag) -> set[str]:
    rel = link.get("rel") or []
    values = rel if isinstance(rel, list) else str(rel).split()
    return {str(r).lower() for r in values}


def _is_font_css_link(link: Tag) -> bool:
    """True for a <link> that fetches font-service CSS (not a connection hint)."""
    href = str(link.get("href", ""))
    if (urlparse(href).hostname or "").lower() not in _FONT_CSS_HOSTS:
        return False
    rels = _link_rels(link)
    if "stylesheet" in rels:
        return True
    return "preload" in rels and str(link.get("as", "")).lower() == "style"


def hero_image_candidates(images: list) -> list[Tag]:
    candidates: list[Tag] = []
    for img in images:
        if not isinstance(img, Tag):
            continue
        cls_attr = img.get("class") or []
        classes = " ".join(cls_attr if isinstance(cls_attr, list) else [str(cls_attr)])
        img_id = str(img.get("id", ""))
        if any(kw in (classes + " " + img_id).lower() for kw in ("hero", "banner", "main")):
            candidates.append(img)
    if candidates:
        return candidates
    # Fallback: the first CONTENT image. ``images[0]`` is usually the header
    # logo, which is never the LCP element.
    content = [
        img
        for img in images
        if isinstance(img, Tag)
        and not _in_landmark(img, _CHROME_LANDMARKS)
        and not _lazy_loading_is_pointless(img)
    ]
    return content[:1]


def hero_fetchpriority_findings(soup: BeautifulSoup, url: str) -> list[dict]:
    images = soup.find_all("img")
    if not images:
        return []
    hero_candidates = hero_image_candidates(images)
    if not hero_candidates or any(img.get("fetchpriority") for img in hero_candidates):
        return []
    return [
        _perf_finding(
            severity="low",
            title="No fetchpriority on hero/primary image",
            description=(
                "The primary image does not have "
                "fetchpriority='high'. Setting this attribute "
                "helps the browser prioritize the Largest "
                "Contentful Paint (LCP) image."
            ),
            url=url,
            recommendation=("Add fetchpriority='high' to the main hero or above-the-fold image."),
            selector="img",
            check_id="perf-no-fetchpriority",
            remediation_effort="medium",
        )
    ]


def check_web_vitals_hints(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for LCP-related web vitals hints (fetchpriority on hero images)."""
    findings: list[dict] = []
    findings.extend(hero_fetchpriority_findings(soup, url))
    return findings


def check_critical_css(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check if critical CSS is inlined in <head>."""
    findings: list[dict] = []

    stylesheets = soup.find_all("link", attrs={"rel": "stylesheet"})
    head = soup.find("head")
    head_styles = head.find_all("style") if isinstance(head, Tag) else []

    if len(stylesheets) > 0 and len(head_styles) == 0:
        findings.append(
            _perf_finding(
                severity="low",
                title="No critical CSS inlined in <head>",
                description=(
                    f"Found {len(stylesheets)} external stylesheet(s) but "
                    "no inline <style> in <head>. Inlining critical CSS "
                    "improves First Contentful Paint (FCP)."
                ),
                url=url,
                recommendation=(
                    "Extract and inline the critical CSS needed for "
                    "above-the-fold content in a <style> tag in <head>."
                ),
                selector="head",
                check_id="perf-no-critical-css",
                remediation_effort="medium",
            )
        )

    return findings


def check_image_formats(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for images not using modern formats (WebP/AVIF)."""
    findings: list[dict] = []
    legacy_images: list[str] = []

    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        src = str(img.get("src", ""))
        if not src or src.startswith("data:"):
            continue
        # Check if legacy format
        lower_src = src.lower()
        if any(lower_src.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif")):
            # Check if wrapped in <picture> with modern source
            picture = img.find_parent("picture")
            if picture:
                sources = picture.find_all("source")
                has_modern = any(
                    any(fmt in str(s.get("type", "")).lower() for fmt in ("webp", "avif"))
                    for s in sources
                )
                if has_modern:
                    continue
            legacy_images.append(src)

    if legacy_images:
        findings.append(
            _perf_finding(
                severity="low",
                title=f"{len(legacy_images)} images using legacy formats",
                description=(
                    f"{len(legacy_images)} image(s) use JPEG/PNG/GIF without "
                    "modern format alternatives (WebP/AVIF). Modern formats "
                    "can reduce file sizes by 25-50%."
                ),
                url=url,
                recommendation=(
                    "Use <picture> elements with WebP/AVIF <source> "
                    "elements as progressive enhancements."
                ),
                selector="img",
                check_id="perf-no-modern-images",
                remediation_effort="medium",
            )
        )
    return findings


def check_modulepreload(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag pages with ES modules but no modulepreload hints."""
    findings: list[dict] = []
    has_module_script = bool(soup.find("script", attrs={"type": "module"}))
    if not has_module_script:
        return findings

    has_modulepreload = False
    for link in soup.find_all("link"):
        if not isinstance(link, Tag):
            continue
        rel = link.get("rel") or []
        if isinstance(rel, list):
            if "modulepreload" in [str(r).lower() for r in rel]:
                has_modulepreload = True
                break
        elif str(rel).lower() == "modulepreload":
            has_modulepreload = True
            break

    if not has_modulepreload:
        findings.append(
            _perf_finding(
                severity="low",
                title="ES modules loaded without modulepreload",
                description=(
                    "The page uses <script type='module'> but has no "
                    "<link rel='modulepreload'> hints. Without "
                    "modulepreload, the browser discovers module "
                    "dependencies late, causing a waterfall of network "
                    "requests."
                ),
                url=url,
                recommendation=(
                    "Add <link rel='modulepreload' href='...'> for "
                    "critical module dependencies to enable parallel "
                    "fetching."
                ),
                selector='link[rel="modulepreload"]',
                check_id="perf-no-modulepreload",
            )
        )
    return findings
