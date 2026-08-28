"""OpenTelemetry setup: one TracerProvider for the whole process, auto-
instrumentation for FastAPI/httpx/SQLAlchemy/Redis (the transport layers
every stage below rides on), and a `traced_span` helper for the four
pipeline stages requirement 1 specifically asks to be traceable:

    ingestion (app.services.pipeline.parse_pdf_bytes)
    LLM agent validation (app.agents.pipeline.extract_and_audit_clause)
    OPA evaluation (app.execution.opa_engine.OPAEngine.evaluate)
    audit logging (app.ledger.service.LedgerService.append_entry)

Auto-instrumentation alone gives you "an HTTP request came in and an HTTP
call went out to OPA/Postgres/Redis" -- it cannot know that the 40ms in
between was specifically "the Logic Auditor Agent checking this clause"
versus "JSON-serializing the response". The manual spans at those four
call sites are what turns a trace from "this request was slow" into "the
LLM agent audit step was slow, specifically for rule_id=X" -- the whole
point of instrumenting a multi-stage pipeline rather than a single service.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode

from app.config import Settings

logger = logging.getLogger(__name__)

_TRACER_NAME = "regengine.pipeline"


def setup_tracing(app: Any, settings: Settings) -> None:
    """Call once, at process startup (app.main, module level -- before
    the first request, since FastAPIInstrumentor patches the ASGI app).
    A no-op (not even auto-instrumentation) when `settings.otel_enabled`
    is False, so tracing overhead is fully removable for, e.g., a
    latency-sensitive load test run."""
    if not settings.otel_enabled:
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED=false).")
        return

    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    sampler = TraceIdRatioBased(settings.otel_traces_sample_ratio)
    provider = TracerProvider(resource=resource, sampler=sampler)

    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        logger.info("OpenTelemetry traces exporting via OTLP/HTTP to %s", settings.otel_exporter_otlp_endpoint)
    else:
        # Zero-config local-dev fallback: spans print to stdout instead of
        # silently vanishing when no collector endpoint is configured --
        # the same "boots cleanly against localhost with no external
        # service required" philosophy the rest of app/config.py follows.
        exporter = ConsoleSpanExporter()
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set; tracing to stdout via ConsoleSpanExporter (dev only).")

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # Auto-instrumentation for the transport layers every manual span
    # below eventually calls into -- so a trace shows "OPA evaluation"
    # (manual span) containing the actual outbound httpx POST (auto span)
    # as a child, not just one opaque block of time.
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    logger.info("OpenTelemetry tracing initialized (service.name=%s, sample_ratio=%.2f).", settings.otel_service_name, settings.otel_traces_sample_ratio)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[Span]:
    """Thin wrapper over `tracer.start_as_current_span` that also records
    any exception raised inside the block onto the span (message +
    ERROR status) before letting it propagate -- so a failed ingestion/
    agent/OPA/ledger call is visible in trace search ("show me every
    ERROR-status ledger.append_entry span") without every call site
    needing its own try/except boilerplate to set that status by hand.

    Usage:
        with traced_span("ingestion.parse_pdf_bytes", filename=filename, size_bytes=len(data)):
            ...
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
