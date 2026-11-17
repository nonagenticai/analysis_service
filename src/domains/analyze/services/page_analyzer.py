"""Extract structural information from HTML pages."""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup
from bs4.element import Tag

from src.domains.analyze.services.metadata_extractor import MetadataExtractor
from src.domains.analyze.services.page_classifier import PageClassifier

logger = logging.getLogger(__name__)

_TRACKING_IFRAME_PATTERNS: tuple[str, ...] = (
    "consent",
    "cookie",
    "analytics",
    "tracking",
    "doubleclick",
    "googlesyndication",
    "googletagmanager",
    "facebook.com/tr",
    "pixel",
    "ads",
)

_PERIPHERAL_KEYWORDS: tuple[str, ...] = (
    "chatbot",
    "chat-widget",
    "chat-launcher",
    "intercom",
    "drift",
    "crisp",
    "hubspot",
    "livechat",
    "tawk",
    "fb-customerchat",
)


def _parse_html(html: str) -> BeautifulSoup | None:
    if not html or not html.strip():
        return None
    try:
        return BeautifulSoup(html, "lxml")
    except ImportError:
        return BeautifulSoup(html, "html.parser")


def _is_peripheral(element: Tag) -> bool:
    peripheral_tags = {"footer", "header", "nav", "aside"}
    for ancestor in element.parents:
        if ancestor.name in peripheral_tags:
            return True
        raw_classes = ancestor.get("class") if isinstance(ancestor, Tag) else None
        classes = " ".join(raw_classes).lower() if isinstance(raw_classes, list) else ""
        el_id = str(ancestor.get("id") or "").lower() if isinstance(ancestor, Tag) else ""
        haystack = classes + " " + el_id
        if any(kw in haystack for kw in _PERIPHERAL_KEYWORDS):
            return True
    return False


def _build_selector(el: Tag) -> str:
    tag = el.name or "div"
    if el.get("id"):
        return f"#{el['id']}"
    if el.get("data-testid"):
        return f'[data-testid="{el["data-testid"]}"]'
    classes = el.get("class") or []
    if classes:
        cls_list = classes if isinstance(classes, list) else [str(classes)]
        selector = f"{tag}.{'.'.join(str(c) for c in cls_list[:3])}"
        parent = el.parent
        if parent and parent.name and parent.name != "[document]":
            if parent.get("id"):
                return f"#{parent['id']} > {selector}"
            parent_classes = parent.get("class") or []
            if parent_classes:
                pcls_list = (
                    parent_classes if isinstance(parent_classes, list) else [str(parent_classes)]
                )
                parent_sel = f"{parent.name}.{'.'.join(str(c) for c in pcls_list[:2])}"
                return f"{parent_sel} > {selector}"
        return selector
    return tag


