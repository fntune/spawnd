"""Runtime observation boundary for deployed execution."""
from __future__ import annotations

from typing import Any, Protocol


class RuntimeObserver(Protocol):
    """Facts emitted by runtimes without owning persistence."""

    def event(self, event_type: str, data: dict[str, Any] | None = None) -> None: ...
    def invocation(self, kind: str, data: dict[str, Any] | None = None) -> None: ...
    def usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        source: str = "unknown",
        raw: dict[str, Any] | None = None,
    ) -> None: ...
    def final(self, text: str) -> None: ...
    def error(self, source: str, message: str, data: dict[str, Any] | None = None) -> None: ...


class NullRuntimeObserver:
    """Observer used when a runtime is executed outside a deployed worker."""

    def event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        _ = (event_type, data)

    def invocation(self, kind: str, data: dict[str, Any] | None = None) -> None:
        _ = (kind, data)

    def usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        source: str = "unknown",
        raw: dict[str, Any] | None = None,
    ) -> None:
        _ = (input_tokens, output_tokens, cost_usd, source, raw)

    def final(self, text: str) -> None:
        _ = text

    def error(self, source: str, message: str, data: dict[str, Any] | None = None) -> None:
        _ = (source, message, data)


class RecordingRuntimeObserver:
    """In-memory observer used by workers before artifact ids are known."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.invocations: list[tuple[str, dict[str, Any]]] = []
        self.errors: list[tuple[str, str, dict[str, Any]]] = []
        self.final_messages: list[str] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.cost_source = "unknown"
        self.raw_usage: dict[str, Any] = {}

    def event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event_type, dict(data or {})))

    def invocation(self, kind: str, data: dict[str, Any] | None = None) -> None:
        self.invocations.append((kind, dict(data or {})))

    def usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        source: str = "unknown",
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost_usd
        self.cost_source = source
        if raw:
            self.raw_usage.update(raw)

    def final(self, text: str) -> None:
        if text:
            self.final_messages.append(text)

    def error(self, source: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.errors.append((source, message, dict(data or {})))

    @property
    def final_text(self) -> str:
        return "\n".join(self.final_messages)


class PostgresRuntimeObserver(RecordingRuntimeObserver):
    """Runtime observer that flushes redacted facts into deployed state."""

    def __init__(
        self,
        *,
        repository: Any,
        run_id: str,
        agent: str,
        attempt_id: str,
        session_id: str,
        invocation_id: str,
        provider: str,
        runtime: str,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.run_id = run_id
        self.agent = agent
        self.attempt_id = attempt_id
        self.session_id = session_id
        self.invocation_id = invocation_id
        self.provider = provider
        self.runtime = runtime
        self._flushed_events = 0
        self._flushed_invocations = 0
        self._flushed_errors = 0
        self._provider_sequence = 0

    def flush(self) -> None:
        """Persist newly observed facts without owning final artifacts."""

        for event_type, data in self.events[self._flushed_events:]:
            self.repository.append_event(self.run_id, self.agent, event_type, data)
        self._flushed_events = len(self.events)

        for kind, data in self.invocations[self._flushed_invocations:]:
            self._provider_sequence += 1
            self.repository.append_event(self.run_id, self.agent, f'runtime_{kind}', data)
            self.repository.record_provider_event(
                run_id=self.run_id,
                agent=self.agent,
                provider=self.provider,
                runtime=self.runtime,
                event_name=kind,
                sequence=self._provider_sequence,
                attempt_id=self.attempt_id,
                session_id=self.session_id,
                invocation_id=self.invocation_id,
                payload_preview=data,
            )
        self._flushed_invocations = len(self.invocations)

        for source, message, data in self.errors[self._flushed_errors:]:
            self.repository.append_event(
                self.run_id,
                self.agent,
                'runtime_error_fact',
                {'source': source, 'message': message, **data},
            )
            self.repository.record_runtime_error(
                run_id=self.run_id,
                agent=self.agent,
                attempt_id=self.attempt_id,
                session_id=self.session_id,
                invocation_id=self.invocation_id,
                source=source,
                message=message,
                retryable=data.get('retryable') if isinstance(data.get('retryable'), bool) else None,
            )
        self._flushed_errors = len(self.errors)
