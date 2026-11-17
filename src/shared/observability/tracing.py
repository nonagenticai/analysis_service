"""OpenTelemetry tracing setup with graceful fallback."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def setup_tracing(service_name: str = "analysis-service", environment: str = "development") -> bool:
    """Configure OpenTelemetry tracing if packages are available.

    Returns True if tracing was configured, False if packages are missing.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name,
                "deployment.environment": environment,
            }
        )

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Auto-instrument FastAPI
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor().instrument()
        except ImportError:
            logger.debug("opentelemetry-instrumentation-fastapi not available")

        # Auto-instrument httpx (outbound API calls)
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        except ImportError:
            logger.debug("opentelemetry-instrumentation-httpx not available")

        # Auto-instrument SQLAlchemy
        try:
            from opentelemetry.instrumentation.sqlalchemy import (  # optional tracing dep
                SQLAlchemyInstrumentor,
            )

            SQLAlchemyInstrumentor().instrument()
        except ImportError:
            logger.debug("opentelemetry-instrumentation-sqlalchemy not available")

        logger.info(
            "OpenTelemetry tracing configured: service=%s environment=%s",
            service_name,
            environment,
        )
        return True

    except ImportError:
        logger.info(
            "OpenTelemetry packages not installed -- tracing disabled. "
            "Install with: pip install analysis-service[tracing]"
        )
        return False
    except (OSError, RuntimeError) as exc:
        logger.warning("Failed to configure OpenTelemetry tracing: %s", exc)
        return False
