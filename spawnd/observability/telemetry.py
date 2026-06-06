"""OpenTelemetry integration with Postgres trace mirroring."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

from spawnd.config import ResolvedTelemetryConfig
from spawnd.artifacts.redaction import redact_attributes
from spawnd.state.repository import DeployedRepository


class TelemetryUnavailable(RuntimeError):
    """Telemetry was required but could not be initialized."""


@dataclass
class LocalSpan:
    """Span state mirrored to Postgres."""

    run_id: str
    agent: str | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    attributes: dict[str, Any]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = 'ok'

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append({'name': name, 'attributes': redact_attributes(attributes or {})})

    def set_error(self, error: str) -> None:
        self.status = 'error'
        self.add_event('exception', {'error': error})


class TelemetryRecorder:
    """Small tracing facade used by deployed backend code."""

    def __init__(self, config: ResolvedTelemetryConfig, repository: DeployedRepository | None = None) -> None:
        self.config = config
        self.repository = repository
        self._otel_tracer: Any | None = None
        self._otel_error: str | None = None
        if config.enabled and config.exporter == 'otlp':
            self._otel_tracer = self._init_otel()

    def _init_otel(self) -> Any | None:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except Exception as exc:
            self._otel_error = str(exc)
            if self.config.failure_policy == 'fail':
                raise TelemetryUnavailable(f'OTLP telemetry unavailable: {exc}') from exc
            return None
        provider = TracerProvider(resource=Resource.create({'service.name': 'spawnd'}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        return trace.get_tracer('spawnd')

    @contextmanager
    def span(
        self,
        name: str,
        *,
        run_id: str,
        agent: str | None = None,
        attributes: dict[str, Any] | None = None,
        parent: LocalSpan | None = None,
    ) -> Iterator[LocalSpan]:
        trace_id = parent.trace_id if parent else uuid4().hex
        local = LocalSpan(
            run_id=run_id,
            agent=agent,
            trace_id=trace_id,
            span_id=uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            name=name,
            attributes=redact_attributes(attributes or {}),
        )
        otel_context = None
        if self._otel_tracer is not None:
            otel_context = self._otel_tracer.start_as_current_span(name, attributes=local.attributes)
        try:
            if otel_context is not None:
                with otel_context as span:
                    yield local
                    for event in local.events:
                        span.add_event(event['name'], event.get('attributes', {}))
                    if local.status == 'error':
                        span.set_status('ERROR')
            else:
                yield local
        except Exception as exc:
            local.set_error(str(exc))
            raise
        finally:
            self._mirror_span(local)

    def record_error(self, run_id: str, error: str) -> None:
        if self.repository is None:
            return
        self.repository.append_event(run_id, '_system', 'telemetry_error', {'error': error})

    @property
    def initialization_error(self) -> str | None:
        return self._otel_error

    def _mirror_span(self, span: LocalSpan) -> None:
        if self.repository is None:
            return
        ended_at = datetime.now(timezone.utc)
        export_status = 'exported' if self._otel_tracer is not None else 'mirrored'
        if self._otel_error:
            export_status = 'degraded'
        self.repository.record_trace_span(
            run_id=span.run_id,
            agent=span.agent,
            trace_id=span.trace_id,
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            name=span.name,
            status=span.status,
            started_at=span.started_at,
            ended_at=ended_at,
            attributes=span.attributes,
            events=span.events,
            export_status=export_status,
        )
