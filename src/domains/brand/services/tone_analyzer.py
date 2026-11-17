"""Brand tone and voice analysis from website content."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

from bs4 import BeautifulSoup


@dataclass
class ToneResult:
    """Brand tone analysis result."""

    tone: str = ""  # professional, casual, friendly, technical, playful, luxury, authoritative
    formality_score: float = 0.5  # 0 (casual) to 1 (formal)
    vocabulary_level: str = ""  # simple, moderate, sophisticated
    confidence: float = 0.0
    markers: list[str] = field(default_factory=list)  # detected tone indicators

    def to_dict(self) -> dict:
        return {
            "tone": self.tone,
            "formality_score": round(self.formality_score, 4),
            "vocabulary_level": self.vocabulary_level,
            "confidence": round(self.confidence, 4),
            "markers": self.markers,
        }


class BrandToneAnalyzer:
    """Analyze brand tone from website content using heuristics."""

    # Contraction patterns (indicate informality)
    _CONTRACTIONS = re.compile(
        r"\b(can't|won't|don't|doesn't|isn't|aren't|wasn't|weren't|"
        r"couldn't|wouldn't|shouldn't|hasn't|haven't|hadn't|"
        r"it's|that's|there's|here's|what's|who's|"
        r"we're|they're|you're|i'm|he's|she's|"
        r"we've|they've|you've|i've|"
        r"we'll|they'll|you'll|i'll|he'll|she'll|"
        r"let's|ain't)\b",
        re.IGNORECASE,
    )

    # Formal vocabulary indicators
    _FORMAL_WORDS: ClassVar[set[str]] = {
        "furthermore",
        "moreover",
        "consequently",
        "nevertheless",
        "henceforth",
        "pursuant",
        "whereas",
        "hereby",
        "therein",
        "thereof",
        "comprehensive",
        "facilitate",
        "implement",
        "optimize",
        "utilize",
        "leverage",
        "synergy",
        "stakeholder",
        "governance",
        "compliance",
        "infrastructure",
        "methodology",
        "paradigm",
        "strategic",
        "enterprise",
    }

    # Casual vocabulary indicators
    _CASUAL_WORDS: ClassVar[set[str]] = {
        "awesome",
        "cool",
        "hey",
        "wow",
        "super",
        "totally",
        "gonna",
        "wanna",
        "gotta",
        "kinda",
        "stuff",
        "thing",
        "basically",
        "literally",
        "amazing",
        "incredible",
        "insane",
        "crazy",
        "love",
        "hate",
        "fun",
        "easy",
        "simple",
        "free",
    }

    # Luxury vocabulary
    _LUXURY_WORDS: ClassVar[set[str]] = {
        "exclusive",
        "premium",
        "bespoke",
        "curated",
        "artisan",
        "handcrafted",
        "exquisite",
        "refined",
        "elegant",
        "luxurious",
        "sophisticated",
        "prestigious",
        "distinguished",
        "impeccable",
    }

    # Technical vocabulary
    _TECHNICAL_WORDS: ClassVar[set[str]] = {
        "api",
        "sdk",
        "framework",
        "architecture",
        "infrastructure",
        "deploy",
        "kubernetes",
        "docker",
        "microservice",
        "endpoint",
        "authentication",
        "encryption",
        "protocol",
        "algorithm",
        "repository",
        "pipeline",
        "integration",
        "scalable",
    }

    def analyze(self, soup: BeautifulSoup) -> ToneResult:
        """Analyze brand tone from HTML content using heuristic markers.

        Args:
            soup: Parsed HTML document.

        Returns:
            ToneResult with classified tone, formality score, and detected markers.
        """
        text = self._extract_body_text(soup)
        if not text or len(text.strip()) < 20:
            return ToneResult()

        formality = self._analyze_formality(text)
        vocabulary = self._analyze_vocabulary(text)
        markers = self._detect_tone_markers(text)
        tone, confidence = self._classify_tone(formality, vocabulary, markers)

        return ToneResult(
            tone=tone,
            formality_score=formality,
            vocabulary_level=vocabulary,
            confidence=confidence,
            markers=markers,
        )

    def _extract_body_text(self, soup: BeautifulSoup) -> str:
        """Extract visible body text, stripping script/style/nav elements."""
        body = soup.find("body")
        if not body:
            return ""

        # Remove non-content elements
        for tag_name in ["script", "style", "nav", "noscript"]:
            for element in body.find_all(tag_name):
                element.decompose()

        return body.get_text(separator=" ", strip=True)

    def _analyze_formality(self, text: str) -> float:
        """Score formality from 0 (casual) to 1 (formal).

        Based on:
        - Contraction frequency (casual indicator)
        - Formal vocabulary presence
        - Sentence structure (passive voice indicators)
        - Exclamation points (casual indicator)
        """
        words = text.split()
        if not words:
            return 0.5

        word_count = len(words)

        # Count contractions
        contraction_count = len(self._CONTRACTIONS.findall(text))
        contraction_ratio = contraction_count / max(word_count, 1)

        # Count formal words
        text_lower = text.lower()
        formal_count = sum(1 for w in self._FORMAL_WORDS if w in text_lower)
        formal_ratio = formal_count / max(word_count / 50, 1)  # normalize per ~50 words

        # Count casual words
        casual_count = sum(1 for w in self._CASUAL_WORDS if w in text_lower)
        casual_ratio = casual_count / max(word_count / 50, 1)

        # Exclamation points
        exclamation_count = text.count("!")
        exclamation_ratio = exclamation_count / max(word_count / 50, 1)

        # Score: higher = more formal
        score = 0.5
        score -= contraction_ratio * 3  # contractions reduce formality
        score += formal_ratio * 0.15  # formal words increase
        score -= casual_ratio * 0.15  # casual words decrease
        score -= exclamation_ratio * 0.1  # exclamations decrease

        return max(0.0, min(1.0, score))

    def _analyze_vocabulary(self, text: str) -> str:
        """Classify vocabulary level as simple/moderate/sophisticated."""
        words = text.split()
        if not words:
            return "simple"

        # Average word length
        avg_length = sum(len(w) for w in words) / len(words)

        # Count long words (8+ characters)
        long_words = sum(1 for w in words if len(w) >= 8)
        long_ratio = long_words / len(words)

        if avg_length > 6 and long_ratio > 0.2:
            return "sophisticated"
        if avg_length > 4.5 or long_ratio > 0.1:
            return "moderate"
        return "simple"

    _EMOJI_PATTERN = re.compile(
        "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff]"
    )
    _IMPERATIVE_PATTERN = re.compile(
        r"(?:^|[.!?]\s+)(Get|Start|Try|Build|Create|Join|Discover|Learn|Shop|Buy|Sign|Subscribe|Download)\b",
        re.IGNORECASE,
    )

    def _exclamation_marker(self, text: str) -> str | None:
        if text.count("!") >= 3:
            return "frequent_exclamations"
        if text.count("!") >= 1:
            return "uses_exclamations"
        return None

    def _word_count_marker(
        self, text_lower: str, words: tuple[str, ...], threshold: int, marker: str
    ) -> str | None:
        count = sum(1 for w in words if w in text_lower)
        return marker if count >= threshold else None

    def _detect_tone_markers(self, text: str) -> list[str]:
        """Detect specific tone markers in text."""
        text_lower = text.lower()
        candidates: list[str | None] = [
            self._exclamation_marker(text),
            "uses_emoji" if self._EMOJI_PATTERN.search(text) else None,
            "asks_questions" if text.count("?") >= 2 else None,
            "uses_imperatives" if self._IMPERATIVE_PATTERN.search(text) else None,
            "uses_contractions" if self._CONTRACTIONS.search(text) else None,
            self._word_count_marker(
                text_lower, tuple(self._TECHNICAL_WORDS), 3, "technical_jargon"
            ),
            self._word_count_marker(text_lower, tuple(self._LUXURY_WORDS), 2, "luxury_language"),
            self._word_count_marker(text_lower, tuple(self._FORMAL_WORDS), 3, "formal_language"),
            "first_person_plural" if re.search(r"\b(we|our|us)\b", text_lower) else None,
            "second_person_address" if re.search(r"\b(you|your|yours)\b", text_lower) else None,
        ]
        return [m for m in candidates if m]

    _MARKER_WEIGHTS: ClassVar[dict[str, dict[str, float]]] = {
        "frequent_exclamations": {"playful": 1.5, "casual": 1},
        "uses_exclamations": {"friendly": 0.5, "casual": 0.3},
        "uses_emoji": {"playful": 2, "casual": 1},
        "asks_questions": {"friendly": 1},
        "uses_imperatives": {"authoritative": 0.5, "professional": 0.3},
        "uses_contractions": {"casual": 1, "friendly": 0.5},
        "technical_jargon": {"technical": 3},
        "luxury_language": {"luxury": 3},
        "formal_language": {"professional": 1, "authoritative": 1},
        "first_person_plural": {"friendly": 0.5},
        "second_person_address": {"friendly": 0.5, "casual": 0.3},
    }

    @staticmethod
    def _score_formality(scores: dict[str, float], formality: float) -> None:
        if formality >= 0.7:
            scores["professional"] += 2
            scores["authoritative"] += 1.5
            scores["luxury"] += 1
        elif formality >= 0.4:
            scores["professional"] += 1
            scores["friendly"] += 1
        else:
            scores["casual"] += 2
            scores["playful"] += 1.5
            scores["friendly"] += 1

    @staticmethod
    def _score_vocabulary(scores: dict[str, float], vocabulary: str) -> None:
        if vocabulary == "sophisticated":
            scores["luxury"] += 2
            scores["authoritative"] += 1.5
            scores["professional"] += 1
        elif vocabulary == "moderate":
            scores["professional"] += 1
            scores["friendly"] += 0.5
        else:
            scores["casual"] += 1
            scores["playful"] += 0.5

    def _score_markers(self, scores: dict[str, float], markers: list[str]) -> None:
        for marker in markers:
            for tone, w in self._MARKER_WEIGHTS.get(marker, {}).items():
                scores[tone] += w

    def _classify_tone(
        self, formality: float, vocabulary: str, markers: list[str]
    ) -> tuple[str, float]:
        """Classify overall tone from formality, vocabulary, and markers.

        Returns (tone_name, confidence).
        """
        scores: dict[str, float] = {
            "professional": 0.0,
            "casual": 0.0,
            "friendly": 0.0,
            "technical": 0.0,
            "playful": 0.0,
            "luxury": 0.0,
            "authoritative": 0.0,
        }
        self._score_formality(scores, formality)
        self._score_vocabulary(scores, vocabulary)
        self._score_markers(scores, markers)

        sorted_tones = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        top_tone, top_score = sorted_tones[0]
        second_score = sorted_tones[1][1] if len(sorted_tones) > 1 else 0

        if top_score == 0:
            return "professional", 0.0

        confidence = (
            top_score / (top_score + second_score) if (top_score + second_score) > 0 else 0.0
        )
        return top_tone, min(confidence, 1.0)
