"""SQLAlchemy Core schema for deployed spawnd state."""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import JSON

metadata = MetaData()

json_type = JSON().with_variant(postgresql.JSONB(none_as_null=True), 'postgresql')

projects = Table(
    'projects',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('slug', String(240), nullable=False, unique=True),
    Column('source_repo', Text),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runs = Table(
    'runs',
    metadata,
    Column('run_id', String(160), primary_key=True),
    Column('project_id', String(64), ForeignKey('projects.id')),
    Column('tenant_id', String(160)),
    Column('idempotency_key', String(160)),
    Column('submitted_by', Text),
    Column('submitted_via', String(80)),
    Column('name', String(240), nullable=False),
    Column('spec', json_type, nullable=False),
    Column('spec_hash', String(64)),
    Column('spec_artifact_id', String(64)),
    Column('status', String(40), nullable=False, server_default='queued'),
    Column('total_cost_usd', Float, nullable=False, server_default='0'),
    Column('max_cost_usd', Float, nullable=False, server_default='25'),
    Column('source_repo', Text),
    Column('source_ref', Text),
    Column('cancelled_at', DateTime(timezone=True)),
    Column('finished_at', DateTime(timezone=True)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
)

agents = Table(
    'agents',
    metadata,
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), primary_key=True),
    Column('name', String(160), primary_key=True),
    Column('plan_name', String(240)),
    Column('status', String(40), nullable=False, server_default='pending'),
    Column('type', String(40), nullable=False, server_default='worker'),
    Column('runtime', String(40), nullable=False),
    Column('model', String(120)),
    Column('prompt_hash', String(64), nullable=False),
    Column('prompt_preview', Text),
    Column('check_command_hash', String(64)),
    Column('check_command_preview', Text),
    Column('branch', Text),
    Column('worktree_locator', Text),
    Column('lease_token', String(120)),
    Column('leased_until', DateTime(timezone=True)),
    Column('worker_id', String(160)),
    Column('heartbeat_at', DateTime(timezone=True)),
    Column('input_tokens', Integer, nullable=False, server_default='0'),
    Column('output_tokens', Integer, nullable=False, server_default='0'),
    Column('cost_usd', Float, nullable=False, server_default='0'),
    Column('cost_source', String(40), nullable=False, server_default='unknown'),
    Column('max_cost_usd', Float, nullable=False, server_default='5'),
    Column('error', Text),
    Column('depends_on', json_type, nullable=False),
    Column('on_failure', String(40), nullable=False, server_default='continue'),
    Column('retry_count', Integer, nullable=False, server_default='3'),
    Column('retry_attempt', Integer, nullable=False, server_default='0'),
    Column('last_error', Text),
    Column('env_metadata', json_type, nullable=False),
    Column('max_subagents', Integer),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
)

events = Table(
    'events',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160), nullable=False),
    Column('event_type', String(80), nullable=False),
    Column('data', json_type, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

responses = Table(
    'responses',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('clarification_id', String(64), ForeignKey('events.id', ondelete='CASCADE'), nullable=False),
    Column('response', Text, nullable=False),
    Column('consumed', Boolean, nullable=False, server_default='false'),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

trace_spans = Table(
    'trace_spans',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('trace_id', String(64), nullable=False),
    Column('span_id', String(32), nullable=False),
    Column('parent_span_id', String(32)),
    Column('name', String(240), nullable=False),
    Column('status', String(40), nullable=False),
    Column('started_at', DateTime(timezone=True), nullable=False),
    Column('ended_at', DateTime(timezone=True), nullable=False),
    Column('duration_ms', Integer, nullable=False),
    Column('attributes', json_type, nullable=False),
    Column('events', json_type, nullable=False),
    Column('export_status', String(40), nullable=False, server_default='pending'),
    UniqueConstraint('trace_id', 'span_id', name='uq_trace_spans_trace_span'),
)

artifacts = Table(
    'artifacts',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='SET NULL')),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='SET NULL')),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='SET NULL')),
    Column('message_id', String(64)),
    Column('tool_call_id', String(64)),
    Column('kind', String(80), nullable=False),
    Column('uri', Text, nullable=False),
    Column('storage_backend', String(80)),
    Column('bucket', Text),
    Column('object_key', Text),
    Column('version_id', Text),
    Column('sha256', String(64), nullable=False),
    Column('size_bytes', Integer, nullable=False),
    Column('redaction_policy', String(80), nullable=False),
    Column('content_type', String(120), nullable=False),
    Column('raw_capture', Boolean, nullable=False, server_default='false'),
    Column('encryption_key_ref', Text),
    Column('retention_class', String(80)),
    Column('expires_at', DateTime(timezone=True)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint('uri', name='uq_artifacts_uri'),
)

