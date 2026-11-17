"""Industry vertical classification from website content."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from bs4 import BeautifulSoup, Tag


@dataclass
class IndustryClassification:
    """Industry classification result."""

    industry: str = ""  # saas, ecommerce, media, healthcare, finance, education, agency, nonprofit
    sub_industry: str = ""  # more specific category
    confidence: float = 0.0  # 0.0-1.0
    signals: list[str] = field(default_factory=list)  # keywords/signals that led to classification

    def to_dict(self) -> dict:
        return {
            "industry": self.industry,
            "sub_industry": self.sub_industry,
            "confidence": round(self.confidence, 4),
            "signals": self.signals,
        }


# Schema.org @type -> (industry, score) mappings
_SCHEMA_TYPE_MAP: dict[str, tuple[str, float]] = {
    "SoftwareApplication": ("saas", 5.0),
    "WebApplication": ("saas", 5.0),
    "Product": ("ecommerce", 5.0),
    "Offer": ("ecommerce", 5.0),
    "ItemList": ("ecommerce", 5.0),
    "NewsArticle": ("media", 5.0),
    "BlogPosting": ("media", 5.0),
    "Article": ("media", 5.0),
    "MedicalOrganization": ("healthcare", 5.0),
    "Hospital": ("healthcare", 5.0),
    "FinancialService": ("finance", 5.0),
    "BankAccount": ("finance", 5.0),
    "EducationalOrganization": ("education", 5.0),
    "Course": ("education", 5.0),
    "ProfessionalService": ("agency", 3.0),
    "NGO": ("nonprofit", 5.0),
    "NonprofitOrganization": ("nonprofit", 5.0),
}


class IndustryDetector:
    """Classifies a website into an industry vertical based on content signals."""

    INDUSTRY_KEYWORDS: ClassVar[dict[str, dict]] = {
        "saas": {
            "keywords": [
                "saas",
                "software",
                "platform",
                "cloud",
                "subscription",
                "api",
                "dashboard",
                "automation",
                "workflow",
                "integration",
                "deploy",
                "scalable",
                "uptime",
                "sla",
            ],
            "sub_industries": {
                "devtools": ["developer", "sdk", "api", "cli", "framework"],
                "martech": ["marketing", "analytics", "campaign", "crm"],
                "fintech": ["payment", "billing", "invoice", "fintech"],
            },
        },
        "ecommerce": {
            "keywords": [
                "shop",
                "store",
                "cart",
                "checkout",
                "product",
                "price",
                "buy",
                "shipping",
                "order",
                "catalog",
                "sale",
                "discount",
                "inventory",
            ],
            "sub_industries": {
                "fashion": ["clothing", "apparel", "fashion", "wear"],
                "electronics": ["electronics", "gadget", "tech", "device"],
                "marketplace": ["marketplace", "seller", "vendor"],
            },
        },
        "media": {
            "keywords": [
                "news",
                "article",
                "publish",
                "editor",
                "journalist",
                "media",
                "press",
                "blog",
                "content",
                "story",
                "reporter",
                "breaking",
            ],
            "sub_industries": {
                "news": ["news", "breaking", "reporter"],
                "blog": ["blog", "post", "article"],
                "entertainment": ["entertainment", "movie", "music", "game"],
            },
        },
        "healthcare": {
            "keywords": [
                "health",
                "medical",
                "patient",
                "doctor",
                "clinical",
                "hospital",
                "therapy",
                "wellness",
                "diagnosis",
                "treatment",
                "pharmaceutical",
                "care",
            ],
            "sub_industries": {
                "telehealth": ["telehealth", "virtual", "remote"],
                "pharma": ["pharmaceutical", "drug", "medicine"],
            },
        },
        "finance": {
            "keywords": [
                "bank",
                "invest",
                "finance",
                "loan",
                "credit",
                "insurance",
                "trading",
                "portfolio",
                "wealth",
                "mortgage",
                "savings",
                "interest",
            ],
            "sub_industries": {
                "banking": ["bank", "savings", "checking"],
                "insurance": ["insurance", "coverage", "policy"],
                "investment": ["invest", "portfolio", "fund"],
            },
        },
        "education": {
            "keywords": [
                "learn",
                "course",
                "student",
                "teach",
                "education",
                "university",
                "school",
                "curriculum",
                "enrollment",
                "degree",
                "certificate",
                "training",
            ],
            "sub_industries": {
                "edtech": ["online", "platform", "interactive"],
                "university": ["university", "college", "campus"],
            },
        },
        "agency": {
            "keywords": [
                "agency",
                "creative",
                "design",
                "branding",
                "strategy",
                "campaign",
                "client",
                "portfolio",
                "case study",
                "consultation",
                "services",
            ],
            "sub_industries": {
                "digital": ["digital", "web", "seo", "social media"],
                "creative": ["creative", "design", "branding"],
            },
        },
        "nonprofit": {
            "keywords": [
                "donate",
                "nonprofit",
                "charity",
                "volunteer",
                "mission",
                "cause",
                "foundation",
                "community",
                "impact",
                "support",
                "fundraise",
            ],
            "sub_industries": {
                "charity": ["charity", "donate", "fundraise"],
                "advocacy": ["advocate", "awareness", "campaign"],
            },
        },
    }

    def classify(
        self, soup: BeautifulSoup, schema_org: dict | None = None
    ) -> IndustryClassification:
        """Classify a website into an industry vertical.

        Args:
            soup: Parsed HTML document.
            schema_org: Optional schema.org JSON-LD data extracted from the page.

        Returns:
            IndustryClassification with industry, sub_industry, confidence, and signals.
        """
        text_segments = self._extract_text_segments(soup)

        # Score from keyword matching
        keyword_scores = self._score_keywords(text_segments)

        # Score from schema.org type
        schema_scores = self._score_schema_type(schema_org)

        # Merge scores
        all_industries = set(keyword_scores.keys()) | set(schema_scores.keys())
        combined_scores: dict[str, float] = {}
        for industry in all_industries:
            combined_scores[industry] = keyword_scores.get(industry, 0.0) + schema_scores.get(
                industry, 0.0
            )

        if not combined_scores or max(combined_scores.values()) == 0:
            return IndustryClassification()

        # Sort by score descending
        sorted_industries = sorted(combined_scores.items(), key=lambda x: x[1], reverse=True)
        top_industry, top_score = sorted_industries[0]
        second_score = sorted_industries[1][1] if len(sorted_industries) > 1 else 0.0

        # Calculate confidence
        confidence = top_score / (top_score + second_score) if top_score > 0 else 0.0

        # Detect sub-industry
        sub_industry = self._detect_sub_industry(top_industry, text_segments)

        # Collect signals: keywords that were actually matched
        signals = self._collect_signals(top_industry, text_segments, schema_org)

        return IndustryClassification(
            industry=top_industry,
            sub_industry=sub_industry,
            confidence=confidence,
            signals=signals,
        )

    @staticmethod
    def _meta_attr_text(soup: BeautifulSoup, attrs: dict) -> str:
        """Return trimmed content of a single matching <meta>, or empty string."""
        meta = soup.find("meta", attrs=attrs)
        if not (meta and isinstance(meta, Tag)):
            return ""
        content = meta.get("content", "")
        if isinstance(content, list):
            content = content[0] if content else ""
        return str(content).strip()

    @staticmethod
    def _extract_headings_text(soup: BeautifulSoup) -> str:
        parts: list[str] = []
        for tag in soup.find_all(["h1", "h2", "h3"]):
            if isinstance(tag, Tag):
                text = tag.get_text(strip=True)
                if text:
                    parts.append(text)
        return " ".join(parts)

    def _extract_meta_text(self, soup: BeautifulSoup) -> str:
        parts: list[str] = []
        title_tag = soup.find("title")
        if title_tag and isinstance(title_tag, Tag):
            text = title_tag.get_text(strip=True)
            if text:
                parts.append(text)
        for attrs in (
            {"name": "description"},
            {"property": "og:description"},
            {"name": "keywords"},
        ):
            content = self._meta_attr_text(soup, attrs)
            if content:
                parts.append(content)
        return " ".join(parts)

    @staticmethod
    def _extract_body_text(soup: BeautifulSoup, limit: int = 2000) -> str:
        parts: list[str] = []
        total_chars = 0
        for p_tag in soup.find_all("p"):
            if not isinstance(p_tag, Tag):
                continue
            text = p_tag.get_text(strip=True)
            if not text:
                continue
            parts.append(text)
            total_chars += len(text)
            if total_chars >= limit:
                break
        return " ".join(parts)[:limit]

    def _extract_text_segments(self, soup: BeautifulSoup) -> dict[str, str]:
        """Extract text segments from the parsed HTML, grouped by importance.

        Args:
            soup: Parsed HTML document.

        Returns:
            Dict with keys "headings", "meta", "body" containing extracted text.
        """
        return {
            "headings": self._extract_headings_text(soup),
            "meta": self._extract_meta_text(soup),
            "body": self._extract_body_text(soup),
        }

    def _score_keywords(self, text_segments: dict[str, str]) -> dict[str, float]:
        """Score each industry based on keyword matches across text segments.

        Heading matches are weighted 3x, meta matches 2x, body matches 1x.

        Args:
            text_segments: Dict with "headings", "meta", "body" text.

        Returns:
            Dict mapping industry name to weighted score.
        """
        headings_lower = text_segments.get("headings", "").lower()
        meta_lower = text_segments.get("meta", "").lower()
        body_lower = text_segments.get("body", "").lower()

        scores: dict[str, float] = {}

        for industry, config in self.INDUSTRY_KEYWORDS.items():
            keywords = config["keywords"]
            score = 0.0

            for keyword in keywords:
                keyword_lower = keyword.lower()
                # Count occurrences in each segment
                heading_count = headings_lower.count(keyword_lower)
                meta_count = meta_lower.count(keyword_lower)
                body_count = body_lower.count(keyword_lower)

                score += heading_count * 3.0
                score += meta_count * 2.0
                score += body_count * 1.0

            scores[industry] = score

        return scores

    def _score_schema_type(self, schema_org: dict | None) -> dict[str, float]:
        """Score industries based on schema.org @type.

        Args:
            schema_org: Schema.org JSON-LD data, or None.

        Returns:
            Dict mapping industry name to schema-based score.
        """
        scores: dict[str, float] = {}

        if not schema_org:
            return scores

        schema_type = schema_org.get("@type", "")

        # Handle list of types
        if isinstance(schema_type, list):
            types = schema_type
        else:
            types = [schema_type] if schema_type else []

        for t in types:
            t_str = str(t).strip()
            if t_str in _SCHEMA_TYPE_MAP:
                industry, boost = _SCHEMA_TYPE_MAP[t_str]
                scores[industry] = scores.get(industry, 0.0) + boost

        return scores

    def _detect_sub_industry(self, industry: str, text_segments: dict[str, str]) -> str:
        """Detect a sub-industry within the matched industry.

        Args:
            industry: The top-level industry classification.
            text_segments: Dict with "headings", "meta", "body" text.

        Returns:
            The highest-scoring sub-industry name, or "" if none matched.
        """
        if industry not in self.INDUSTRY_KEYWORDS:
            return ""

        sub_industries = self.INDUSTRY_KEYWORDS[industry].get("sub_industries", {})
        if not sub_industries:
            return ""

        combined_text = " ".join(text_segments.values()).lower()

        best_sub = ""
        best_count = 0

        for sub_name, sub_keywords in sub_industries.items():
            count = 0
            for keyword in sub_keywords:
                count += combined_text.count(keyword.lower())
            if count > best_count:
                best_count = count
                best_sub = sub_name

        return best_sub

    def _collect_signals(
        self, industry: str, text_segments: dict[str, str], schema_org: dict | None
    ) -> list[str]:
        """Collect the specific signals (keywords) that matched for the given industry.

        Args:
            industry: The classified industry.
            text_segments: Dict with "headings", "meta", "body" text.
            schema_org: Schema.org data, or None.

        Returns:
            List of matched keyword/signal strings.
        """
        signals: list[str] = []

        if industry in self.INDUSTRY_KEYWORDS:
            combined_text = " ".join(text_segments.values()).lower()
            for keyword in self.INDUSTRY_KEYWORDS[industry]["keywords"]:
                if keyword.lower() in combined_text:
                    signals.append(keyword)

        # Add schema.org signal if applicable
        if schema_org:
            schema_type = schema_org.get("@type", "")
            if isinstance(schema_type, list):
                for t in schema_type:
                    t_str = str(t).strip()
                    if t_str in _SCHEMA_TYPE_MAP and _SCHEMA_TYPE_MAP[t_str][0] == industry:
                        signals.append(f"schema:{t_str}")
            else:
                t_str = str(schema_type).strip()
                if t_str in _SCHEMA_TYPE_MAP and _SCHEMA_TYPE_MAP[t_str][0] == industry:
                    signals.append(f"schema:{t_str}")

        return signals