class PageAnalyzer:
    """Extract interactive elements, content sections, and navigation from HTML.

    Attributes:
        max_interactive_elements: Cap on interactive elements per page.
        max_sections: Cap on content sections per page.
        classifier: PageClassifier for type detection.
    """

    def __init__(
        self,
        max_interactive_elements: int = 50,
        max_sections: int = 30,
    ) -> None:
        self.max_interactive_elements = max_interactive_elements
        self.max_sections = max_sections
        self.classifier = PageClassifier()

    def analyze(
        self,
        page_id: str,
        url: str,
        title: str,
        html: str,
        text: str = "",
        word_count: int | None = None,
    ) -> dict:
        """Analyze a single page and return structured data.

        Args:
            page_id: Unique page identifier.
            url: Page URL.
            title: Page title.
            html: Raw HTML content.
            text: Pre-extracted text content.
            word_count: Pre-computed word count, if available.

        Returns:
            Dict with page_type, interactive_elements, sections, navigation, etc.
        """
        soup = _parse_html(html)

        interactive = self._extract_interactive(soup) if soup else []
        if soup:
            sections = self._extract_sections(soup)
            headings = self._extract_headings(soup)
        elif text:
            headings = self._extract_headings_from_text(text)
            sections = self._extract_sections_from_text(text, headings)
        else:
            sections = []
            headings = []
        navigation = self._extract_navigation(soup) if soup else []
        has_forms = self._detect_forms(soup) if soup else False
        has_media = self._detect_media(soup) if soup else False
        meta_desc = ""
        if soup:
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                meta_desc = str(meta["content"])

        metadata = MetadataExtractor.extract(soup) if soup else {}
        logo_candidates = self._extract_logo_candidates(soup) if soup else []

        text = text or ""
        actual_word_count = word_count or len(text.split())

        page_type = self.classifier.classify(
            url=url,
            has_forms=has_forms,
            interactive_elements=interactive,
            content_sections=sections,
        )

        return {
            "page_id": page_id,
            "url": url,
            "title": title,
            "page_type": page_type.value,
            "interactive_elements": interactive,
            "content_sections": sections,
            "navigation": navigation,
            "headings": headings,
            "word_count": actual_word_count,
            "has_forms": has_forms,
            "has_media": has_media,
            "meta_description": meta_desc,
            "metadata": metadata,
            "logo_candidates": logo_candidates,
        }

    @staticmethod
    def _detect_media(soup: BeautifulSoup) -> bool:
        if soup.find(["video", "audio", "img", "picture", "source"]):
            return True
        for iframe in soup.find_all("iframe"):
            src = str(iframe.get("src") or "").lower()
            if not src:
                continue
            if any(pat in src for pat in _TRACKING_IFRAME_PATTERNS):
                continue
            return True
        return False

    @staticmethod
    def _detect_forms(soup: BeautifulSoup) -> bool:
        for form in soup.find_all("form"):
            if not _is_peripheral(form):
                return True
        for btn in soup.find_all("button", attrs={"type": "submit"}):
            if not btn.find_parent("form") and not _is_peripheral(btn):
                return True
        inputs_outside_form = [
            inp
            for inp in soup.find_all("input")
            if not inp.find_parent("form") and not _is_peripheral(inp)
        ]
        return len(inputs_outside_form) >= 2

    def _extract_interactive(self, soup: BeautifulSoup) -> list[dict]:
        elements: list[dict] = []
        remaining = self.max_interactive_elements
        for tag_name in ("button", "input", "select", "textarea", "a"):
            if remaining <= 0:
                break
            for el in soup.find_all(tag_name, limit=remaining):
                elements.append(
                    {
                        "tag": tag_name,
                        "selector": _build_selector(el),
                        "text": (el.get_text(strip=True) or "")[:100],
                        "element_type": str(el.get("type") or ""),
                        "name": str(el.get("name") or ""),
                        "aria_label": str(el.get("aria-label") or ""),
                    }
                )
                remaining -= 1
        return elements

    def _extract_sections(self, soup: BeautifulSoup) -> list[dict]:
        sections: list[dict] = []
        section_keywords = {
            "hero": ["hero", "banner", "jumbotron", "masthead", "splash"],
            "features": [
                "features",
                "feature",
                "benefits",
                "capabilities",
                "highlights",
                "advantage",
                "why-us",
                "why-choose",
            ],
            "testimonials": [
                "testimonials",
                "testimonial",
                "reviews",
                "review",
                "quotes",
                "social-proof",
                "customer-stories",
                "customer-reviews",
                "success-stories",
            ],
            "cta": ["cta", "call-to-action", "signup", "get-started"],
            "pricing": ["pricing", "plans", "tiers", "packages"],
            "footer": ["footer", "site-footer"],
        }
        for section_el in soup.find_all(
            ["section", "main", "header", "article", "aside"],
            limit=self.max_sections,
        ):
            raw_classes = section_el.get("class")
            classes = (
                " ".join(raw_classes) if isinstance(raw_classes, list) else str(raw_classes or "")
            )
            el_id = str(section_el.get("id") or "")
            section_type = "content"
            for stype, keywords in section_keywords.items():
                if any(kw in classes.lower() or kw in el_id.lower() for kw in keywords):
                    section_type = stype
                    break
            heading = ""
            h_tag = section_el.find(["h1", "h2", "h3"])
            if h_tag:
                heading = h_tag.get_text(strip=True)[:100]
            text = section_el.get_text(strip=True)[:200]
            sections.append(
                {
                    "section_type": section_type,
                    "heading": heading,
                    "text_preview": text,
                    "selector": _build_selector(section_el),
                    "word_count": len(section_el.get_text().split()),
                }
            )
        return sections

    def _extract_navigation(self, soup: BeautifulSoup) -> list[dict]:
        nav_elements: list[dict] = []
        for nav in soup.find_all("nav", limit=5):
            items = [a.get_text(strip=True) for a in nav.find_all("a", limit=20)]
            nav_elements.append(
                {
                    "nav_type": "menu",
                    "items": items,
                    "selector": _build_selector(nav),
                }
            )
        bc = soup.find(attrs={"aria-label": "breadcrumb"}) or soup.find(class_="breadcrumb")
        if bc:
            items = [a.get_text(strip=True) for a in bc.find_all("a", limit=10)]
            nav_elements.append(
                {
                    "nav_type": "breadcrumb",
                    "items": items,
                    "selector": _build_selector(bc),
                }
            )
        return nav_elements

    def _extract_headings(self, soup: BeautifulSoup) -> list[str]:
        headings: list[str] = []
        for h in soup.find_all(["h1", "h2", "h3", "h4"], limit=20):
            text = h.get_text(strip=True)
            if text:
                headings.append(text[:100])
        return headings

    @staticmethod
    def _extract_headings_from_text(text: str) -> list[str]:
        """Extract likely headings from plain text when HTML is unavailable.

        Heuristic: short lines (4-80 chars) that are followed by longer content lines,
        excluding lines that look like sentences (ending in period/comma).
        """
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        headings: list[str] = []
        for i, line in enumerate(lines):
            if len(line) < 4 or len(line) > 80:
                continue
            # Skip lines that look like sentences
            if line[-1] in ".,:;!?" and len(line) > 40:
                continue
            # Skip lines starting with common non-heading patterns
            if line.startswith(("http", "©", "Privacy", "Terms", "Cookie")):
                continue
            # A heading is typically followed by longer content
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if len(next_line) > len(line):
                    headings.append(line)
            elif i == 0:
                headings.append(line)
        return headings[:20]

    @staticmethod
    def _extract_sections_from_text(text: str, headings: list[str]) -> list[dict]:
        """Extract content sections from plain text using detected headings."""
        if not headings:
            return []
        sections: list[dict] = []
        section_keywords = {
            "features": ["feature", "capability", "what we offer", "everything you need"],
            "pricing": ["pricing", "plans", "cost", "free trial"],
            "testimonials": ["testimonial", "trusted by", "what our", "reviews"],
            "cta": ["get started", "sign up", "request demo", "contact"],
            "hero": ["welcome", "unify", "transform", "powerful"],
        }
        for heading in headings[:10]:
            heading_lower = heading.lower()
            section_type = "content"
            for st, keywords in section_keywords.items():
                if any(kw in heading_lower for kw in keywords):
                    section_type = st
                    break
            sections.append(
                {
                    "heading": heading,
                    "section_type": section_type,
                    "content_preview": "",
                }
            )
        return sections

    @staticmethod
    def _logo_candidates_from_links(soup: BeautifulSoup, rel_substring: str) -> list[str]:
        """Extract href values from <link rel> tags whose rel contains rel_substring."""
        out: list[str] = []
        for link in soup.find_all("link", rel=lambda r: r and rel_substring in " ".join(r).lower()):
            href = link.get("href")
            if href:
                out.append(str(href))
        return out

    @staticmethod
    def _logo_candidates_from_imgs(soup: BeautifulSoup) -> list[str]:
        """Extract src values from <img> tags whose attrs hint at a logo."""
        out: list[str] = []
        for img in soup.find_all("img", limit=50):
            src = str(img.get("src") or "")
            alt = str(img.get("alt") or "").lower()
            raw_classes = img.get("class") or []
            classes = " ".join(raw_classes).lower() if isinstance(raw_classes, list) else ""
            haystack = src.lower() + " " + alt + " " + classes
            if src and any(kw in haystack for kw in ("logo", "brand")):
                out.append(src)
        return out

    @staticmethod
    def _extract_logo_candidates(soup: BeautifulSoup) -> list[str]:
        """Extract logo candidate URLs from HTML."""
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(url: str) -> None:
            url = url.strip()
            if url and url not in seen:
                seen.add(url)
                candidates.append(url)

        for url in PageAnalyzer._logo_candidates_from_links(soup, "icon"):
            _add(url)
        for url in PageAnalyzer._logo_candidates_from_links(soup, "apple-touch-icon"):
            _add(url)

        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            _add(str(og_img["content"]))

        for url in PageAnalyzer._logo_candidates_from_imgs(soup):
            _add(url)

        return candidates