checks = Table(
    'checks',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160), nullable=False),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='SET NULL')),
    Column('runtime_invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='SET NULL')),
    Column('command_hash', String(64), nullable=False),
    Column('command_preview', Text),
    Column('shell', Text),
    Column('cwd_locator', Text),
    Column('env_metadata', json_type),
    Column('exit_code', Integer, nullable=False),
    Column('signal', String(80)),
    Column('duration_ms', Integer, nullable=False),
    Column('output_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('stdout_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('stderr_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('started_at', DateTime(timezone=True)),
    Column('completed_at', DateTime(timezone=True)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

git_provenance = Table(
    'git_provenance',
    metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='SET NULL')),
    Column('base_ref', Text),
    Column('remote', Text),
    Column('worktree_locator', Text),
    Column('base_sha', String(64)),
    Column('merge_base_sha', String(64)),
    Column('head_sha', String(64)),
    Column('branch', Text),
    Column('commit_sha', String(64)),
    Column('pr_url', Text),
    Column('pr_number', Integer),
    Column('patch_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('commit_message_hash', String(64)),
    Column('commit_message_preview', Text),
    Column('changed_files_count', Integer),
    Column('insertions_count', Integer),
    Column('deletions_count', Integer),
    Column('diff_stats', json_type, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

agent_attempts = Table(
    'agent_attempts',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160), nullable=False),
    Column('attempt_number', Integer, nullable=False),
    Column('runtime', String(40), nullable=False),
    Column('model', String(120)),
    Column('status', String(40), nullable=False, server_default='claimed'),
    Column('worker_id', String(160)),
    Column('lease_token', String(120)),
    Column('leased_until', DateTime(timezone=True)),
    Column('heartbeat_at', DateTime(timezone=True)),
    Column('started_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('finished_at', DateTime(timezone=True)),
    Column('error_id', String(64)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('updated_at', DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()),
    UniqueConstraint('run_id', 'agent', 'attempt_number', name='uq_agent_attempts_run_agent_attempt'),
)

runtime_sessions = Table(
    'runtime_sessions',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='CASCADE'), nullable=False),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160), nullable=False),
    Column('provider', String(40), nullable=False),
    Column('runtime', String(40), nullable=False),
    Column('provider_session_id', Text),
    Column('provider_thread_id', Text),
    Column('cwd_locator', Text),
    Column('model', String(120)),
    Column('model_provider', String(80)),
    Column('sdk_package', String(120)),
    Column('sdk_version', String(80)),
    Column('cli_version', String(120)),
    Column('auth_mode', String(80)),
    Column('ephemeral', Boolean),
    Column('permission_mode', String(80)),
    Column('sandbox_policy', String(120)),
    Column('metadata', json_type, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runtime_options = Table(
    'runtime_options',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE'), nullable=False),
    Column('option_name', Text, nullable=False),
    Column('option_kind', String(80), nullable=False),
    Column('value_hash', String(64)),
    Column('value_preview', Text),
    Column('value_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('source', String(80), nullable=False),
    UniqueConstraint('session_id', 'option_name', 'source', name='uq_runtime_options_session_option_source'),
)

runtime_invocations = Table(
    'runtime_invocations',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE')),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='CASCADE'), nullable=False),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160), nullable=False),
    Column('provider_turn_id', Text),
    Column('provider_request_id', Text),
    Column('sequence', Integer, nullable=False),
    Column('kind', String(40), nullable=False),
    Column('status', String(40), nullable=False),
    Column('started_at', DateTime(timezone=True), nullable=False),
    Column('completed_at', DateTime(timezone=True)),
    Column('duration_ms', Integer),
    Column('api_duration_ms', Integer),
    Column('exit_code', Integer),
    Column('signal', String(80)),
    Column('stop_reason', Text),
    Column('is_error', Boolean, nullable=False, server_default='false'),
    Column('error_id', String(64)),
    Column(
        'final_message_artifact_id',
        String(64),
        ForeignKey('artifacts.id', use_alter=True, name='fk_runtime_invocations_final_message_artifact'),
    ),
    Column('final_message_hash', String(64)),
    UniqueConstraint('session_id', 'sequence', name='uq_runtime_invocations_session_sequence'),
)

runtime_messages = Table(
    'runtime_messages',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('provider_message_id', Text),
    Column('provider_uuid', Text),
    Column('session_provider_id', Text),
    Column('role', String(40), nullable=False),
    Column('subtype', String(80)),
    Column('sequence', Integer, nullable=False),
    Column('parent_tool_call_id', String(64)),
    Column('model', String(120)),
    Column('status', String(40)),
    Column('stop_reason', Text),
    Column('error_id', String(64), ForeignKey('runtime_errors.id', ondelete='SET NULL')),
    Column('content_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('content_hash', String(64)),
    Column('content_preview', Text),
    UniqueConstraint('invocation_id', 'sequence', name='uq_runtime_messages_invocation_sequence'),
)

runtime_content_blocks = Table(
    'runtime_content_blocks',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('message_id', String(64), ForeignKey('runtime_messages.id', ondelete='CASCADE'), nullable=False),
    Column('sequence', Integer, nullable=False),
    Column('block_type', String(80), nullable=False),
    Column('provider_block_id', Text),
    Column('tool_call_id', String(64)),
    Column('content_hash', String(64)),
    Column('content_preview', Text),
    Column('content_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('metadata', json_type, nullable=False),
)

runtime_items = Table(
    'runtime_items',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('provider_item_id', Text),
    Column('provider_thread_id', Text),
    Column('provider_turn_id', Text),
    Column('item_type', String(120), nullable=False),
    Column('phase', String(80)),
    Column('started_at', DateTime(timezone=True)),
    Column('completed_at', DateTime(timezone=True)),
    Column('content_hash', String(64)),
    Column('content_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('metadata', json_type, nullable=False),
    UniqueConstraint('invocation_id', 'provider_item_id', name='uq_runtime_items_invocation_provider_item'),
)

runtime_tool_calls = Table(
    'runtime_tool_calls',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('message_id', String(64), ForeignKey('runtime_messages.id', ondelete='SET NULL')),
    Column('provider_tool_use_id', Text),
    Column('tool_origin', String(80), nullable=False),
    Column('tool_name', Text, nullable=False),
    Column('mcp_server', Text),
    Column('input_hash', String(64)),
    Column('input_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('permission_id', String(64)),
    Column('started_at', DateTime(timezone=True)),
    Column('completed_at', DateTime(timezone=True)),
    Column('status', String(40)),
    Column('error_id', String(64), ForeignKey('runtime_errors.id', ondelete='SET NULL')),
)

runtime_tool_results = Table(
    'runtime_tool_results',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('tool_call_id', String(64), ForeignKey('runtime_tool_calls.id', ondelete='CASCADE'), nullable=False),
    Column('provider_tool_use_id', Text),
    Column('is_error', Boolean),
    Column('output_hash', String(64)),
    Column('output_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('summary_preview', Text),
    Column('metadata', json_type, nullable=False),
)

runtime_plans = Table(
    'runtime_plans',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('provider_item_id', Text),
    Column('sequence', Integer, nullable=False),
    Column('step', Text),
    Column('status', String(40)),
    Column('explanation_hash', String(64)),
    Column('explanation_preview', Text),
    Column('delta_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runtime_file_changes = Table(
    'runtime_file_changes',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('tool_call_id', String(64), ForeignKey('runtime_tool_calls.id', ondelete='SET NULL')),
    Column('path_hash', String(64), nullable=False),
    Column('path_preview', Text),
    Column('change_kind', String(80)),
    Column('diff_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('patch_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('started_at', DateTime(timezone=True)),
    Column('completed_at', DateTime(timezone=True)),
    Column('metadata', json_type, nullable=False),
)

runtime_errors = Table(
    'runtime_errors',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='SET NULL')),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='SET NULL')),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='SET NULL')),
    Column('source', String(80), nullable=False),
    Column('code', String(120)),
    Column('message_hash', String(64), nullable=False),
    Column('message_preview', Text),
    Column('details_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('retryable', Boolean),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

token_usage = Table(
    'token_usage',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='CASCADE')),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE')),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE')),
    Column('message_id', String(64), ForeignKey('runtime_messages.id', ondelete='CASCADE')),
    Column('provider', String(40), nullable=False),
    Column('model', String(120)),
    Column('scope', String(40), nullable=False),
    Column('input_tokens', Integer, nullable=False, server_default='0'),
    Column('cached_input_tokens', Integer, nullable=False, server_default='0'),
    Column('output_tokens', Integer, nullable=False, server_default='0'),
    Column('reasoning_output_tokens', Integer, nullable=False, server_default='0'),
    Column('total_tokens', Integer, nullable=False, server_default='0'),
    Column('context_window', Integer),
    Column('raw_usage', json_type, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

cost_usage = Table(
    'cost_usage',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='CASCADE')),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE')),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE')),
    Column('provider', String(40), nullable=False),
    Column('model', String(120)),
    Column('amount_usd', Float, nullable=False, server_default='0'),
    Column('source', String(40), nullable=False, server_default='unknown'),
    Column('raw_cost', json_type, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

context_usage_snapshots = Table(
    'context_usage_snapshots',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE'), nullable=False),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE')),
    Column('total_tokens', Integer),
    Column('max_tokens', Integer),
    Column('raw_max_tokens', Integer),
    Column('percentage', Float),
    Column('model', String(120)),
    Column('is_auto_compact_enabled', Boolean),
    Column('categories', json_type, nullable=False),
    Column('memory_files', json_type, nullable=False),
    Column('mcp_tools', json_type, nullable=False),
    Column('agents', json_type, nullable=False),
    Column('api_usage', json_type),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runtime_permissions = Table(
    'runtime_permissions',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('tool_call_id', String(64), ForeignKey('runtime_tool_calls.id', ondelete='SET NULL')),
    Column('provider_tool_use_id', Text),
    Column('source', String(80), nullable=False),
    Column('permission_mode', String(80)),
    Column('decision', String(40), nullable=False),
    Column('reason_hash', String(64)),
    Column('reason_preview', Text),
    Column('updated_input_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runtime_hooks = Table(
    'runtime_hooks',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('provider_hook_event', Text, nullable=False),
    Column('phase', String(80), nullable=False),
    Column('tool_call_id', String(64), ForeignKey('runtime_tool_calls.id', ondelete='SET NULL')),
    Column('provider_tool_use_id', Text),
    Column('agent_provider_id', Text),
    Column('input_hash', String(64)),
    Column('input_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('output_hash', String(64)),
    Column('output_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('duration_ms', Integer),
    Column('status', String(40)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
)

runtime_mcp_servers = Table(
    'runtime_mcp_servers',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE'), nullable=False),
    Column('name', Text, nullable=False),
    Column('status', String(40), nullable=False),
    Column('server_name', Text),
    Column('server_version', Text),
    Column('scope', String(80)),
    Column('config_hash', String(64)),
    Column('config_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('error_id', String(64), ForeignKey('runtime_errors.id', ondelete='SET NULL')),
    UniqueConstraint('session_id', 'name', name='uq_runtime_mcp_servers_session_name'),
)

runtime_mcp_tools = Table(
    'runtime_mcp_tools',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('mcp_server_id', String(64), ForeignKey('runtime_mcp_servers.id', ondelete='CASCADE'), nullable=False),
    Column('name', Text, nullable=False),
    Column('description_hash', String(64)),
    Column('annotations', json_type, nullable=False),
    UniqueConstraint('mcp_server_id', 'name', name='uq_runtime_mcp_tools_server_name'),
)

runtime_subtasks = Table(
    'runtime_subtasks',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE'), nullable=False),
    Column('provider_task_id', Text, nullable=False),
    Column('provider_agent_id', Text),
    Column('task_type', String(80)),
    Column('description_hash', String(64)),
    Column('description_preview', Text),
    Column('status', String(40)),
    Column('output_artifact_id', String(64), ForeignKey('artifacts.id')),
    Column('summary_hash', String(64)),
    Column('summary_preview', Text),
    Column('started_at', DateTime(timezone=True)),
    Column('completed_at', DateTime(timezone=True)),
    UniqueConstraint('invocation_id', 'provider_task_id', name='uq_runtime_subtasks_invocation_task'),
)

provider_events = Table(
    'provider_events',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('attempt_id', String(64), ForeignKey('agent_attempts.id', ondelete='CASCADE')),
    Column('session_id', String(64), ForeignKey('runtime_sessions.id', ondelete='CASCADE')),
    Column('invocation_id', String(64), ForeignKey('runtime_invocations.id', ondelete='CASCADE')),
    Column('provider', String(40), nullable=False),
    Column('runtime', String(40), nullable=False),
    Column('event_name', Text, nullable=False),
    Column('provider_event_id', Text),
    Column('provider_thread_id', Text),
    Column('provider_turn_id', Text),
    Column('provider_message_id', Text),
    Column('sequence', BigInteger, nullable=False),
    Column('occurred_at', DateTime(timezone=True)),
    Column('received_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('payload_schema', Text),
    Column('payload_version', Text),
    Column('payload_hash', String(64)),
    Column('payload_preview', json_type, nullable=False),
    Column('payload_artifact_id', String(64), ForeignKey('artifacts.id')),
    UniqueConstraint('session_id', 'sequence', name='uq_provider_events_session_sequence'),
)

worker_nodes = Table(
    'worker_nodes',
    metadata,
    Column('worker_id', String(160), primary_key=True),
    Column('hostname', Text),
    Column('version', String(80)),
    Column('started_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('heartbeat_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('capacity', json_type, nullable=False),
    Column('status', String(40), nullable=False, server_default='active'),
)

queue_outbox = Table(
    'queue_outbox',
    metadata,
    Column('id', String(64), primary_key=True),
    Column('run_id', String(160), ForeignKey('runs.run_id', ondelete='CASCADE'), nullable=False),
    Column('agent', String(160)),
    Column('event_type', String(80), nullable=False),
    Column('payload', json_type, nullable=False),
    Column('status', String(40), nullable=False, server_default='pending'),
    Column('attempts', Integer, nullable=False, server_default='0'),
    Column('next_attempt_at', DateTime(timezone=True)),
    Column('created_at', DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column('published_at', DateTime(timezone=True)),
)

Index('idx_agents_run_status', agents.c.run_id, agents.c.status)
Index('idx_agents_worker_lease', agents.c.worker_id, agents.c.leased_until)
Index('idx_events_run_agent', events.c.run_id, events.c.agent)
Index('idx_events_run_type', events.c.run_id, events.c.event_type)
Index('idx_trace_spans_run_agent', trace_spans.c.run_id, trace_spans.c.agent)
Index('idx_artifacts_run_agent_kind', artifacts.c.run_id, artifacts.c.agent, artifacts.c.kind)
Index('idx_agent_attempts_run_agent', agent_attempts.c.run_id, agent_attempts.c.agent)
Index('idx_runtime_sessions_attempt', runtime_sessions.c.attempt_id)
Index('idx_runtime_sessions_provider_session', runtime_sessions.c.provider, runtime_sessions.c.provider_session_id)
Index('idx_runtime_sessions_provider_thread', runtime_sessions.c.provider, runtime_sessions.c.provider_thread_id)
Index('idx_runtime_options_session', runtime_options.c.session_id)
Index('idx_runtime_invocations_attempt', runtime_invocations.c.attempt_id)
Index('idx_runtime_invocations_run_agent', runtime_invocations.c.run_id, runtime_invocations.c.agent)
Index('idx_runtime_invocations_provider_turn', runtime_invocations.c.provider_turn_id)
Index('idx_runtime_messages_invocation', runtime_messages.c.invocation_id, runtime_messages.c.sequence)
Index('idx_runtime_items_invocation', runtime_items.c.invocation_id)
Index('idx_runtime_tool_calls_invocation_tool', runtime_tool_calls.c.invocation_id, runtime_tool_calls.c.tool_name, runtime_tool_calls.c.status)
Index('idx_runtime_tool_results_call', runtime_tool_results.c.tool_call_id)
Index('idx_runtime_plans_invocation', runtime_plans.c.invocation_id, runtime_plans.c.sequence)
Index('idx_runtime_file_changes_invocation', runtime_file_changes.c.invocation_id)
Index('idx_runtime_errors_run', runtime_errors.c.run_id, runtime_errors.c.created_at)
Index('idx_token_usage_run_agent', token_usage.c.run_id, token_usage.c.agent, token_usage.c.created_at)
Index('idx_cost_usage_run_agent', cost_usage.c.run_id, cost_usage.c.agent, cost_usage.c.created_at)
Index('idx_context_usage_session', context_usage_snapshots.c.session_id, context_usage_snapshots.c.created_at)
Index('idx_runtime_permissions_invocation', runtime_permissions.c.invocation_id)
Index('idx_runtime_hooks_invocation', runtime_hooks.c.invocation_id)
Index('idx_runtime_mcp_tools_server', runtime_mcp_tools.c.mcp_server_id)
Index('idx_runtime_subtasks_invocation', runtime_subtasks.c.invocation_id)
Index('idx_provider_events_session', provider_events.c.session_id, provider_events.c.sequence)
Index('idx_provider_events_run_received', provider_events.c.run_id, provider_events.c.received_at)
Index('idx_provider_events_provider_name_received', provider_events.c.provider, provider_events.c.event_name, provider_events.c.received_at)
Index('idx_queue_outbox_status', queue_outbox.c.status, queue_outbox.c.next_attempt_at)
