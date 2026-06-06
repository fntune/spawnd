"""Runtime configuration for deployed spawnd backends."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from spawnd.models.specs import TelemetryConfig

TelemetryExporter = Literal['none', 'otlp']
TelemetryCapture = Literal['orchestrator', 'full']
TelemetryFailurePolicy = Literal['degrade', 'fail']


@dataclass(frozen=True)
class ArtifactStorageConfig:
    """S3 API artifact storage settings."""

    bucket: str | None
    endpoint: str | None
    region: str | None
    prefix: str

    @property
    def configured(self) -> bool:
        return bool(self.bucket)


@dataclass(frozen=True)
class ResolvedTelemetryConfig:
    """Effective telemetry config after applying environment overrides."""

    enabled: bool
    exporter: TelemetryExporter
    capture: TelemetryCapture
    failure_policy: TelemetryFailurePolicy


@dataclass(frozen=True)
class BackendConfig:
    """Connection and observability config for deployed mode."""

    database_url: str | None
    redis_url: str | None
    artifacts: ArtifactStorageConfig
    telemetry: ResolvedTelemetryConfig

    @property
    def deployed(self) -> bool:
        return bool(self.database_url)


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {'1', 'true', 'yes', 'on'}


def _literal_env(name: str, default: str, allowed: set[str]) -> str:
    value = os.environ.get(name, default)
    if value not in allowed:
        raise ValueError(f'{name} must be one of: {", ".join(sorted(allowed))}')
    return value


def resolve_telemetry_config(plan: TelemetryConfig | None = None) -> ResolvedTelemetryConfig:
    """Resolve telemetry settings from plan config plus env overrides."""

    base = plan or TelemetryConfig()
    enabled = _bool_env('SPAWND_TELEMETRY_ENABLED', base.enabled)
    exporter = _literal_env(
        'SPAWND_TELEMETRY_EXPORTER',
        base.exporter,
        {'none', 'otlp'},
    )
    capture = _literal_env(
        'SPAWND_TELEMETRY_CAPTURE',
        base.capture,
        {'orchestrator', 'full'},
    )
    failure_policy = _literal_env(
        'SPAWND_TELEMETRY_FAILURE_POLICY',
        base.failure_policy,
        {'degrade', 'fail'},
    )
    return ResolvedTelemetryConfig(
        enabled=enabled,
        exporter=exporter,  # type: ignore[arg-type]
        capture=capture,  # type: ignore[arg-type]
        failure_policy=failure_policy,  # type: ignore[arg-type]
    )


def load_backend_config(plan_telemetry: TelemetryConfig | None = None) -> BackendConfig:
    """Load deployed backend config from environment variables."""

    return BackendConfig(
        database_url=os.environ.get('SPAWND_DATABASE_URL'),
        redis_url=os.environ.get('SPAWND_REDIS_URL'),
        artifacts=ArtifactStorageConfig(
            bucket=os.environ.get('SPAWND_ARTIFACTS_BUCKET'),
            endpoint=os.environ.get('SPAWND_ARTIFACTS_ENDPOINT'),
            region=os.environ.get('SPAWND_ARTIFACTS_REGION'),
            prefix=os.environ.get('SPAWND_ARTIFACTS_PREFIX', '').strip('/'),
        ),
        telemetry=resolve_telemetry_config(plan_telemetry),
    )
