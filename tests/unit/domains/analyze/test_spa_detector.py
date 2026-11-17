"""Unit tests for SPA detection."""

from src.domains.analyze.services.spa_detector import detect_spa_pages


class TestDetectSpaPages:
    def test_empty_pages(self) -> None:
        assert detect_spa_pages([]) == set()

    def test_no_homepage(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "about", "title": "Acme"},
            {"page_id": "p2", "page_type": "pricing", "title": "Acme"},
        ]
        assert detect_spa_pages(pages) == set()

    def test_spa_detected(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": "My App"},
            {"page_id": "p3", "page_type": "pricing", "title": "My App"},
        ]
        result = detect_spa_pages(pages)
        assert result == {"p2", "p3"}

    def test_different_titles_not_flagged(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": "About Us"},
            {"page_id": "p3", "page_type": "pricing", "title": "Pricing"},
        ]
        assert detect_spa_pages(pages) == set()

    def test_homepage_not_flagged(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
        ]
        assert detect_spa_pages(pages) == set()

    def test_empty_title_not_flagged(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": ""},
        ]
        assert detect_spa_pages(pages) == set()

    def test_partial_match(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": "My App"},
            {"page_id": "p3", "page_type": "pricing", "title": "Pricing - My App"},
        ]
        result = detect_spa_pages(pages)
        assert result == {"p2"}
        assert "p3" not in result
