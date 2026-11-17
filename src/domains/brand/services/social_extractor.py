"""Social media profile extraction from HTML.

Extracts social media profile URLs from HTML documents by scanning
footer elements, social-link containers, and schema.org structured data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag


@dataclass
class SocialProfiles:
    """Social media profile URLs organized by platform."""

    facebook: str = ""
    twitter: str = ""
    linkedin: str = ""
    instagram: str = ""
    youtube: str = ""
    tiktok: str = ""
    github: str = ""
    pinterest: str = ""
    threads: str = ""
    other: list[str] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        """Check if any social profiles were found."""
        return bool(
            self.facebook
            or self.twitter
            or self.linkedin
            or self.instagram
            or self.youtube
            or self.tiktok
            or self.github
            or self.pinterest
            or self.threads
            or self.other
        )

    @property
    def count(self) -> int:
        """Count of populated platform fields."""
        c = 0
        for f in (
            self.facebook,
            self.twitter,
            self.linkedin,
            self.instagram,
            self.youtube,
            self.tiktok,
            self.github,
            self.pinterest,
            self.threads,
        ):
            if f:
                c += 1
        return c + len(self.other)

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "facebook": self.facebook,
            "twitter": self.twitter,
            "linkedin": self.linkedin,
            "instagram": self.instagram,
            "youtube": self.youtube,
            "tiktok": self.tiktok,
            "github": self.github,
            "pinterest": self.pinterest,
            "threads": self.threads,
            "other": self.other,
        }


# Domain -> platform field name
_PLATFORM_DOMAINS: dict[str, str] = {
    "facebook.com": "facebook",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "linkedin.com": "linkedin",
    "instagram.com": "instagram",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "github.com": "github",
    "pinterest.com": "pinterest",
    "threads.net": "threads",
}

# URL path prefixes that indicate share/intent links (not profile links)
_SHARE_PATTERNS: list[str] = [
    "twitter.com/intent",
    "x.com/intent",
    "facebook.com/sharer",
    "facebook.com/share",
    "linkedin.com/sharearticle",
    "pinterest.com/pin/create",
]


class SocialExtractor:
    """Extracts social media profile URLs from HTML documents."""

    def _consume_schema_same_as(
        self, schema_org: dict | None, profiles: SocialProfiles, seen: set[str]
    ) -> None:
        if not (schema_org and "same_as" in schema_org):
            return
        same_as = schema_org["same_as"]
        if not isinstance(same_as, list):
            return
        for url in same_as:
            if not isinstance(url, str):
                continue
            platform, clean_url = self._classify_url(url)
            if platform and platform not in seen:
                self._set_platform(profiles, platform, clean_url)
                seen.add(platform)
            elif not platform and clean_url and clean_url not in profiles.other:
                profiles.other.append(clean_url)

    def extract_social_profiles(
        self,
        soup: BeautifulSoup,
        schema_org: dict | None = None,
    ) -> SocialProfiles:
        """Extract social media profile URLs from HTML.

        Scans footer elements first, then divs/sections/navs with social-related
        class names, then schema.org sameAs data.

        Args:
            soup: Parsed HTML document.
            schema_org: Optional schema.org data dict with a "same_as" key
                        containing a list of URLs.

        Returns:
            SocialProfiles with discovered profile URLs.
        """
        profiles = SocialProfiles()
        seen_platforms: set[str] = set()

        for footer in soup.find_all("footer"):
            self._extract_from_container(footer, profiles, seen_platforms)
        for tag_name in ("div", "section", "nav"):
            for element in soup.find_all(tag_name, class_=re.compile(r"social", re.IGNORECASE)):
                self._extract_from_container(element, profiles, seen_platforms)

        self._consume_schema_same_as(schema_org, profiles, seen_platforms)
        return profiles

    def _extract_from_container(
        self,
        container: Tag,
        profiles: SocialProfiles,
        seen_platforms: set[str],
    ) -> None:
        """Extract social links from a container element."""
        for anchor in container.find_all("a", href=True):
            href = anchor.get("href", "")
            if not href or not isinstance(href, str):
                continue

            # Skip share/intent links
            if self._is_share_link(href):
                continue

            platform, clean_url = self._classify_url(href)
            if platform:
                if platform not in seen_platforms:
                    self._set_platform(profiles, platform, clean_url)
                    seen_platforms.add(platform)
            elif clean_url:
                # It parsed as a URL but didn't match a known platform
                # Only add if it looks like a social-type link (has a known social-ish domain)
                pass

    def _classify_url(self, url: str) -> tuple[str, str]:
        """Classify a URL by social media platform.

        Args:
            url: The URL to classify.

        Returns:
            Tuple of (platform_name, clean_url). Returns ("", "") if the URL
            does not match any known social media platform.
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return ("", "")

        hostname = parsed.hostname
        if not hostname:
            return ("", "")

        # Strip www. prefix for matching
        hostname_clean = hostname.lower()
        if hostname_clean.startswith("www."):
            hostname_clean = hostname_clean[4:]

        for domain, platform in _PLATFORM_DOMAINS.items():
            if hostname_clean == domain or hostname_clean.endswith("." + domain):
                return (platform, url)

        return ("", "")

    def _is_share_link(self, url: str) -> bool:
        """Check if a URL is a share/intent link rather than a profile link.

        Args:
            url: The URL to check.

        Returns:
            True if the URL is a share/intent link.
        """
        url_lower = url.lower()
        return any(pattern in url_lower for pattern in _SHARE_PATTERNS)

    def _parse_handle(self, url: str, platform: str) -> str:
        """Extract a social media handle from a profile URL.

        Args:
            url: The profile URL.
            platform: The platform name (e.g. "twitter", "instagram").

        Returns:
            The handle string (e.g. "@acme"), or empty string if not extractable.
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return ""

        path = parsed.path.strip("/")
        if not path:
            return ""

        # Remove common path prefixes
        path_parts = path.split("/")
        if not path_parts:
            return ""

        # For platforms like linkedin, the first segment might be "company" or "in"
        if platform == "linkedin":
            # linkedin.com/company/acme or linkedin.com/in/person
            if len(path_parts) >= 2 and path_parts[0] in ("company", "in"):
                return path_parts[1]
            return path_parts[0]

        # For youtube, handle /c/, /channel/, /user/ prefixes
        if platform == "youtube":
            if len(path_parts) >= 2 and path_parts[0] in ("c", "channel", "user"):
                return path_parts[1]
            return path_parts[0]

        # For most platforms (twitter, instagram, tiktok, facebook, github, pinterest, threads)
        handle = path_parts[0]

        # Strip leading @ if present (tiktok, threads use @ in URLs)
        handle = handle.lstrip("@")

        if handle:
            return f"@{handle}"

        return ""

    def _set_platform(self, profiles: SocialProfiles, platform: str, url: str) -> None:
        """Set a platform field on the SocialProfiles dataclass."""
        setattr(profiles, platform, url)
