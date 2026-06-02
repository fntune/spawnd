"""Tests for the Codex CLI executor."""
from __future__ import annotations
import pytest
from spawnd.runtime.executor import AgentConfig
from spawnd.runtime.executors.base import get_executor
from spawnd.runtime.executors.codex import CodexExecutor
from spawnd.storage.db import get_agent, init_db, insert_agent, insert_plan
from spawnd.tools.toolset import manager_toolset, worker_toolset

from tests.helpers import require_row

def _fake_codex(tmp_path):
    script = tmp_path / 'codex'
    _ = script.write_text('#!/usr/bin/env bash\nset -eu\nprintf \'%s\\n\' "$@" > "$PWD/codex-args.txt"\nlast_message=""\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "--output-last-message" ]; then\n    shift\n    last_message="$1"\n  fi\n  shift || true\ndone\nprintf \'codex stdout\\n\'\nprintf \'agent=%s tree=%s flag=%s\\n\' "$SPAWND_AGENT_NAME" "$SPAWND_TREE_PATH" "${MY_FLAG:-}" > "$PWD/codex-env.txt"\nif [ -n "$last_message" ]; then\n  printf \'final from codex\\n\' > "$last_message"\nfi\n')
    _ = script.chmod(493)
    return script

def _insert_codex_agent(run_id: str, worktree, *, agent_type: str='worker'):
    db = init_db(run_id)
    _ = insert_plan(db, run_id, 'test', 'name: test', 25.0)
    _ = insert_agent(db, run_id, 'worker', 'task', agent_type=agent_type, check_command='true', runtime='codex', cost_source='codex-cli', env={'MY_FLAG': 'set'})
    _ = db.execute('UPDATE agents SET worktree = ? WHERE run_id = ? AND name = ?', (str(worktree), run_id, 'worker'))
    _ = db.commit()
    _ = db.close()

def test_codex_executor_registers():
    ex = get_executor('codex')
    assert isinstance(ex, CodexExecutor)
    assert ex.runtime == 'codex'

@pytest.mark.asyncio
async def test_codex_executor_runs_exec_and_check(tmp_path, monkeypatch):
    _ = monkeypatch.chdir(tmp_path)
    _ = (tmp_path / '.spawnd' / 'runs').mkdir(parents=True)
    worktree = tmp_path / 'worktree'
    _ = worktree.mkdir()
    fake_codex = _fake_codex(tmp_path)
    run_id = 'codex-run'
    _ = _insert_codex_agent(run_id, worktree)
    executor = CodexExecutor()
    result = await executor.run(AgentConfig(name='worker', run_id=run_id, prompt='make a small improvement', worktree=worktree, check_command='printf check-ok > check.txt', model='sonnet', runtime='codex', env={'SPAWND_CODEX_BIN': str(fake_codex), 'MY_FLAG': 'set'}), worker_toolset(system_prompt='sys'))
    assert result['success'] is True
    assert (worktree / 'check.txt').read_text() == 'check-ok'
    assert (worktree / 'codex-env.txt').read_text() == 'agent=worker tree=worker flag=set\n'
    args = (worktree / 'codex-args.txt').read_text().splitlines()
    assert args[:2] == ['exec', '--cd']
    assert str(worktree) in args
    assert '--model' in args
    assert args[args.index('--model') + 1] == 'gpt-5'
    assert '--output-last-message' in args
    assert '--ephemeral' in args
    assert args[args.index('--sandbox') + 1] == 'workspace-write'
    assert args[-1] == 'make a small improvement'
    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, 'worker'))
    _ = db.close()
    assert agent['status'] == 'completed'
    assert agent['iteration'] == 1
    log = (tmp_path / '.spawnd' / 'runs' / run_id / 'logs' / 'worker.log').read_text()
    assert 'codex stdout' in log
    assert 'final from codex' in log

@pytest.mark.asyncio
async def test_codex_executor_fails_when_check_fails(tmp_path, monkeypatch):
    _ = monkeypatch.chdir(tmp_path)
    _ = (tmp_path / '.spawnd' / 'runs').mkdir(parents=True)
    worktree = tmp_path / 'worktree'
    _ = worktree.mkdir()
    fake_codex = _fake_codex(tmp_path)
    run_id = 'codex-check-fail'
    _ = _insert_codex_agent(run_id, worktree)
    executor = CodexExecutor()
    result = await executor.run(AgentConfig(name='worker', run_id=run_id, prompt='make a small improvement', worktree=worktree, check_command='printf check-failed >&2; exit 7', model='gpt-5', runtime='codex', env={'SPAWND_CODEX_BIN': str(fake_codex)}), worker_toolset(system_prompt='sys'))
    assert result['success'] is False
    assert result['status'] == 'failed'
    assert 'check-failed' in result['error']
    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, 'worker'))
    _ = db.close()
    assert agent['status'] == 'failed'
    assert 'check-failed' in agent['error']

@pytest.mark.asyncio
async def test_codex_executor_rejects_manager_toolset(tmp_path, monkeypatch):
    _ = monkeypatch.chdir(tmp_path)
    _ = (tmp_path / '.spawnd' / 'runs').mkdir(parents=True)
    worktree = tmp_path / 'worktree'
    _ = worktree.mkdir()
    run_id = 'codex-manager'
    _ = _insert_codex_agent(run_id, worktree, agent_type='manager')
    executor = CodexExecutor()
    result = await executor.run(AgentConfig(name='worker', run_id=run_id, prompt='coordinate work', worktree=worktree, check_command='true', model='gpt-5', runtime='codex'), manager_toolset(system_prompt='sys'))
    assert result['success'] is False
    assert result['status'] == 'failed'
    assert result['error'] == 'codex runtime currently supports worker agents only'
    db = init_db(run_id)
    agent = require_row(get_agent(db, run_id, 'worker'))
    _ = db.close()
    assert agent['status'] == 'failed'
    assert agent['error'] == 'codex runtime currently supports worker agents only'
