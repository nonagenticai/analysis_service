"""Additional unit tests for tracing module to cover instrumentation branches."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from src.shared.observability.tracing import setup_tracing


class TestTracingInstrumentationBranches:
    """Test the individual instrumentation import branches."""

    def _make_otel_modules(self, *, fastapi=True, httpx=True, sqlalchemy=True):
        """Helper to create otel module mocks with optional missing instrumentors."""
        mock_trace = MagicMock()
        mock_resource_cls = MagicMock()
        mock_provider_cls = MagicMock()
        mock_exporter_cls = MagicMock()
        mock_processor_cls = MagicMock()

        modules: dict[str, MagicMock | None] = {
            "opentelemetry": MagicMock(trace=mock_trace),
            "opentelemetry.trace": mock_trace,
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                OTLPSpanExporter=mock_exporter_cls
            ),
            "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource_cls),
            "opentelemetry.sdk.trace": MagicMock(TracerProvider=mock_provider_cls),
            "opentelemetry.sdk.trace.export": MagicMock(BatchSpanProcessor=mock_processor_cls),
        }

        # Set up instrumentor modules -- None triggers ImportError
        mock_fastapi = MagicMock() if fastapi else None
        mock_httpx = MagicMock() if httpx else None
        mock_sqlalchemy = MagicMock() if sqlalchemy else None

        modules["opentelemetry.instrumentation.fastapi"] = mock_fastapi
        modules["opentelemetry.instrumentation.httpx"] = mock_httpx
        modules["opentelemetry.instrumentation.sqlalchemy"] = mock_sqlalchemy

        return modules, mock_trace, mock_fastapi, mock_httpx, mock_sqlalchemy

    def test_fastapi_missing_does_not_prevent_tracing(self):
        """When FastAPI instrumentor is missing, tracing should still succeed."""
        modules, mock_trace, _, _, _ = self._make_otel_modules(fastapi=False)

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        # set_tracer_provider should still be called
        mock_trace.set_tracer_provider.assert_called_once()

    def test_httpx_missing_does_not_prevent_tracing(self):
        """When httpx instrumentor is missing, tracing should still succeed."""
        modules, mock_trace, _, _, _ = self._make_otel_modules(httpx=False)

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        mock_trace.set_tracer_provider.assert_called_once()

    def test_sqlalchemy_missing_does_not_prevent_tracing(self):
        """When SQLAlchemy instrumentor is missing, tracing should still succeed."""
        modules, mock_trace, _, _, _ = self._make_otel_modules(sqlalchemy=False)

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        mock_trace.set_tracer_provider.assert_called_once()

    def test_all_instrumentors_called_when_present(self):
        """When all instrumentors are present, each should have .instrument() called."""
        modules, _, mock_fastapi, mock_httpx, mock_sqlalchemy = self._make_otel_modules()

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        # Each instrumentor module's class should have instrument() called
        mock_fastapi.FastAPIInstrumentor.return_value.instrument.assert_called_once()
        mock_httpx.HTTPXClientInstrumentor.return_value.instrument.assert_called_once()
        mock_sqlalchemy.SQLAlchemyInstrumentor.return_value.instrument.assert_called_once()

    def test_all_instrumentors_missing_still_succeeds(self):
        """Tracing should succeed even if all three optional instrumentors are missing."""
        modules, mock_trace, _, _, _ = self._make_otel_modules(
            fastapi=False, httpx=False, sqlalchemy=False
        )

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        mock_trace.set_tracer_provider.assert_called_once()

    def test_general_exception_returns_false(self):
        """Non-ImportError exceptions should return False."""
        mock_trace = MagicMock()
        mock_resource = MagicMock()
        mock_resource.create = MagicMock(side_effect=RuntimeError("resource error"))

        modules = {
            "opentelemetry": MagicMock(trace=mock_trace),
            "opentelemetry.trace": mock_trace,
            "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                OTLPSpanExporter=MagicMock()
            ),
            "opentelemetry.sdk.resources": MagicMock(Resource=mock_resource),
            "opentelemetry.sdk.trace": MagicMock(),
            "opentelemetry.sdk.trace.export": MagicMock(),
        }

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is False
        # set_tracer_provider should NOT have been called since resource creation failed
        mock_trace.set_tracer_provider.assert_not_called()

    def test_all_otel_missing_returns_false(self):
        """When all otel packages are missing, should return False."""
        with patch.dict(sys.modules, {"opentelemetry": None}):
            result = setup_tracing()
            assert result is False

    def test_batch_span_processor_wired_to_provider(self):
        """Verify the provider has a span processor added."""
        modules, _, _, _, _ = self._make_otel_modules()
        mock_provider_cls = modules["opentelemetry.sdk.trace"].TracerProvider
        mock_processor_cls = modules["opentelemetry.sdk.trace.export"].BatchSpanProcessor

        with patch.dict(sys.modules, modules):
            result = setup_tracing(service_name="test-svc", environment="test")

        assert result is True
        provider_instance = mock_provider_cls.return_value
        provider_instance.add_span_processor.assert_called_once_with(
            mock_processor_cls.return_value
        )
