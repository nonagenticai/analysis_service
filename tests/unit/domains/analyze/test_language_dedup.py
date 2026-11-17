"""Unit tests for language deduplication."""

from src.domains.analyze.services.language_dedup import (
    _extract_lang_and_base,
    detect_language_variants,
)


class TestExtractLangAndBase:
    def test_english_prefix(self) -> None:
        lang, base = _extract_lang_and_base("/en/products")
        assert lang == "en"
        assert base == "/products"

    def test_german_prefix(self) -> None:
        lang, base = _extract_lang_and_base("/de/products")
        assert lang == "de"
        assert base == "/products"

    def test_regional_variant(self) -> None:
        lang, base = _extract_lang_and_base("/pt-BR/about")
        assert lang == "pt"
        assert base == "/about"

    def test_root_with_lang(self) -> None:
        lang, base = _extract_lang_and_base("/en/")
        assert lang == "en"
        assert base == "/"

    def test_bare_root(self) -> None:
        lang, base = _extract_lang_and_base("/")
        assert lang is None
        assert base == "/"

    def test_no_lang_prefix(self) -> None:
        lang, base = _extract_lang_and_base("/products")
        assert lang is None
        assert base == "/products"


class TestDetectLanguageVariants:
    def test_empty_pages(self) -> None:
        assert detect_language_variants([]) == set()

    def test_no_duplicates(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/about"},
            {"page_id": "p2", "url": "https://example.com/pricing"},
        ]
        assert detect_language_variants(pages) == set()

    def test_dedup_keeps_english(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/en/about"},
            {"page_id": "p2", "url": "https://example.com/de/about"},
            {"page_id": "p3", "url": "https://example.com/fr/about"},
        ]
        dupes = detect_language_variants(pages, keep_lang="en")
        assert dupes == {"p2", "p3"}

    def test_dedup_keeps_german(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/en/about"},
            {"page_id": "p2", "url": "https://example.com/de/about"},
        ]
        dupes = detect_language_variants(pages, keep_lang="de")
        assert dupes == {"p1"}

    def test_no_preferred_lang_keeps_first(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/fr/about"},
            {"page_id": "p2", "url": "https://example.com/de/about"},
        ]
        dupes = detect_language_variants(pages, keep_lang="en")
        assert dupes == {"p2"}

    def test_mixed_lang_and_non_lang(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/en/about"},
            {"page_id": "p2", "url": "https://example.com/about"},
        ]
        # Both map to /about, one has lang prefix -> grouped together
        dupes = detect_language_variants(pages, keep_lang="en")
        assert dupes == {"p2"}

    def test_single_page_no_dedup(self) -> None:
        pages = [{"page_id": "p1", "url": "https://example.com/en/about"}]
        assert detect_language_variants(pages) == set()

    def test_root_pages_grouped(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/"},
            {"page_id": "p2", "url": "https://example.com/en/"},
            {"page_id": "p3", "url": "https://example.com/de/"},
        ]
        dupes = detect_language_variants(pages, keep_lang="en")
        assert "p3" in dupes
        assert "p1" in dupes
        assert "p2" not in dupes

    def test_different_paths_not_grouped(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/en/about"},
            {"page_id": "p2", "url": "https://example.com/en/pricing"},
        ]
        assert detect_language_variants(pages) == set()
