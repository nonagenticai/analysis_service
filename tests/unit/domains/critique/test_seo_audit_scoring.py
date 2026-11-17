"""Unit tests for the grouped SEO audit scoring (pure, no DB, no LLM)."""

from __future__ import annotations

from src.domains.critique.services.scanners.seo.scoring import (
    GROUP_WEIGHTS,
    compute_seo_audit_score,
    group_of,
)


def _finding(check_id: str, severity: str = "high") -> dict:
    return {"category": "seo", "check_id": check_id, "severity": severity}


class TestGroupOf:
    def test_legacy_on_page(self):
        assert group_of("seo-missing-title") == "on_page"
        assert group_of("seo-keyword-alignment") == "on_page"  # legacy wins over prefix

    def test_legacy_technical(self):
        assert group_of("seo-robots-txt-missing") == "technical"
        assert group_of("seo-og-tags-missing") == "technical"

    def test_legacy_schema(self):
        assert group_of("seo-structured-data-missing") == "schema"

    def test_new_prefix_groups(self):
        assert group_of("seo-content-thin") == "content"
        assert group_of("seo-keyword-stuffing") == "keyword"
        assert group_of("seo-snippet-no-faq-schema") == "snippet"

    def test_unknown_and_none_default_to_on_page(self):
        assert group_of(None) == "on_page"
        assert group_of("seo-bogus-unmapped") == "on_page"


class TestComputeSeoAuditScore:
    def test_empty_is_perfect(self):
        result = compute_seo_audit_score([])
        assert result["overall_score"] == 100.0
        assert result["grade"] == "A+"
        assert set(result["group_scores"]) == set(GROUP_WEIGHTS)
        assert all(v == 100.0 for v in result["group_scores"].values())

    def test_only_seo_findings_contribute(self):
        # A non-seo finding must not move the score.
        result = compute_seo_audit_score(
            [{"category": "cro", "check_id": "cro-hero-no-h1", "severity": "high"}]
        )
        assert result["overall_score"] == 100.0

    def test_content_penalty_lowers_content_group_and_overall(self):
        result = compute_seo_audit_score([_finding("seo-content-very-thin", "high")])
        content = result["group_scores"]["content"]
        # Not-None is part of the assertion: the group was MEASURED and penalised.
        assert content is not None
        assert content < 100.0
        assert result["overall_score"] < 100.0
        # Other groups stay perfect.
        assert result["group_scores"]["technical"] == 100.0

    def test_weights_sum_to_one(self):
        assert abs(sum(GROUP_WEIGHTS.values()) - 1.0) < 1e-9
