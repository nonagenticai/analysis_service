"""Tests for src.metrics -- Prometheus metrics behavior and configuration."""

from prometheus_client import Counter, Gauge, Histogram

from src.metrics import (
    http_request_duration_seconds,
    http_requests_total,
    review_active_jobs,
    review_cache_hits,
    review_cache_misses,
    review_errors_total,
    review_jobs_total,
    review_llm_latency_seconds,
    review_llm_tokens_total,
    review_rate_limited_total,
)


class TestCounterMetrics:
    """Verify counters are correctly typed and labeled."""

    def test_review_jobs_total_is_counter_with_labels(self):
        assert isinstance(review_jobs_total, Counter)
        # prometheus_client strips _total suffix from internal _name for Counters
        assert "analysis_review_jobs" in review_jobs_total._name
        assert "tenant_id" in review_jobs_total._labelnames
        assert "status" in review_jobs_total._labelnames

    def test_review_errors_total_has_correct_labels(self):
        assert isinstance(review_errors_total, Counter)
        assert "review_type" in review_errors_total._labelnames
        assert "error_type" in review_errors_total._labelnames

    def test_review_llm_tokens_total_tracks_direction(self):
        assert isinstance(review_llm_tokens_total, Counter)
        assert "review_type" in review_llm_tokens_total._labelnames
        assert "direction" in review_llm_tokens_total._labelnames

    def test_review_cache_hits_is_unlabeled_counter(self):
        assert isinstance(review_cache_hits, Counter)
        assert review_cache_hits._labelnames == ()

    def test_review_cache_misses_is_unlabeled_counter(self):
        assert isinstance(review_cache_misses, Counter)
        assert review_cache_misses._labelnames == ()

    def test_review_rate_limited_total_has_tenant_label(self):
        assert isinstance(review_rate_limited_total, Counter)
        assert "tenant_id" in review_rate_limited_total._labelnames

    def test_http_requests_total_has_method_endpoint_status(self):
        assert isinstance(http_requests_total, Counter)
        labels = set(http_requests_total._labelnames)
        assert labels == {"method", "endpoint", "status_code"}


class TestHistogramMetrics:
    """Verify histograms have correct type, name, and bucket configuration."""

    def test_review_llm_latency_has_review_type_label(self):
        assert isinstance(review_llm_latency_seconds, Histogram)
        assert "review_type" in review_llm_latency_seconds._labelnames

    def test_review_llm_latency_buckets_cover_expected_range(self):
        # Buckets should cover 1s to 300s for LLM calls
        buckets = review_llm_latency_seconds._kwargs.get(
            "buckets", review_llm_latency_seconds._upper_bounds
        )
        assert min(buckets[:-1]) <= 5.0, "Smallest bucket should be <= 5s for fast LLM calls"
        assert max(buckets[:-1]) >= 120.0, "Largest bucket should be >= 120s for slow LLM calls"

    def test_http_request_duration_has_method_and_endpoint(self):
        assert isinstance(http_request_duration_seconds, Histogram)
        labels = set(http_request_duration_seconds._labelnames)
        assert labels == {"method", "endpoint"}


class TestGaugeMetrics:
    """Verify gauge metrics have correct type and name."""

    def test_review_active_jobs_is_gauge(self):
        assert isinstance(review_active_jobs, Gauge)
        assert review_active_jobs._name == "review_active_jobs"


class TestMetricNamingConvention:
    """Verify metrics follow the analysis_ prefix convention for new metrics."""

    def test_new_metrics_use_analysis_prefix(self):
        # New metrics should use the unified analysis_ prefix
        assert http_requests_total._name.startswith("analysis_")
        assert http_request_duration_seconds._name.startswith("analysis_")
        assert review_jobs_total._name.startswith("analysis_")
        assert review_errors_total._name.startswith("analysis_")
        assert review_cache_hits._name.startswith("analysis_")
        assert review_cache_misses._name.startswith("analysis_")
        assert review_rate_limited_total._name.startswith("analysis_")

    def test_backward_compatible_aliases_keep_old_names(self):
        # review_active_jobs is a backward-compatible alias
        assert review_active_jobs._name == "review_active_jobs"
