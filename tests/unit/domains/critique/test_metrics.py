"""Tests for metrics service."""

from src.domains.critique.services.metrics import (
    _Counter,
    _Gauge,
    _Histogram,
    findings_total,
    jobs_total,
    render_prometheus,
)


class TestCounter:
    def test_inc(self):
        counter = _Counter("test_counter", "Test counter")
        counter.inc()
        counter.inc(2.0)
        output = counter.render()
        assert "test_counter" in output
        assert "3.0" in output

    def test_labels(self):
        counter = _Counter("test_labeled", "Labeled counter")
        counter.inc(status="success")
        counter.inc(status="error")
        output = counter.render()
        assert 'status="success"' in output
        assert 'status="error"' in output


class TestGauge:
    def test_set(self):
        gauge = _Gauge("test_gauge", "Test gauge")
        gauge.set(42.0)
        output = gauge.render()
        assert "42.0" in output

    def test_inc_dec(self):
        gauge = _Gauge("test_gauge2", "Test gauge 2")
        gauge.set(10.0)
        gauge.inc(5.0)
        gauge.dec(3.0)
        output = gauge.render()
        assert "12.0" in output


class TestHistogram:
    def test_observe(self):
        hist = _Histogram("test_hist", "Test histogram", buckets=(1.0, 5.0, 10.0, float("inf")))
        hist.observe(0.5)
        hist.observe(3.0)
        hist.observe(7.0)
        output = hist.render()
        assert "test_hist_bucket" in output
        assert "test_hist_sum" in output
        assert "test_hist_count" in output


class TestRenderPrometheus:
    def test_render(self):
        # Reset some metrics
        jobs_total.inc(status="completed")
        findings_total.inc(category="seo")
        output = render_prometheus()
        assert "critique_jobs_total" in output
        assert "critique_findings_total" in output
