"""Codex CLI executor."""
from __future__ import annotations
import asyncio
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from spawnd.runtime.executors.base import Executor, register
from spawnd.storage.db import get_agent, insert_event, open_db, update_agent_cost, update_agent_iteration, update_agent_status
from spawnd.storage.paths import ensure_log_file
if TYPE_CHECKING:
    from spawnd.runtime.agent_run import AgentConfig
    from spawnd.tools.toolset import Toolset
logger = logging.getLogger('spawnd.executors.codex')
DEFAULT_CODEX_MODEL = 'gpt-5'

def _build_agent_env(config: AgentConfig) -> dict[str, str]:
    """Build environment variables for the Codex subprocess."""
    env = os.environ.copy()
    _ = env.update({'SPAWND_RUN_ID': config.run_id, 'SPAWND_AGENT_NAME': config.name, 'SPAWND_PARENT_AGENT': config.parent or '', 'SPAWND_TREE_PATH': config.tree_path()})
    if config.env:
        _ = env.update(config.env)
    return env

def _env_truthy(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None:
        return default
    return value.lower() in {'1', 'true', 'yes', 'on'}

def _build_codex_command(config: AgentConfig, env: dict[str, str], last_message_path: Path) -> list[str]:
    """Build the recommended non-interactive Codex CLI invocation."""
    codex_bin = env.get('SPAWND_CODEX_BIN', 'codex')
    model = config.model if config.model and config.model != 'sonnet' else DEFAULT_CODEX_MODEL
    cmd = [codex_bin, 'exec', '--cd', str(config.worktree), '--model', model, '--output-last-message', str(last_message_path)]
    if _env_truthy(env, 'SPAWND_CODEX_EPHEMERAL', True):
        _ = cmd.append('--ephemeral')
    if _env_truthy(env, 'SPAWND_CODEX_DANGEROUS_BYPASS', False):
        _ = cmd.append('--dangerously-bypass-approvals-and-sandbox')
    else:
        sandbox = env.get('SPAWND_CODEX_SANDBOX', 'workspace-write')
        if sandbox:
            _ = cmd.extend(['--sandbox', sandbox])
    extra_args = env.get('SPAWND_CODEX_EXTRA_ARGS')
    if extra_args:
        _ = cmd.extend(shlex.split(extra_args))
    _ = cmd.append(config.prompt)
    return cmd

async def _run_subprocess(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(subprocess.run, args, cwd=cwd, env=env, capture_output=True, text=True)

async def _run_check(config: AgentConfig, env: dict[str, str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(subprocess.run, config.check_command, shell=True, cwd=config.worktree, env=env, capture_output=True, text=True)

class CodexExecutor(Executor):
    """Drive a worker via `codex exec`."""
    runtime = 'codex'

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        db = open_db(config.run_id)
        is_manager = 'mark_plan_complete' in toolset.coord
        log_path = ensure_log_file(config.run_id, config.name)
        try:
            _ = update_agent_status(db, config.run_id, config.name, 'running')
            _ = insert_event(db, config.run_id, config.name, 'started', {'prompt': config.prompt[:200]})
            if is_manager:
                error = 'codex runtime currently supports worker agents only'
                _ = update_agent_status(db, config.run_id, config.name, 'failed', error)
                _ = insert_event(db, config.run_id, config.name, 'error', {'error': error})
                return {'success': False, 'status': 'failed', 'cost': 0.0, 'error': error}
            env = _build_agent_env(config)
            last_message_path = log_path.with_suffix('.last-message.txt')
            cmd = _build_codex_command(config, env, last_message_path)
            _ = logger.info(f'Starting worker {config.name} via Codex CLI in {config.worktree}')
            result = await _run_subprocess(cmd, cwd=config.worktree, env=env)
            _ = update_agent_iteration(db, config.run_id, config.name, 1)
            with open(log_path, "a", encoding="utf-8") as f:
                if result.stdout:
                    _ = f.write(result.stdout)
                    if not result.stdout.endswith('\n'):
                        _ = f.write('\n')
                if result.stderr:
                    _ = f.write(result.stderr)
                    if not result.stderr.endswith('\n'):
                        _ = f.write('\n')
            if result.returncode != 0:
                error = result.stderr.strip() or result.stdout.strip() or f'codex exec exited {result.returncode}'
                _ = update_agent_status(db, config.run_id, config.name, 'failed', error[:1000])
                _ = insert_event(db, config.run_id, config.name, 'error', {'error': error[:1000]})
                return {'success': False, 'status': 'failed', 'cost': 0.0, 'error': error[:1000]}
            check = await _run_check(config, env)
            if check.stdout or check.stderr:
                with open(log_path, "a", encoding="utf-8") as f:
                    _ = f.write('\n[check stdout]\n')
                    _ = f.write(check.stdout)
                    _ = f.write('\n[check stderr]\n')
                    _ = f.write(check.stderr)
            if check.returncode != 0:
                error = check.stderr.strip() or check.stdout.strip() or f'check exited {check.returncode}'
                _ = update_agent_status(db, config.run_id, config.name, 'failed', error[:1000])
                _ = insert_event(db, config.run_id, config.name, 'error', {'error': error[:1000]})
                return {'success': False, 'status': 'failed', 'cost': 0.0, 'error': error[:1000]}
            final_message = ''
            if last_message_path.exists():
                final_message = last_message_path.read_text(encoding="utf-8")
                if final_message:
                    with open(log_path, "a", encoding="utf-8") as f:
                        _ = f.write('\n[final message]\n')
                        _ = f.write(final_message)
                        if not final_message.endswith('\n'):
                            _ = f.write('\n')
            _ = update_agent_cost(db, config.run_id, config.name, 0.0)
            _ = update_agent_status(db, config.run_id, config.name, 'completed')
            _ = insert_event(db, config.run_id, config.name, 'done', {'summary': final_message[:1000] or 'Codex CLI completed and check passed'})
            agent = get_agent(db, config.run_id, config.name)
            return {'success': True, 'status': agent['status'] if agent else 'completed', 'cost': 0.0}
        except Exception as e:
            _ = logger.error(f'Codex worker {config.name} failed: {e}')
            _ = update_agent_status(db, config.run_id, config.name, 'failed', str(e))
            _ = insert_event(db, config.run_id, config.name, 'error', {'error': str(e)})
            return {'success': False, 'status': 'failed', 'cost': 0.0, 'error': str(e)}
        finally:
            _ = db.close()
_ = register(CodexExecutor())
