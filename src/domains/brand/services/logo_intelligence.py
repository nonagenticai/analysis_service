"""Enhanced logo analysis and intelligence extraction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag


@dataclass
class LogoVariant:
    """A single logo variant found on the page."""

    url: str = ""
    source: str = ""  # schema_org, img_tag, inline_svg, favicon, og_image, manifest
    format: str = ""  # svg, png, jpg, ico, webp
    width: int | None = None
    height: int | None = None
    is_svg: bool = False
    in_header: bool = False

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "source": self.source,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "is_svg": self.is_svg,
            "in_header": self.in_header,
        }


@dataclass
class LogoIntelligence:
    """Comprehensive logo analysis result."""

    primary_logo_url: str = ""
    variants: list[LogoVariant] = field(default_factory=list)
    position: str = ""  # header-left, header-center, header-right, unknown
    has_svg: bool = False
    has_retina: bool = False  # any variant > 100px width
    inline_svg_count: int = 0
    logo_alt_text: str = ""

    def to_dict(self) -> dict:
        return {
            "primary_logo_url": self.primary_logo_url,
            "variants": [v.to_dict() for v in self.variants],
            "position": self.position,
            "has_svg": self.has_svg,
            "has_retina": self.has_retina,
            "inline_svg_count": self.inline_svg_count,
            "logo_alt_text": self.logo_alt_text,
        }


class LogoIntelligenceExtractor:
    """Enhanced logo extraction with variant detection and positioning."""

    def extract(
        self, soup: BeautifulSoup, base_url: str, schema_org: dict | None = None
    ) -> LogoIntelligence:
        """Extract comprehensive logo intelligence from HTML.

        Scans schema.org, img tags, inline SVGs, favicons, and og:image
        to build a complete picture of logo variants and positioning.

        Args:
            soup: Parsed HTML document.
            base_url: Base URL for resolving relative logo URLs.
            schema_org: Optional schema.org structured data dict.

        Returns:
            LogoIntelligence with primary logo, variants, and positioning.
        """
        variants: list[LogoVariant] = []

        # Collect from all sources
        variants.extend(self._collect_schema_org_logos(schema_org, base_url))
        variants.extend(self._collect_img_logos(soup, base_url))
        variants.extend(self._collect_inline_svg_logos(soup))
        variants.extend(self._collect_favicon_logos(soup, base_url))
        variants.extend(self._collect_og_logos(soup))

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_variants: list[LogoVariant] = []
        for v in variants:
            if v.url and v.url not in seen_urls:
                seen_urls.add(v.url)
                unique_variants.append(v)
            elif not v.url and v.source == "inline_svg":
                # Inline SVGs have no URL, keep them all
                unique_variants.append(v)

        # Detect position
        position = self._detect_position(soup)

        # Select primary logo
        primary_url = self._select_primary_logo(unique_variants, base_url)

        # Find alt text
        alt_text = self._find_logo_alt_text(soup, base_url)

        # Stats
        has_svg = any(v.is_svg for v in unique_variants)
        has_retina = any((v.width is not None and v.width > 100) for v in unique_variants)
        inline_svg_count = sum(1 for v in unique_variants if v.source == "inline_svg")

        return LogoIntelligence(
            primary_logo_url=primary_url,
            variants=unique_variants,
            position=position,
            has_svg=has_svg,
            has_retina=has_retina,
            inline_svg_count=inline_svg_count,
            logo_alt_text=alt_text,
        )

    def _collect_schema_org_logos(
        self, schema_org: dict | None, base_url: str
    ) -> list[LogoVariant]:
        """Collect logos from schema.org data."""
        variants = []
        if not schema_org:
            return variants

        logo = schema_org.get("logo", "")
        if isinstance(logo, dict):
            logo = logo.get("url", "") or logo.get("@id", "")
        if logo and isinstance(logo, str):
            url = self._resolve_url(logo, base_url)
            variants.append(
                LogoVariant(
                    url=url,
                    source="schema_org",
                    format=self._detect_format(url),
                    is_svg=url.lower().endswith(".svg"),
                    in_header=False,
                )
            )
        return variants

    def _collect_img_logos(self, soup: BeautifulSoup, base_url: str) -> list[LogoVariant]:
        """Collect logos from img tags matching logo patterns."""
        variants = []
        for img in soup.find_all("img"):
            if not isinstance(img, Tag):
                continue

            # Check if this img is a logo
            if not self._is_logo_img(img):
                continue

            src = img.get("src", "")
            if not src:
                continue
            src_str = str(src) if isinstance(src, str) else str(src[0])
            url = self._resolve_url(src_str, base_url)

            # Determine dimensions
            width = self._parse_int(img.get("width"))
            height = self._parse_int(img.get("height"))

            # Check if in header
            in_header = self._is_in_header(img)

            variants.append(
                LogoVariant(
                    url=url,
                    source="img_tag",
                    format=self._detect_format(url),
                    width=width,
                    height=height,
                    is_svg=url.lower().endswith(".svg"),
                    in_header=in_header,
                )
            )
        return variants

    def _collect_inline_svg_logos(self, soup: BeautifulSoup) -> list[LogoVariant]:
        """Collect inline SVG elements that are logos."""
        variants = []
        for svg in soup.find_all("svg"):
            if not isinstance(svg, Tag):
                continue

            # Check if this SVG is a logo
            cls_attr = svg.get("class") or []
            svg_classes = " ".join(cls_attr if isinstance(cls_attr, list) else [str(cls_attr)])
            svg_id = str(svg.get("id", ""))

            # Check SVG attributes
            is_logo = "logo" in svg_classes.lower() or "logo" in svg_id.lower()

            # Check parent element
            if not is_logo:
                parent = svg.parent
                if parent and isinstance(parent, Tag):
                    cls_attr = parent.get("class") or []
                    parent_classes = " ".join(
                        cls_attr if isinstance(cls_attr, list) else [str(cls_attr)]
                    )
                    parent_id = str(parent.get("id", ""))
                    is_logo = "logo" in parent_classes.lower() or "logo" in parent_id.lower()

            if is_logo:
                in_header = self._is_in_header(svg)
                variants.append(
                    LogoVariant(
                        url="",  # inline SVG has no URL
                        source="inline_svg",
                        format="svg",
                        is_svg=True,
                        in_header=in_header,
                    )
                )
        return variants

    def _collect_favicon_logos(self, soup: BeautifulSoup, base_url: str) -> list[LogoVariant]:
        """Collect favicon/icon links."""
        variants = []
        for link in soup.find_all("link", rel=re.compile(r"icon", re.I)):
            if not isinstance(link, Tag):
                continue
            href = link.get("href", "")
            if not href:
                continue
            href_str = str(href) if isinstance(href, str) else str(href[0])
            url = self._resolve_url(href_str, base_url)

            # Parse sizes
            sizes = str(link.get("sizes", ""))
            width, height = None, None
            if sizes and "x" in sizes.lower():
                parts = sizes.lower().split("x")
                try:
                    width = int(parts[0])
                    height = int(parts[1])
                except ValueError, IndexError:
                    pass

            variants.append(
                LogoVariant(
                    url=url,
                    source="favicon",
                    format=self._detect_format(url),
                    width=width,
                    height=height,
                    is_svg=url.lower().endswith(".svg"),
                    in_header=False,
                )
            )
        return variants

    def _collect_og_logos(self, soup: BeautifulSoup) -> list[LogoVariant]:
        """Collect og:image as a logo variant."""
        variants = []
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and isinstance(og, Tag):
            content = og.get("content", "")
            url = str(content) if isinstance(content, str) else str(content[0]) if content else ""
            if url:
                variants.append(
                    LogoVariant(
                        url=url,
                        source="og_image",
                        format=self._detect_format(url),
                        is_svg=url.lower().endswith(".svg"),
                        in_header=False,
                    )
                )
        return variants

    @staticmethod
    def _classes_lower(el: Tag) -> str:
        cls_attr = el.get("class") or []
        return " ".join(cls_attr if isinstance(cls_attr, list) else [str(cls_attr)]).lower()

    def _find_logo_in_header(self, header: Tag) -> Tag | None:
        """Locate a logo <img> or <svg class*=logo> element inside ``header``."""
        for img in header.find_all("img"):
            if isinstance(img, Tag) and self._is_logo_img(img):
                return img
        for svg in header.find_all("svg"):
            if isinstance(svg, Tag) and "logo" in self._classes_lower(svg):
                return svg
        return None

    def _position_from_ancestors(self, logo_el: Tag, header: Tag) -> str | None:
        """Walk ancestors of ``logo_el`` to find a position-hint class."""
        parent = logo_el.parent
        while parent and isinstance(parent, Tag) and parent != header:
            classes_lower = self._classes_lower(parent)
            if "center" in classes_lower:
                return "header-center"
            if "right" in classes_lower:
                return "header-right"
            parent = parent.parent
        return None

    def _detect_position(self, soup: BeautifulSoup) -> str:
        """Detect logo position within the header."""
        header = soup.find("header")
        if not header or not isinstance(header, Tag):
            return "unknown"

        logo_el = self._find_logo_in_header(header)
        if not logo_el:
            return "unknown"

        return self._position_from_ancestors(logo_el, header) or "header-left"

    def _is_same_domain(self, url: str, site_domain: str) -> bool:
        """Check if a URL belongs to (or is a CDN for) the given site domain.

        Returns True if the URL's domain shares a base with the site domain,
        or if the source is a favicon/schema_org (always trusted).
        """
        if not site_domain or not url:
            return False
        url_domain = urlparse(url).netloc.lower()
        if not url_domain:
            return False
        # Extract base domain (last two parts) for comparison
        site_base = ".".join(site_domain.rsplit(".", 2)[-2:]).lstrip(".")
        url_base = ".".join(url_domain.rsplit(".", 2)[-2:]).lstrip(".")
        # Direct match or subdomain relationship
        if site_base == url_base:
            return True
        # Check if the URL domain contains the site's name (CDN patterns like
        # "stripeassets.com" for stripe.com, "githubassets.com" for github.com)
        site_name = site_base.split(".")[0]
        return bool(site_name and len(site_name) > 2 and site_name in url_domain)

    @staticmethod
    def _largest_by_dims(candidates: list[LogoVariant]) -> str:
        """Return URL of largest variant (by w*h) or the first if none have dims."""
        with_dims = [v for v in candidates if v.width and v.height]
        if with_dims:
            best = max(with_dims, key=lambda v: (v.width or 0) * (v.height or 0))
            return best.url
        return candidates[0].url

    def _pick_header_svg(self, variants: list[LogoVariant], site_domain: str) -> str:
        for v in variants:
            if v.is_svg and v.in_header and v.url and self._is_same_domain(v.url, site_domain):
                return v.url
        for v in variants:
            if v.is_svg and v.in_header and v.url:
                return v.url
        return ""

    def _pick_header_image(self, variants: list[LogoVariant], site_domain: str) -> str:
        header_imgs = [v for v in variants if v.in_header and not v.is_svg and v.url]
        if not header_imgs:
            return ""
        same_domain_imgs = [v for v in header_imgs if self._is_same_domain(v.url, site_domain)]
        return self._largest_by_dims(same_domain_imgs or header_imgs)

    @staticmethod
    def _pick_favicon_svg(variants: list[LogoVariant]) -> str:
        for v in variants:
            if v.source == "favicon" and v.is_svg and v.url:
                return v.url
        return ""

    @staticmethod
    def _pick_schema_org(variants: list[LogoVariant]) -> str:
        for v in variants:
            if v.source == "schema_org" and v.url:
                return v.url
        return ""

    def _pick_same_domain_svg(self, variants: list[LogoVariant], site_domain: str) -> str:
        if not site_domain:
            return ""
        for v in variants:
            if v.is_svg and v.url and self._is_same_domain(v.url, site_domain):
                return v.url
        return ""

    def _pick_largest_favicon(self, variants: list[LogoVariant]) -> str:
        favicons = [v for v in variants if v.source == "favicon" and v.url]
        return self._largest_by_dims(favicons) if favicons else ""

    def _pick_og_image(self, variants: list[LogoVariant], site_domain: str) -> str:
        for v in variants:
            if v.source != "og_image" or not v.url:
                continue
            if not site_domain or self._is_same_domain(v.url, site_domain):
                return v.url
        return ""

    def _pick_same_domain_img(self, variants: list[LogoVariant], site_domain: str) -> str:
        if not site_domain:
            return ""
        for v in variants:
            if v.source == "img_tag" and v.url and self._is_same_domain(v.url, site_domain):
                return v.url
        return ""

    @staticmethod
    def _pick_fallback(variants: list[LogoVariant]) -> str:
        for v in variants:
            if v.is_svg and v.url:
                return v.url
        for v in variants:
            if v.source == "img_tag" and v.url:
                return v.url
        return ""

    def _select_primary_logo(self, variants: list[LogoVariant], base_url: str = "") -> str:
        """Select the best primary logo from variants.

        Priority:
        1. SVG in header (same domain preferred)
        2. Largest PNG/JPG in header (same domain preferred)
        3. Favicon SVG (always same-domain)
        4. schema.org logo
        5. SVG anywhere -- same domain only
        6. Largest favicon
        7. og:image (only if URL domain matches site domain)
        8. Any img_tag logo -- same domain only
        Fallback: first SVG anywhere, then first img_tag
        """
        site_domain = urlparse(base_url).netloc.lower() if base_url else ""
        for picker in (
            lambda: self._pick_header_svg(variants, site_domain),
            lambda: self._pick_header_image(variants, site_domain),
            lambda: self._pick_favicon_svg(variants),
            lambda: self._pick_schema_org(variants),
            lambda: self._pick_same_domain_svg(variants, site_domain),
            lambda: self._pick_largest_favicon(variants),
            lambda: self._pick_og_image(variants, site_domain),
            lambda: self._pick_same_domain_img(variants, site_domain),
            lambda: self._pick_fallback(variants),
        ):
            url = picker()
            if url:
                return url
        return ""

    def _is_logo_img(self, img: Tag) -> bool:
        """Check if an img tag is a logo based on class/id/alt/src."""
        attrs_to_check = []

        # Class
        classes = img.get("class") or []
        if isinstance(classes, list):
            attrs_to_check.append(" ".join(classes))
        else:
            attrs_to_check.append(str(classes))

        # ID
        attrs_to_check.append(str(img.get("id", "")))

        # Alt
        attrs_to_check.append(str(img.get("alt", "")))

        # Src filename
        src = str(img.get("src", ""))
        if "/" in src:
            attrs_to_check.append(src.split("/")[-1])
        else:
            attrs_to_check.append(src)

        combined = " ".join(attrs_to_check).lower()
        return "logo" in combined

    def _is_in_header(self, element: Tag) -> bool:
        """Check if element is inside a <header> tag."""
        parent = element.parent
        depth = 0
        while parent and depth < 10:
            if parent.name == "header":
                return True
            parent = parent.parent
            depth += 1
        return False

    def _find_logo_alt_text(self, soup: BeautifulSoup, base_url: str = "") -> str:
        """Find alt text from logo images, preferring same-domain logos.

        When the site domain is known, only returns alt text from same-domain
        logo images to avoid picking up customer/third-party logo alt text.
        """
        site_domain = urlparse(base_url).netloc.lower() if base_url else ""
        fallback_alt = ""

        for img in soup.find_all("img"):
            if isinstance(img, Tag) and self._is_logo_img(img):
                alt = img.get("alt", "")
                if alt:
                    alt_str = str(alt) if isinstance(alt, str) else str(alt[0])
                    src = str(img.get("src", ""))
                    resolved_src = self._resolve_url(src, base_url) if src else ""
                    if site_domain and resolved_src:
                        if self._is_same_domain(resolved_src, site_domain):
                            return alt_str
                        # Skip third-party logo alt text when domain is known
                        continue
                    if not fallback_alt:
                        fallback_alt = alt_str

        return fallback_alt

    def _resolve_url(self, url: str, base_url: str) -> str:
        """Resolve relative URLs to absolute."""
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        if url.startswith("//"):
            return f"https:{url}"
        return urljoin(base_url, url)

    def _detect_format(self, url: str) -> str:
        """Detect image format from URL."""
        lower = url.lower()
        for fmt in ("svg", "png", "jpg", "jpeg", "ico", "webp", "gif"):
            if f".{fmt}" in lower:
                return "jpg" if fmt == "jpeg" else fmt
        return ""

    @staticmethod
    def _parse_int(val: object) -> int | None:
        """Safely parse an integer from an attribute value."""
        if val is None:
            return None
        try:
            return int(str(val).split()[0]) if val else None
        except ValueError, IndexError:
            return None
