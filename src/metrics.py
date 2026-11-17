"""Prometheus metrics definitions for analysis_service.

Merges metrics from all five source services into a single module
with a unified naming convention using the 'analysis_' prefix.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# HTTP request metrics (shared)
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "analysis_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "analysis_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ---------------------------------------------------------------------------
# LLM metrics (shared across domains)
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "analysis_llm_calls_total",
    "Total LLM API calls",
    ["domain", "status"],
)

llm_latency_seconds = Histogram(
    "analysis_llm_latency_seconds",
    "LLM API call latency in seconds",
    ["domain"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

llm_tokens_total = Counter(
    "analysis_llm_tokens_total",
    "Total tokens consumed by LLM API calls",
    ["domain", "direction"],  # direction: input, output
)

# ---------------------------------------------------------------------------
# Active jobs gauge (shared)
# ---------------------------------------------------------------------------

active_jobs = Gauge(
    "analysis_active_jobs",
    "Currently active jobs",
    ["domain"],
)

# ---------------------------------------------------------------------------
# Analyze domain metrics
# ---------------------------------------------------------------------------

analyze_jobs_total = Counter(
    "analysis_analyze_jobs_total",
    "Total analysis jobs",
    ["status"],
)

analyze_pages_analyzed_total = Counter(
    "analysis_analyze_pages_analyzed_total",
    "Total pages analyzed",
)

# ---------------------------------------------------------------------------
# Critique domain metrics
# ---------------------------------------------------------------------------

critique_jobs_total = Counter(
    "analysis_critique_jobs_total",
    "Total critique scan jobs",
    ["status"],
)

critique_scanner_duration_seconds = Histogram(
    "analysis_critique_scanner_duration_seconds",
    "Per-scanner duration in seconds",
    ["scanner"],
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 180.0],
)

# ---------------------------------------------------------------------------
# Review domain metrics
# ---------------------------------------------------------------------------

review_jobs_total = Counter(
    "analysis_review_jobs_total",
    "Total review jobs created",
    ["tenant_id", "status"],
)

review_llm_latency_seconds = Histogram(
    "analysis_review_llm_latency_seconds",
    "Review LLM call latency in seconds",
    ["review_type"],
    buckets=(1, 5, 10, 30, 60, 120, 180, 300),
)

review_errors_total = Counter(
    "analysis_review_errors_total",
    "Total review processing errors",
    ["review_type", "error_type"],
)

review_cache_hits = Counter(
    "analysis_review_cache_hits_total",
    "Cache hits for review results",
)

review_cache_misses = Counter(
    "analysis_review_cache_misses_total",
    "Cache misses for review results",
)

review_rate_limited_total = Counter(
    "analysis_review_rate_limited_total",
    "Total rate limited requests",
    ["tenant_id"],
)

# ---------------------------------------------------------------------------
# Brand domain metrics
# ---------------------------------------------------------------------------

brand_jobs_total = Counter(
    "analysis_brand_jobs_total",
    "Total brand extraction jobs",
    ["status"],
)

# ---------------------------------------------------------------------------
# Backward-compatible aliases used by domain code from original services
# ---------------------------------------------------------------------------

# Analyze domain aliases (original analyzer_service metric names)
analyzer_llm_calls_total = Counter(
    "analyzer_llm_calls_total",
    "Total LLM calls from analyzer",
    ["status"],
)

analyzer_llm_latency_seconds = Histogram(
    "analyzer_llm_latency_seconds",
    "Analyzer LLM call latency",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

analyzer_review_calls_total = Counter(
    "analyzer_review_calls_total",
    "Total review calls from analyzer",
    ["review_type", "status"],
)

analyzer_review_latency_seconds = Histogram(
    "analyzer_review_latency_seconds",
    "Analyzer review latency",
    ["review_type"],
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

# Review domain aliases
review_active_jobs = Gauge(
    "review_active_jobs",
    "Currently active review jobs",
)

review_llm_tokens_total = Counter(
    "review_llm_tokens_total",
    "Total tokens used by review LLM calls",
    ["review_type", "direction"],
)
