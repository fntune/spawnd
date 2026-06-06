"""Tests for deployed telemetry mirroring."""
from spawnd.config import ResolvedTelemetryConfig
from spawnd.observability.telemetry import TelemetryRecorder
from spawnd.models.specs import AgentSpec, PlanSpec
from tests.deployed_helpers import make_repo


def test_telemetry_recorder_mirrors_redacted_span():
    repo = make_repo()
    repo.create_run(PlanSpec(name='deploy', agents=[AgentSpec(name='a', prompt='task')]), 'run-1')
    recorder = TelemetryRecorder(
        ResolvedTelemetryConfig(enabled=False, exporter='none', capture='full', failure_policy='degrade'),
        repository=repo,
    )
    with recorder.span('spawnd.agent', run_id='run-1', agent='a', attributes={'api_key': 'secret', 'safe': 'value'}) as span:
        span.add_event('step', {'token': 'secret', 'ok': True})
    spans = repo.fetch_trace_spans('run-1')
    assert len(spans) == 1
    assert spans[0]['attributes']['api_key'] == '<redacted>'
    assert spans[0]['events'][0]['attributes']['token'] == '<redacted>'
