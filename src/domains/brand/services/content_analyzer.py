"""Brand content analysis - tagline, voice, messages, audience."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag


@dataclass
class ContentAnalysis:
    """Result of analyzing brand content from a webpage."""

    brand_voice: str = ""  # professional, casual, friendly, technical, corporate
    brand_voice_confidence: float = 0.0
    tagline: str = ""
    tagline_source: str = ""  # hero_h1, meta_description, og_description
    key_messages: list[str] = field(default_factory=list)
    target_audience_hints: list[str] = field(default_factory=list)
    word_count: int = 0
    reading_level: str = ""  # simple, moderate, complex

    def to_dict(self) -> dict:
        return {
            "brand_voice": self.brand_voice,
            "brand_voice_confidence": self.brand_voice_confidence,
            "tagline": self.tagline,
            "tagline_source": self.tagline_source,
            "key_messages": self.key_messages,
            "target_audience_hints": self.target_audience_hints,
            "word_count": self.word_count,
            "reading_level": self.reading_level,
        }


# Voice category keyword lists
_VOICE_KEYWORDS: dict[str, list[str]] = {
    "professional": [
        "solution",
        "enterprise",
        "industry",
        "leading",
        "excellence",
        "trusted",
        "optimize",
        "leverage",
        "strategic",
        "comprehensive",
    ],
    "casual": [
        "awesome",
        "cool",
        "hey",
        "love",
        "amazing",
        "wow",
        "easy",
        "fun",
        "simple",
        "free",
    ],
    "friendly": [
        "welcome",
        "together",
        "community",
        "share",
        "help",
        "support",
        "join",
        "connect",
        "discover",
        "enjoy",
    ],
    "technical": [
        "api",
        "sdk",
        "framework",
        "documentation",
        "integration",
        "deploy",
        "infrastructure",
        "platform",
        "developer",
        "code",
    ],
    "corporate": [
        "stakeholder",
        "synergy",
        "quarterly",
        "revenue",
        "compliance",
        "governance",
        "portfolio",
        "board",
        "shareholder",
        "fiscal",
    ],
}

# Audience keyword indicators: (patterns, audience_label)
_AUDIENCE_INDICATORS: list[tuple[list[str], str]] = [
    (["developers", "developer", "engineering", "api", "sdk", "open source"], "developers"),
    (["enterprise", "business", "organization", "company", "corporate"], "enterprise"),
    (["shop", "buy", "price", "cart", "deal", "shipping"], "consumers"),
    (["create", "creator", "design", "content", "publish", "portfolio"], "creators"),
    (["learn", "student", "course", "education", "tutorial"], "students"),
]

# Tags to exclude from key messages (parent chain check)
_EXCLUDED_PARENTS = {"nav", "footer", "aside"}


class ContentAnalyzer:
    """Analyzes brand content from parsed HTML documents."""

    def extract_content_analysis(self, soup: BeautifulSoup) -> ContentAnalysis:
        """Run all content analysis sub-methods and return a populated ContentAnalysis.

        Args:
            soup: Parsed HTML document.

        Returns:
            ContentAnalysis with all fields populated.
        """
        analysis = ContentAnalysis()

        # Extract tagline
        analysis.tagline, analysis.tagline_source = self._extract_tagline(soup)

        # Get visible text for voice and audience analysis
        visible_texts = self._get_visible_text(soup)

        # Analyze brand voice
        analysis.brand_voice, analysis.brand_voice_confidence = self._analyze_brand_voice(
            visible_texts
        )

        # Extract key messages
        analysis.key_messages = self._extract_key_messages(soup)

        # Infer target audience
        analysis.target_audience_hints = self._infer_target_audience(visible_texts, soup)

        # Word count from visible text
        combined_text = " ".join(visible_texts)
        words = combined_text.split()
        analysis.word_count = len(words)

        # Reading level
        analysis.reading_level = self._estimate_reading_level(combined_text)

        return analysis

    @staticmethod
    def _meta_content(soup: BeautifulSoup, attrs: dict, max_length: int) -> str:
        """Return the trimmed content="" of a <meta> matching ``attrs`` (<= max_length)."""
        meta = soup.find("meta", attrs=attrs)
        if not (meta and isinstance(meta, Tag)):
            return ""
        content = meta.get("content", "")
        if isinstance(content, list):
            content = content[0] if content else ""
        content = str(content).strip()
        return content if content and len(content) <= max_length else ""

    @staticmethod
    def _hero_h1(soup: BeautifulSoup, max_length: int) -> str:
        """Return the first <h1> nested inside a hero-tagged container (<= max_length)."""
        hero_containers = soup.find_all(
            ["section", "div", "header"], class_=re.compile(r"hero", re.IGNORECASE)
        )
        if not hero_containers:
            hero_containers = soup.find_all(
                ["section", "div", "header"], id=re.compile(r"hero", re.IGNORECASE)
            )
        for container in hero_containers:
            h1 = container.find("h1")
            if h1 and isinstance(h1, Tag):
                text = h1.get_text(separator=" ", strip=True)
                if text and len(text) <= max_length:
                    return text
        return ""

    def _extract_tagline(self, soup: BeautifulSoup) -> tuple[str, str]:
        """Extract a tagline from the page using priority-based fallback.

        Priority 1: First <h1> inside a <section>, <div>, or <header> with "hero" in class/id
        Priority 2: First <h1> on the page (if <= 120 chars)
        Priority 3: meta description (if <= 120 chars)
        Priority 4: og:description (if <= 120 chars)

        Returns:
            Tuple of (tagline_text, source_label). Returns ("", "") if nothing found.
        """
        max_length = 120

        hero = self._hero_h1(soup, max_length)
        if hero:
            return (hero, "hero_h1")

        first_h1 = soup.find("h1")
        if first_h1 and isinstance(first_h1, Tag):
            text = first_h1.get_text(separator=" ", strip=True)
            if text and len(text) <= max_length:
                return (text, "hero_h1")

        meta_desc = self._meta_content(soup, {"name": "description"}, max_length)
        if meta_desc:
            return (meta_desc, "meta_description")

        og_desc = self._meta_content(soup, {"property": "og:description"}, max_length)
        if og_desc:
            return (og_desc, "og_description")

        return ("", "")

    def _analyze_brand_voice(self, texts: list[str]) -> tuple[str, float]:
        """Analyze brand voice based on keyword frequency heuristic.

        Args:
            texts: List of visible text segments from the page.

        Returns:
            Tuple of (voice_category, confidence). Defaults to ("professional", 0.0)
            if no keywords are found.
        """
        combined = " ".join(texts).lower()

        category_counts: dict[str, int] = {}
        for category, keywords in _VOICE_KEYWORDS.items():
            count = 0
            for keyword in keywords:
                pattern = r"\b" + re.escape(keyword) + r"\b"
                matches = re.findall(pattern, combined, re.IGNORECASE)
                count += len(matches)
            category_counts[category] = count

        total = sum(category_counts.values())
        if total == 0:
            return ("professional", 0.0)

        # Sort by count descending
        sorted_categories = sorted(category_counts.items(), key=lambda x: x[1], reverse=True)
        winner_name, winner_count = sorted_categories[0]
        second_count = sorted_categories[1][1] if len(sorted_categories) > 1 else 0

        denominator = winner_count + second_count
        confidence = winner_count / denominator if denominator > 0 else 0.0

        return (winner_name, round(confidence, 2))

    def _extract_key_messages(self, soup: BeautifulSoup) -> list[str]:
        """Extract key messages from heading tags, filtering out nav/footer/aside headings.

        Args:
            soup: Parsed HTML document.

        Returns:
            List of heading text strings, up to 10.
        """
        messages: list[str] = []

        for heading in soup.find_all(["h1", "h2", "h3"]):
            if not isinstance(heading, Tag):
                continue

            # Check parent chain up to 3 levels for excluded containers
            if self._has_excluded_parent(heading, max_depth=3):
                continue

            text = heading.get_text(strip=True)
            if text:
                messages.append(text)

            if len(messages) >= 10:
                break

        return messages

    def _has_excluded_parent(self, tag: Tag, max_depth: int = 3) -> bool:
        """Check if a tag has an excluded parent (nav, footer, aside) within max_depth levels.

        Args:
            tag: The HTML tag to check.
            max_depth: Maximum number of parent levels to check.

        Returns:
            True if an excluded parent is found.
        """
        current = tag.parent
        depth = 0
        while current and depth < max_depth:
            if isinstance(current, Tag) and current.name in _EXCLUDED_PARENTS:
                return True
            current = current.parent
            depth += 1
        return False

    def _infer_target_audience(self, texts: list[str], soup: BeautifulSoup) -> list[str]:
        """Infer target audience from keyword indicators in page text.

        Args:
            texts: List of visible text segments.
            soup: Parsed HTML document (reserved for future use).

        Returns:
            List of unique audience labels.
        """
        combined = " ".join(texts).lower()
        audiences: list[str] = []

        for keywords, audience_label in _AUDIENCE_INDICATORS:
            for keyword in keywords:
                pattern = r"\b" + re.escape(keyword) + r"\b"
                if re.search(pattern, combined, re.IGNORECASE):
                    if audience_label not in audiences:
                        audiences.append(audience_label)
                    break

        return audiences

    def _estimate_reading_level(self, text: str) -> str:
        """Estimate reading level based on sentence and word length.

        Args:
            text: The full text content to analyze.

        Returns:
            One of "simple", "moderate", or "complex".
        """
        if not text.strip():
            return "simple"

        # Split into sentences by .!?
        sentences = re.split(r"[.!?]+", text)
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            return "simple"

        # Calculate average words per sentence
        total_words = 0
        total_word_length = 0
        word_count = 0
        for sentence in sentences:
            words = sentence.split()
            total_words += len(words)
            for word in words:
                # Strip punctuation for word length calculation
                clean_word = re.sub(r"[^\w]", "", word)
                if clean_word:
                    total_word_length += len(clean_word)
                    word_count += 1

        if word_count == 0:
            return "simple"

        avg_words_per_sentence = total_words / len(sentences)
        avg_word_length = total_word_length / word_count

        if avg_words_per_sentence > 20 and avg_word_length > 6:
            return "complex"
        if avg_words_per_sentence > 12 or avg_word_length > 5:
            return "moderate"
        return "simple"

    def _get_visible_text(self, soup: BeautifulSoup) -> list[str]:
        """Extract visible text from the page body, excluding script/style/nav tags.

        Args:
            soup: Parsed HTML document.

        Returns:
            List of non-empty text segments.
        """
        body = soup.find("body")
        if not body or not isinstance(body, Tag):
            return []

        # Remove script, style, and nav elements from a copy
        # Work on a copy to avoid modifying the original soup
        from copy import copy

        body_copy = copy(body)

        for tag_name in ("script", "style", "nav"):
            for element in body_copy.find_all(tag_name):
                element.decompose()

        texts: list[str] = []
        for text in body_copy.stripped_strings:
            cleaned = text.strip()
            if cleaned:
                texts.append(cleaned)

        return texts
