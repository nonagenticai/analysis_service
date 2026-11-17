"""Shared text-extraction + tokenization helpers for the SEO audit checks.

The audit-grade SEO groups (content/keyword/snippet) reason over the page's
*prose*, not its markup. These helpers turn a parsed ``soup`` into visible text
and normalized tokens, deterministically and **without mutating** the shared
soup (every scanner receives the same parse tree, so ``extract()`` is forbidden).

Everything here is Tier 0 — pure code on the parsed DOM, no network, no model.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

# Tags whose text is not human-visible page copy.
_NON_TEXT_PARENTS = {"script", "style", "noscript", "template"}
# Boilerplate regions excluded from the *main* content used for readability and
# keyword density (chrome, not article body).
_BOILERPLATE_TAGS = {"nav", "header", "footer", "aside", "form"}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s|$)")

# Compact English stop-word set — enough to keep keyword extraction honest
# without pulling an NLP dependency. Kept deliberately small and self-owned.
STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "for",
        "from",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "me",
        "more",
        "most",
        "my",
        "no",
        "nor",
        "not",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "any",
        "because",
        "before",
        "below",
        "between",
        "both",
        "during",
        "each",
        "few",
        "further",
        "most",
        "now",
        "th",
        "s",
        "re",
        "ve",
        "ll",
        "don",
    ]
)


# Compact Dutch stop-word set, same purpose as ``STOPWORDS``. Applying the
# English set to a Dutch page counts "die", "het", "een", "voor" as keywords.
DUTCH_STOPWORDS: frozenset[str] = frozenset(
    [
        "aan",
        "al",
        "alle",
        "als",
        "bij",
        "dan",
        "dat",
        "de",
        "deze",
        "die",
        "dit",
        "door",
        "een",
        "en",
        "er",
        "haar",
        "hem",
        "het",
        "hier",
        "hij",
        "hoe",
        "hun",
        "ik",
        "in",
        "is",
        "je",
        "jij",
        "kan",
        "maar",
        "me",
        "meer",
        "met",
        "mij",
        "na",
        "naar",
        "niet",
        "nog",
        "nu",
        "of",
        "om",
        "onder",
        "ons",
        "ook",
        "op",
        "over",
        "te",
        "tegen",
        "tot",
        "u",
        "uit",
        "van",
        "veel",
        "voor",
        "wat",
        "we",
        "wel",
        "wie",
        "wij",
        "zal",
        "ze",
        "zich",
        "zij",
        "zijn",
        "zo",
        "zonder",
    ]
)

_STOPWORDS_BY_LANGUAGE: dict[str, frozenset[str]] = {
    "en": STOPWORDS,
    "nl": DUTCH_STOPWORDS,
}


def page_language(soup: BeautifulSoup) -> str:
    """Primary language subtag of ``<html lang>`` (``"nl-NL"`` -> ``"nl"``), or ``""``."""
    html = soup.find("html")
    lang = str(html.get("lang", "")) if isinstance(html, Tag) else ""
    return lang.strip().lower().split("-")[0].split("_")[0]


def stopwords_for_language(lang: str) -> frozenset[str] | None:
    """Stop-words for a page language; ``None`` when the language is unsupported.

    An undeclared language keeps the historical English behaviour. A declared
    language with no stop-word list returns ``None`` so callers SKIP rather than
    judge that page's copy with English rules.
    """
    return _STOPWORDS_BY_LANGUAGE.get(lang or "en")


def visible_text(soup: BeautifulSoup) -> str:
    """All human-visible page text, whitespace-collapsed. Non-mutating.

    Walks text nodes rather than ``extract()``-ing tags, so the shared soup is
    left intact for every other scanner.
    """
    return _collect_text(soup.find("body") or soup, exclude=_NON_TEXT_PARENTS)


def main_text(soup: BeautifulSoup) -> str:
    """The page's primary content text (chrome stripped), whitespace-collapsed.

    Prefers an explicit ``<main>``/``<article>`` region; otherwise falls back to
    the body with nav/header/footer/aside/form excluded. This is the text
    readability and keyword density should reason over — the article, not the
    site furniture.
    """
    root = soup.find("main") or soup.find("article") or soup.find("body") or soup
    return _collect_text(root, exclude=_NON_TEXT_PARENTS | _BOILERPLATE_TAGS)


def _collect_text(root: Tag, *, exclude: set[str]) -> str:
    parts: list[str] = []
    for node in root.descendants:
        if not isinstance(node, NavigableString):
            continue
        parent = node.parent
        if parent is not None and _has_excluded_ancestor(parent, exclude):
            continue
        parts.append(str(node))
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _has_excluded_ancestor(tag: Tag, exclude: set[str]) -> bool:
    """True if ``tag`` or any ancestor is an excluded region."""
    cur: Tag | None = tag
    while cur is not None and isinstance(cur, Tag):
        if cur.name in exclude:
            return True
        cur = cur.parent
    return False


def words(text: str) -> list[str]:
    """Lowercased alphabetic word tokens from a text blob."""
    return [m.group(0).lower() for m in _WORD_RE.finditer(text)]


def content_words(text: str) -> list[str]:
    """Word tokens with stop-words and 1-char tokens removed (for keyword TF)."""
    return [w for w in words(text) if len(w) > 1 and w not in STOPWORDS]


def phrase_in_tokens(phrase: str, text: str) -> bool:
    """True if ``phrase`` occurs as whole word(s) in ``text`` (word-boundary match).

    Tokenizes both sides the same way, then looks for the phrase's tokens as a
    contiguous run. Avoids substring false-positives (``"art"`` must not match
    inside ``"smart"``). A single-word phrase degenerates to token membership.
    """
    needle = words(phrase)
    if not needle:
        return False
    haystack = words(text)
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


def sentence_count(text: str) -> int:
    """Approximate sentence count (>=1 when any text is present)."""
    if not text.strip():
        return 0
    parts = [p for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return max(len(parts), 1)


_VOWELS = "aeiouy"


def syllable_count(word: str) -> int:
    """Heuristic syllable count for an English word (vowel-group counting).

    Counts contiguous vowel runs, then discounts a silent trailing 'e'. Not
    perfect, but stable and dependency-free — good enough for readability scoring
    where only aggregate ratios matter. Every word counts as at least one.
    """
    word = word.lower()
    count = 0
    prev_vowel = False
    for ch in word:
        is_vowel = ch in _VOWELS
        if is_vowel and not prev_vowel:
            count += 1
        prev_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def readability_metrics(text: str) -> dict[str, float]:
    """Return ``{flesch_reading_ease, gunning_fog}`` for a text blob.

    Self-written implementations of the two standard formulas (no external NLP
    dependency):

    - Flesch Reading Ease = 206.835 - 1.015*(words/sentence) - 84.6*(syllables/word)
    - Gunning Fog = 0.4*((words/sentence) + 100*(complex_words/words))

    where a "complex" word has >=3 syllables. Returns neutral values (100 FRE,
    0 fog) when there is no measurable text.
    """
    toks = words(text)
    total = len(toks)
    sentences = sentence_count(text)
    if not total or not sentences:
        return {"flesch_reading_ease": 100.0, "gunning_fog": 0.0}

    syllables = 0
    complex_words = 0
    for w in toks:
        syl = syllable_count(w)
        syllables += syl
        if syl >= 3:
            complex_words += 1

    words_per_sentence = total / sentences
    syllables_per_word = syllables / total
    fre = 206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word
    fog = 0.4 * (words_per_sentence + 100 * (complex_words / total))
    return {"flesch_reading_ease": round(fre, 2), "gunning_fog": round(fog, 2)}
