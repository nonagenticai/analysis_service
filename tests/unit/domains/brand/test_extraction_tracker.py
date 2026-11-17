"""Tests for extraction method tracker."""

from __future__ import annotations

import time

import pytest

from src.domains.brand.services.extraction_tracker import ExtractionTracker


class TestExtractionTracker:
    def test_record_single(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.9)

        results = tracker.get_all_results()
        assert len(results) == 1
        assert results[0].method == "html_meta"
        assert results[0].field == "primary_color"
        assert results[0].value == "#FF5733"
        assert results[0].confidence == 0.9

    def test_record_multiple(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.9)
        tracker.record("css_parser", "font_family", "Roboto", confidence=0.8)
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png", confidence=0.7)

        results = tracker.get_all_results()
        assert len(results) == 3
        assert results[0].method == "html_meta"
        assert results[1].method == "css_parser"
        assert results[2].method == "schema_org"
        assert results[0].field == "primary_color"
        assert results[1].field == "font_family"
        assert results[2].field == "logo_url"

    def test_get_methods_used_unique(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733")
        tracker.record("css_parser", "font_family", "Roboto")
        tracker.record("html_meta", "name", "Acme Corp")
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png")
        tracker.record("css_parser", "accent_color", "#2563EB")

        methods = tracker.get_methods_used()
        assert methods == ["html_meta", "css_parser", "schema_org"]

    def test_get_methods_used_empty(self) -> None:
        tracker = ExtractionTracker()
        assert tracker.get_methods_used() == []

    def test_get_field_sources(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.9)
        tracker.record("css_parser", "primary_color", "#E74C3C", confidence=0.7)
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png", confidence=0.8)

        sources = tracker.get_field_sources("primary_color")
        assert len(sources) == 2
        assert sources[0].method == "html_meta"
        assert sources[0].value == "#FF5733"
        assert sources[1].method == "css_parser"
        assert sources[1].value == "#E74C3C"

    def test_get_field_sources_no_match(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733")

        sources = tracker.get_field_sources("nonexistent_field")
        assert sources == []

    def test_overall_confidence_single(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.85)

        assert tracker.overall_confidence() == pytest.approx(0.85)

    def test_overall_confidence_multiple_fields(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.8)
        tracker.record("css_parser", "font_family", "Roboto", confidence=0.6)

        # Average of per-field max: (0.8 + 0.6) / 2 = 0.7
        assert tracker.overall_confidence() == pytest.approx(0.7)

    def test_overall_confidence_same_field_multiple_methods(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.6)
        tracker.record("css_parser", "primary_color", "#E74C3C", confidence=0.9)
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png", confidence=0.8)

        # Per-field max: primary_color=0.9, logo_url=0.8
        # Average: (0.9 + 0.8) / 2 = 0.85
        assert tracker.overall_confidence() == pytest.approx(0.85)

    def test_overall_confidence_empty(self) -> None:
        tracker = ExtractionTracker()
        assert tracker.overall_confidence() == 0.0

    def test_field_count(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733")
        tracker.record("css_parser", "primary_color", "#E74C3C")
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png")
        tracker.record("html_meta", "name", "Acme Corp")

        assert tracker.field_count() == 3

    def test_result_count(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733")
        tracker.record("css_parser", "primary_color", "#E74C3C")
        tracker.record("schema_org", "logo_url", "https://example.com/logo.png")

        assert tracker.result_count() == 3

    def test_to_dict_structure(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.9)

        d = tracker.to_dict()
        assert "methods_used" in d
        assert "overall_confidence" in d
        assert "field_count" in d
        assert "result_count" in d
        assert "results" in d
        assert isinstance(d["methods_used"], list)
        assert isinstance(d["overall_confidence"], float)
        assert isinstance(d["field_count"], int)
        assert isinstance(d["result_count"], int)
        assert isinstance(d["results"], list)

    def test_to_dict_results(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=0.9)
        tracker.record("css_parser", "font_family", "Roboto", confidence=0.8)

        d = tracker.to_dict()
        assert d["methods_used"] == ["html_meta", "css_parser"]
        assert d["overall_confidence"] == pytest.approx(0.85)
        assert d["field_count"] == 2
        assert d["result_count"] == 2
        assert len(d["results"]) == 2

        r0 = d["results"][0]
        assert r0["method"] == "html_meta"
        assert r0["field"] == "primary_color"
        assert r0["value"] == "#FF5733"
        assert r0["confidence"] == 0.9
        assert "timestamp_ms" in r0

        r1 = d["results"][1]
        assert r1["method"] == "css_parser"
        assert r1["field"] == "font_family"
        assert r1["value"] == "Roboto"
        assert r1["confidence"] == 0.8

    def test_confidence_clamped(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733", confidence=1.5)
        tracker.record("css_parser", "font_family", "Roboto", confidence=-0.3)

        results = tracker.get_all_results()
        assert results[0].confidence == 1.0
        assert results[1].confidence == 0.0

    def test_timestamp_ms_positive(self) -> None:
        tracker = ExtractionTracker()
        tracker.record("html_meta", "primary_color", "#FF5733")
        time.sleep(0.01)  # small delay to ensure non-zero timestamp
        tracker.record("css_parser", "font_family", "Roboto")

        results = tracker.get_all_results()
        assert results[0].timestamp_ms >= 0
        assert results[1].timestamp_ms >= 0
        assert results[1].timestamp_ms >= results[0].timestamp_ms
