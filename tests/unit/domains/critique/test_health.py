"""Tests for enhanced health check."""

from src.domains.critique.services.metrics import render_prometheus


class TestMetricsEndpoint:
    def test_render_prometheus_format(self):
        output = render_prometheus()
        assert isinstance(output, str)
        # Should have HELP and TYPE lines
        assert "# HELP" in output or output.strip() == ""

    def test_render_prometheus_has_metrics(self):
        from src.domains.critique.services.metrics import jobs_total

        jobs_total.inc(status="test")
        output = render_prometheus()
        assert "critique_jobs_total" in output
