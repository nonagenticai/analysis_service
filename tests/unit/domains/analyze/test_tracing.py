"""Tests for OpenTelemetry tracing setup."""

from unittest.mock import MagicMock, patch

from src.shared.observability.tracing import setup_tracing


class TestSetupTracing:
    def test_returns_false_when_otel_not_installed(self):
        """setup_tracing should return False when otel packages are missing."""
        with patch.dict("sys.modules", {"opentelemetry": None}):
            result = setup_tracing()
            assert result is False

    def test_returns_true_or_false_based_on_installation(self):
        """setup_tracing should return a concrete True/False, not just truthy/falsy."""
        result = setup_tracing(service_name="test-service", environment="test")
        assert result is True or result is False

    def test_passes_service_name_to_resource(self):
        """Custom service_name and environment should be passed through to the resource."""
        mock_trace = MagicMock()
        mock_resource_cls = MagicMock()
        mock_provider_cls = MagicMock()
        mock_exporter_cls = MagicMock()
        mock_processor_cls = MagicMock()

        # Track what Resource.create is called with
        captured_attrs = {}

        def capture_create(attrs):
            captured_attrs.update(attrs)
            return MagicMock()

        mock_resource_cls.create = capture_create

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry": MagicMock(trace=mock_trace),
                "opentelemetry.trace": mock_trace,
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                    OTLPSpanExporter=mock_exporter_cls
                ),
                "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
                "opentelemetry.sdk.trace": MagicMock(TracerProvider=mock_provider_cls),
                "opentelemetry.sdk.trace.export": MagicMock(BatchSpanProcessor=mock_processor_cls),
                "opentelemetry.instrumentation.fastapi": MagicMock(),
                "opentelemetry.instrumentation.httpx": MagicMock(),
                "opentelemetry.instrumentation.sqlalchemy": MagicMock(),
            },
        ):
            result = setup_tracing(service_name="my-custom-svc", environment="staging")

        assert result is True
        assert captured_attrs["service.name"] == "my-custom-svc"
        assert captured_attrs["deployment.environment"] == "staging"

    def test_sets_tracer_provider_on_success(self):
        """On successful setup, trace.set_tracer_provider should be called."""
        mock_trace = MagicMock()
        mock_resource_cls = MagicMock()
        mock_provider_cls = MagicMock()
        mock_exporter_cls = MagicMock()
        mock_processor_cls = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry": MagicMock(trace=mock_trace),
                "opentelemetry.trace": mock_trace,
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                    OTLPSpanExporter=mock_exporter_cls
                ),
                "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
                "opentelemetry.sdk.trace": MagicMock(TracerProvider=mock_provider_cls),
                "opentelemetry.sdk.trace.export": MagicMock(BatchSpanProcessor=mock_processor_cls),
                "opentelemetry.instrumentation.fastapi": MagicMock(),
                "opentelemetry.instrumentation.httpx": MagicMock(),
                "opentelemetry.instrumentation.sqlalchemy": MagicMock(),
            },
        ):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        mock_trace.set_tracer_provider.assert_called_once()

    def test_returns_false_on_non_import_exception(self):
        """Non-ImportError exceptions should return False (not crash)."""
        mock_trace = MagicMock()
        mock_resource_cls = MagicMock()
        mock_resource_cls.create = MagicMock(side_effect=RuntimeError("resource error"))

        with patch.dict(
            "sys.modules",
            {
                "opentelemetry": MagicMock(trace=mock_trace),
                "opentelemetry.trace": mock_trace,
                "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                    OTLPSpanExporter=MagicMock()
                ),
                "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
                "opentelemetry.sdk.trace": MagicMock(),
                "opentelemetry.sdk.trace.export": MagicMock(),
            },
        ):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is False


class TestTracingSettingsDefaults:
    def test_tracing_disabled_by_default(self):
        from src.config import TracingSettings

        settings = TracingSettings()
        assert settings.enabled is False

    def test_default_endpoint_is_blank(self):
        """Tracing is opt-in (TRACING_ENDPOINT); no localhost lock-in."""
        from src.config import TracingSettings

        settings = TracingSettings()
        assert settings.endpoint == ""

    def test_default_sample_rate_is_100_percent(self):
        from src.config import TracingSettings

        settings = TracingSettings()
        assert settings.sample_rate == 1.0

    def test_default_service_name(self):
        from src.config import TracingSettings

        settings = TracingSettings()
        assert settings.service_name == "analysis-service"
