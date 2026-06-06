"""Tests for deployed backend config and redaction."""
from spawnd.config import load_backend_config, resolve_telemetry_config
from spawnd.artifacts.redaction import canonical_json_hash, redact_attributes, redact_env, redact_freeform_text, text_metadata
from spawnd.models.specs import TelemetryConfig


def test_backend_config_reads_deployed_environment(monkeypatch):
    monkeypatch.setenv('SPAWND_DATABASE_URL', 'postgresql+psycopg://db/spawnd')
    monkeypatch.setenv('SPAWND_REDIS_URL', 'redis://localhost:6379/0')
    monkeypatch.setenv('SPAWND_ARTIFACTS_BUCKET', 'spawnd-artifacts')
    monkeypatch.setenv('SPAWND_ARTIFACTS_PREFIX', 'prod')
    config = load_backend_config()
    assert config.deployed is True
    assert config.database_url == 'postgresql+psycopg://db/spawnd'
    assert config.redis_url == 'redis://localhost:6379/0'
    assert config.artifacts.configured is True
    assert config.artifacts.prefix == 'prod'


def test_telemetry_env_overrides_plan(monkeypatch):
    plan = TelemetryConfig(enabled=False, exporter='none', capture='orchestrator', failure_policy='degrade')
    monkeypatch.setenv('SPAWND_TELEMETRY_ENABLED', '1')
    monkeypatch.setenv('SPAWND_TELEMETRY_EXPORTER', 'otlp')
    monkeypatch.setenv('SPAWND_TELEMETRY_CAPTURE', 'full')
    monkeypatch.setenv('SPAWND_TELEMETRY_FAILURE_POLICY', 'fail')
    resolved = resolve_telemetry_config(plan)
    assert resolved.enabled is True
    assert resolved.exporter == 'otlp'
    assert resolved.capture == 'full'
    assert resolved.failure_policy == 'fail'


def test_redaction_omits_sensitive_env_values():
    env = {'PATH': '/bin', 'OPENAI_API_KEY': 'secret', 'DATABASE_URL': 'postgres://secret'}
    redacted = redact_env(env)
    assert redacted['count'] == 3
    assert redacted['keys'] == ['PATH']
    assert redacted['sensitive_key_hashes']
    assert 'secret' not in str(redacted)


def test_redaction_summarizes_text_and_attributes():
    text = 'OPENAI_API_KEY=secret-value\nnormal line'
    redacted = redact_freeform_text(text)
    assert 'secret-value' not in redacted
    assert 'OPENAI_API_KEY=<redacted>' in redacted
    meta = text_metadata(text)
    assert meta['bytes'] == len(text.encode('utf-8'))
    attrs = redact_attributes({'password': 'secret', 'safe': 'value'})
    assert attrs == {'password': '<redacted>', 'safe': 'value'}


def test_freeform_redaction_omits_deployed_connection_strings():
    text = "\n".join(
        [
            "SPAWND_DATABASE_URL=postgresql+psycopg://user:secret@db/spawnd",
            "SPAWND_REDIS_URL='redis://:secret@redis:6379/0'",
            "PUBLIC_FLAG=true",
        ]
    )

    redacted = redact_freeform_text(text)

    assert "postgresql+psycopg" not in redacted
    assert "redis://:secret" not in redacted
    assert "SPAWND_DATABASE_URL=<redacted>" in redacted
    assert "SPAWND_REDIS_URL=<redacted>" in redacted
    assert "PUBLIC_FLAG=true" in redacted


def test_canonical_json_hash_is_order_insensitive():
    assert canonical_json_hash({'b': 2, 'a': 1}) == canonical_json_hash({'a': 1, 'b': 2})
