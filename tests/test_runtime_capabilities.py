"""Tests for deployed runtime capability contracts."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import sys
import types

import pytest

from spawnd.io.validation import validate_plan
from spawnd.models.specs import AgentSpec, Defaults, McpServerSpec, PlanSpec
from spawnd.runtime.agent_config import resolve_agent_plan_config
from spawnd.runtime.agent_run import AgentConfig
from spawnd.runtime.executors.claude import _external_mcp_servers
from spawnd.runtime.executors.claude import ClaudeExecutor
from spawnd.runtime.executors.openai import _openai_conversation_id, _openai_mcp_servers, OpenAIExecutor
from spawnd.runtime.executor import run_worker
from spawnd.tools.mcp import build_server
from spawnd.tools.toolset import worker_toolset


def test_reviewer_role_defaults_to_readonly_and_explicit_agent_setting_wins():
    readonly = resolve_agent_plan_config(AgentSpec(name='review', prompt='task', use_role='reviewer'), Defaults())
    assert readonly.write_allowed is False

    override = resolve_agent_plan_config(
        AgentSpec(name='review', prompt='task', use_role='reviewer', write_allowed=True),
        Defaults(),
    )
    assert override.write_allowed is True


def test_default_worker_toolset_does_not_expose_shell():
    assert worker_toolset().code == ['Read', 'Write', 'Edit', 'Glob', 'Grep']


@pytest.mark.asyncio
async def test_run_worker_builds_readonly_toolset(monkeypatch):
    captured = {}

    class FakeExecutor:
        async def run(self, config, toolset):
            captured['config'] = config
            captured['toolset'] = toolset
            return {'success': True, 'status': 'completed'}

    monkeypatch.setattr('spawnd.runtime.executor._get_executor', lambda runtime: FakeExecutor())

    await run_worker(
        AgentConfig(
            name='review',
            run_id='run-1',
            prompt='task',
            worktree='.',
            runtime='claude',
            write_allowed=False,
        )
    )

    toolset = captured['toolset']
    assert toolset.write_allowed is False
    assert toolset.code == ['Read', 'Glob', 'Grep']


def test_plan_validation_accepts_claude_openai_and_codex_mcp():
    server = McpServerSpec(name='docs', type='http', url='https://mcp.example.test', tools=['search'])
    assert validate_plan(
        PlanSpec(
            name='mcp',
            defaults=Defaults(runtime='claude', mcp_servers=[server]),
            agents=[AgentSpec(name='a', prompt='task')],
        )
    ) == []
    assert validate_plan(
        PlanSpec(
            name='mcp',
            defaults=Defaults(runtime='openai', mcp_servers=[server]),
            agents=[AgentSpec(name='a', prompt='task')],
        )
    ) == []
    assert validate_plan(
        PlanSpec(
            name='mcp',
            defaults=Defaults(runtime='codex', mcp_servers=[server]),
            agents=[AgentSpec(name='a', prompt='task')],
        )
    ) == []


def test_plan_validation_accepts_codex_manager():
    assert validate_plan(
        PlanSpec(
            name='manager',
            defaults=Defaults(runtime='codex'),
            agents=[AgentSpec(name='manager', type='manager', prompt='manage')],
        )
    ) == []


def test_plan_validation_rejects_unsupported_codex_mcp_shapes():
    errors = validate_plan(
        PlanSpec(
            name='mcp',
            defaults=Defaults(runtime='codex'),
            agents=[
                AgentSpec(
                    name='a',
                    prompt='task',
                    mcp_servers=[
                        McpServerSpec(name='spawnd', type='stdio', command='server'),
                        McpServerSpec(name='events', type='sse', url='https://mcp.example.test/sse'),
                        McpServerSpec(name='headers', type='http', url='https://mcp.example.test', headers={'X-Test': '1'}),
                        McpServerSpec(
                            name='badref',
                            type='http',
                            url='https://mcp.example.test',
                            header_refs={'X-Test': 'SPAWND_MCP_TOKEN'},
                        ),
                    ],
                )
            ],
        )
    )

    assert errors == [
        'Agent a MCP server name spawnd is reserved',
        'Agent a runtime codex does not support SSE MCP server events',
        'Agent a runtime codex MCP server headers does not support literal headers',
        'Agent a runtime codex MCP server badref only supports Authorization header_refs',
    ]


@pytest.mark.asyncio
async def test_spawnd_mcp_server_exposes_worker_tools(monkeypatch):
    monkeypatch.setenv('SPAWND_RUN_ID', 'run-1')
    monkeypatch.setenv('SPAWND_AGENT_NAME', 'worker')
    monkeypatch.setenv('SPAWND_AGENT_TYPE', 'worker')

    assert set(await build_server().get_tools()) == {
        'mark_complete',
        'request_clarification',
        'report_progress',
        'report_blocker',
    }


@pytest.mark.asyncio
async def test_spawnd_mcp_server_exposes_manager_tools(monkeypatch):
    monkeypatch.setenv('SPAWND_RUN_ID', 'run-1')
    monkeypatch.setenv('SPAWND_AGENT_NAME', 'manager')
    monkeypatch.setenv('SPAWND_AGENT_TYPE', 'manager')

    assert set(await build_server().get_tools()) == {
        'spawn_worker',
        'respond_to_clarification',
        'cancel_worker',
        'get_worker_status',
        'get_pending_clarifications',
        'mark_plan_complete',
    }


def test_claude_external_mcp_servers_resolve_secret_refs(monkeypatch, tmp_path):
    monkeypatch.setenv('SPAWND_MCP_TOKEN', 'secret-token')
    config = AgentConfig(
        name='a',
        run_id='run-1',
        prompt='task',
        worktree=tmp_path,
        runtime='claude',
        mcp_servers=[
            McpServerSpec(
                name='docs',
                type='http',
                url='https://mcp.example.test',
                header_refs={'Authorization': 'SPAWND_MCP_TOKEN'},
                tools=['search'],
            )
        ],
    )

    servers, allowed = _external_mcp_servers(config)

    assert servers == {
        'docs': {
            'type': 'http',
            'url': 'https://mcp.example.test',
            'headers': {'Authorization': 'secret-token'},
        }
    }
    assert allowed == ['mcp__docs__search']


@pytest.mark.asyncio
async def test_openai_mcp_servers_resolve_secret_refs(monkeypatch, tmp_path):
    entered = []

    class FakeServer:
        transport = ''

        def __init__(self, *, name, params):
            self.name = name
            self.params = params

        async def __aenter__(self):
            entered.append(self)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeHttpServer(FakeServer):
        transport = 'http'

    class FakeStdioServer(FakeServer):
        transport = 'stdio'

    class FakeSseServer(FakeServer):
        transport = 'sse'

    fake_mcp = types.ModuleType('agents.mcp')
    fake_mcp.MCPServerStreamableHttp = FakeHttpServer
    fake_mcp.MCPServerStdio = FakeStdioServer
    fake_mcp.MCPServerSse = FakeSseServer
    monkeypatch.setitem(sys.modules, 'agents.mcp', fake_mcp)
    monkeypatch.setenv('SPAWND_MCP_TOKEN', 'secret-token')
    config = AgentConfig(
        name='a',
        run_id='run-1',
        prompt='task',
        worktree=tmp_path,
        runtime='openai',
        mcp_servers=[
            McpServerSpec(
                name='docs',
                type='http',
                url='https://mcp.example.test/mcp',
                header_refs={'Authorization': 'SPAWND_MCP_TOKEN'},
            )
        ],
    )

    async with AsyncExitStack() as stack:
        servers = await _openai_mcp_servers(config, stack)

    assert servers == entered
    assert entered[0].name == 'docs'
    assert entered[0].params == {
        'url': 'https://mcp.example.test/mcp',
        'headers': {'Authorization': 'secret-token'},
    }


@pytest.mark.asyncio
async def test_openai_conversation_id_reuses_existing_or_creates(monkeypatch):
    assert await _openai_conversation_id('conv-existing') == 'conv-existing'

    class FakeConversations:
        async def create(self):
            return types.SimpleNamespace(id='conv-new')

    class FakeOpenAI:
        def __init__(self):
            self.conversations = FakeConversations()

    fake_openai = types.ModuleType('openai')
    fake_openai.AsyncOpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, 'openai', fake_openai)

    assert await _openai_conversation_id(None) == 'conv-new'


@pytest.mark.asyncio
async def test_claude_executor_interrupts_client_on_cancellation(monkeypatch, tmp_path):
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAssistantMessage:
        pass

    class FakeResultMessage:
        pass

    class FakeTextBlock:
        pass

    class FakeClient:
        def __init__(self, options):
            self.options = options
            self.interrupted = False
            captured['client'] = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def query(self, prompt):
            captured['prompt'] = prompt

        async def receive_response(self):
            await asyncio.sleep(60)
            yield FakeResultMessage()

        async def interrupt(self):
            self.interrupted = True

    fake_sdk = types.ModuleType('claude_agent_sdk')
    fake_sdk.AssistantMessage = FakeAssistantMessage
    fake_sdk.ClaudeAgentOptions = FakeOptions
    fake_sdk.ClaudeSDKClient = FakeClient
    fake_sdk.ResultMessage = FakeResultMessage
    fake_sdk.TextBlock = FakeTextBlock
    fake_sdk.create_sdk_mcp_server = lambda *args: {'server': args}
    monkeypatch.setitem(sys.modules, 'claude_agent_sdk', fake_sdk)

    task = asyncio.create_task(
        ClaudeExecutor().run(
            AgentConfig(name='a', run_id='run-1', prompt='task', worktree=tmp_path, runtime='claude'),
            worker_toolset(),
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured['client'].interrupted is True


@pytest.mark.asyncio
async def test_openai_executor_cancels_streaming_result_on_cancellation(monkeypatch, tmp_path):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured['agent_kwargs'] = kwargs

    class FakeStreamingResult:
        raw_responses = []
        final_output = None

        def __init__(self):
            self.cancelled = False
            captured['result'] = self

        async def stream_events(self):
            await asyncio.sleep(60)
            yield object()

        def cancel(self, mode='immediate'):
            self.cancelled = mode

    class FakeRunner:
        @staticmethod
        def run_streamed(agent, prompt, *, max_turns, conversation_id):
            captured['run'] = {
                'agent': agent,
                'prompt': prompt,
                'max_turns': max_turns,
                'conversation_id': conversation_id,
            }
            return FakeStreamingResult()

    fake_agents = types.ModuleType('agents')
    fake_agents.Agent = FakeAgent
    fake_agents.Runner = FakeRunner
    monkeypatch.setitem(sys.modules, 'agents', fake_agents)

    task = asyncio.create_task(
        OpenAIExecutor().run(
            AgentConfig(
                name='a',
                run_id='run-1',
                prompt='task',
                worktree=tmp_path,
                runtime='openai',
                resume_session_id='conv-1',
            ),
            worker_toolset(),
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured['run']['conversation_id'] == 'conv-1'
    assert captured['result'].cancelled == 'immediate'
