"""Codex executor.

The Codex Python SDK is the preferred engine when available. It manages the
local app-server with an async context manager, which pairs startup and
shutdown explicitly. The CLI engine remains available as a fallback for
machines that only have the `codex` executable installed.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import shlex
import subprocess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypedDict

from spawnd.runtime.executors.base import Executor, register
from spawnd.storage.db import (
    get_agent,
    insert_event,
    open_db,
    update_agent_cost,
    update_agent_iteration,
    update_agent_status,
)
from spawnd.storage.paths import ensure_log_file

if TYPE_CHECKING:
    from spawnd.runtime.agent_run import AgentConfig
    from spawnd.tools.toolset import Toolset

logger = logging.getLogger("spawnd.executors.codex")

CODEX_ENGINE_AUTO = "auto"
CODEX_ENGINE_SDK = "sdk"
CODEX_ENGINE_CLI = "cli"


class _CodexRunResult(TypedDict, total=False):
    status: str
    error: str
    final_message: str
    thread_id: str
    input_tokens: int
    output_tokens: int


def _build_agent_env(config: AgentConfig) -> dict[str, str]:
    """Build environment variables for Codex subprocesses and checks."""
    env = os.environ.copy()
    env.update(
        {
            "SPAWND_RUN_ID": config.run_id,
            "SPAWND_AGENT_NAME": config.name,
            "SPAWND_PARENT_AGENT": config.parent or "",
            "SPAWND_TREE_PATH": config.tree_path(),
        }
    )
    if config.env:
        env.update(config.env)
    return env


def _env_truthy(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _codex_model(config: AgentConfig, env: dict[str, str]) -> str | None:
    env_model = env.get("SPAWND_CODEX_MODEL")
    if env_model:
        return env_model
    if config.model and config.model not in {"sonnet", "opus", "haiku"}:
        return config.model
    return None


def _codex_engine(env: dict[str, str]) -> str:
    engine = env.get("SPAWND_CODEX_ENGINE", CODEX_ENGINE_AUTO).lower()
    if engine not in {CODEX_ENGINE_AUTO, CODEX_ENGINE_SDK, CODEX_ENGINE_CLI}:
        raise ValueError("SPAWND_CODEX_ENGINE must be one of: auto, sdk, cli")
    return engine


def _load_codex_sdk() -> ModuleType | None:
    try:
        return importlib.import_module("openai_codex")
    except ModuleNotFoundError as exc:
        if exc.name != "openai_codex":
            raise
        return None


def _sdk_required_error() -> str:
    return (
        "Codex SDK engine requested but openai_codex is not installed. "
        "Install the optional Codex SDK dependency or use SPAWND_CODEX_ENGINE=cli."
    )


def _sdk_sandbox(module: ModuleType, env: dict[str, str]):
    value = env.get("SPAWND_CODEX_SANDBOX", "workspace-write").lower()
    sandbox = module.Sandbox
    if value in {"read-only", "read_only"}:
        return sandbox.read_only
    if value in {"workspace-write", "workspace_write"}:
        return sandbox.workspace_write
    if value in {"danger-full-access", "danger_full_access", "full-access", "full_access"}:
        return sandbox.full_access
    raise ValueError(
        "SPAWND_CODEX_SANDBOX must be one of: read-only, workspace-write, danger-full-access"
    )


def _sdk_approval_mode(module: ModuleType, env: dict[str, str]):
    value = env.get("SPAWND_CODEX_APPROVAL_MODE", "deny_all").lower()
    approval_mode = module.ApprovalMode
    if value in {"deny_all", "never"}:
        return approval_mode.deny_all
    if value in {"auto_review", "on-request", "on_request"}:
        return approval_mode.auto_review
    raise ValueError("SPAWND_CODEX_APPROVAL_MODE must be one of: deny_all, auto_review")


def _build_codex_command(
    config: AgentConfig,
    env: dict[str, str],
    last_message_path: Path,
) -> list[str]:
    """Build the documented non-interactive Codex CLI invocation."""
    codex_bin = env.get("SPAWND_CODEX_BIN", "codex")
    cmd = [
        codex_bin,
        "exec",
        "--cd",
        str(config.worktree),
        "--output-last-message",
        str(last_message_path),
    ]
    model = _codex_model(config, env)
    if model:
        cmd.extend(["--model", model])

    if _env_truthy(env, "SPAWND_CODEX_EPHEMERAL", True):
        cmd.append("--ephemeral")

    if _env_truthy(env, "SPAWND_CODEX_DANGEROUS_BYPASS", False):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox = env.get("SPAWND_CODEX_SANDBOX", "workspace-write")
        if sandbox:
            cmd.extend(["--sandbox", sandbox])

    extra_args = env.get("SPAWND_CODEX_EXTRA_ARGS")
    if extra_args:
        cmd.extend(shlex.split(extra_args))

    cmd.append(config.prompt)
    return cmd


async def _run_subprocess(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


async def _run_check(config: AgentConfig, env: dict[str, str]) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(
        subprocess.run,
        config.check_command,
        shell=True,
        cwd=config.worktree,
        env=env,
        capture_output=True,
        text=True,
    )


def _append_process_output(log_path: Path, result: subprocess.CompletedProcess) -> None:
    with open(log_path, "a", encoding="utf-8") as f:
        if result.stdout:
            _ = f.write(result.stdout)
            if not result.stdout.endswith("\n"):
                _ = f.write("\n")
        if result.stderr:
            _ = f.write(result.stderr)
            if not result.stderr.endswith("\n"):
                _ = f.write("\n")


def _append_check_output(log_path: Path, check: subprocess.CompletedProcess) -> None:
    if not check.stdout and not check.stderr:
        return
    with open(log_path, "a", encoding="utf-8") as f:
        _ = f.write("\n[check stdout]\n")
        _ = f.write(check.stdout)
        _ = f.write("\n[check stderr]\n")
        _ = f.write(check.stderr)


def _usage_tokens(usage: Any) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    dumped = usage.model_dump(by_alias=True, mode="json")
    total = dumped.get("total") or {}
    input_tokens = int(total.get("inputTokens") or 0)
    output_tokens = int(total.get("outputTokens") or 0)
    reasoning_tokens = int(total.get("reasoningOutputTokens") or 0)
    return input_tokens, output_tokens + reasoning_tokens


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status))


class CodexExecutor(Executor):
    """Drive a Codex worker via SDK or CLI."""

    runtime = "codex"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        db = open_db(config.run_id)
        is_manager = "mark_plan_complete" in toolset.coord
        log_path = ensure_log_file(config.run_id, config.name)

        try:
            update_agent_status(db, config.run_id, config.name, "running")
            _ = insert_event(db, config.run_id, config.name, "started", {"prompt": config.prompt[:200]})

            if is_manager:
                error = "codex runtime currently supports worker agents only"
                update_agent_status(db, config.run_id, config.name, "failed", error)
                _ = insert_event(db, config.run_id, config.name, "error", {"error": error})
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}

            env = _build_agent_env(config)
            engine = _codex_engine(env)
            sdk_module = _load_codex_sdk()

            if engine == CODEX_ENGINE_SDK and sdk_module is None:
                error = _sdk_required_error()
                update_agent_status(db, config.run_id, config.name, "failed", error)
                _ = insert_event(db, config.run_id, config.name, "error", {"error": error})
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}

            use_sdk = engine == CODEX_ENGINE_SDK or (
                engine == CODEX_ENGINE_AUTO and sdk_module is not None
            )
            if use_sdk:
                assert sdk_module is not None
                result = await self._run_sdk(config, env, log_path, sdk_module)
            else:
                result = await self._run_cli(config, env, log_path)

            update_agent_iteration(db, config.run_id, config.name, 1)

            status = result.get("status", "failed")
            if status != "completed":
                error = result.get("error", "Codex run failed")
                update_agent_status(db, config.run_id, config.name, "failed", error[:1000])
                _ = insert_event(db, config.run_id, config.name, "error", {"error": error[:1000]})
                return {"success": False, "status": "failed", "cost": 0.0, "error": error[:1000]}

            check = await _run_check(config, env)
            _append_check_output(log_path, check)
            if check.returncode != 0:
                error = check.stderr.strip() or check.stdout.strip() or f"check exited {check.returncode}"
                update_agent_status(db, config.run_id, config.name, "failed", error[:1000])
                _ = insert_event(db, config.run_id, config.name, "error", {"error": error[:1000]})
                return {"success": False, "status": "failed", "cost": 0.0, "error": error[:1000]}

            update_agent_cost(db, config.run_id, config.name, 0.0)
            update_agent_status(db, config.run_id, config.name, "completed")
            _ = insert_event(
                db,
                config.run_id,
                config.name,
                "done",
                {
                    "summary": result.get("final_message", "")[:1000]
                    or "Codex completed and check passed"
                },
            )
            agent = get_agent(db, config.run_id, config.name)
            return {
                "success": True,
                "status": agent["status"] if agent else "completed",
                "cost": 0.0,
                "vendor_session_id": result.get("thread_id"),
            }

        except Exception as e:
            logger.error(f"Codex worker {config.name} failed: {e}")
            update_agent_status(db, config.run_id, config.name, "failed", str(e))
            _ = insert_event(db, config.run_id, config.name, "error", {"error": str(e)})
            return {"success": False, "status": "failed", "cost": 0.0, "error": str(e)}

        finally:
            db.close()

    async def _run_sdk(
        self,
        config: AgentConfig,
        env: dict[str, str],
        log_path: Path,
        sdk_module: ModuleType,
    ) -> _CodexRunResult:
        logger.info(f"Starting worker {config.name} via Codex SDK in {config.worktree}")
        sandbox = _sdk_sandbox(sdk_module, env)
        approval_mode = _sdk_approval_mode(sdk_module, env)
        ephemeral = _env_truthy(env, "SPAWND_CODEX_EPHEMERAL", True)
        model = _codex_model(config, env)
        thread_kwargs = {
            "approval_mode": approval_mode,
            "cwd": str(config.worktree),
            "ephemeral": ephemeral,
            "sandbox": sandbox,
        }
        turn_kwargs = {
            "approval_mode": approval_mode,
            "cwd": str(config.worktree),
            "sandbox": sandbox,
        }
        if model:
            thread_kwargs["model"] = model
            turn_kwargs["model"] = model
        async_codex = getattr(sdk_module, "AsyncCodex")

        async with async_codex() as codex:
            thread = await codex.thread_start(**thread_kwargs)
            turn_result = await thread.run(config.prompt, **turn_kwargs)

        final_message = turn_result.final_response or ""
        input_tokens, output_tokens = _usage_tokens(turn_result.usage)
        with open(log_path, "a", encoding="utf-8") as f:
            _ = f.write(f"[codex sdk]\nthread: {thread.id}\nturn: {turn_result.id}\n")
            _ = f.write(f"status: {_status_value(turn_result.status)}\n")
            _ = f.write(f"input_tokens: {input_tokens}\noutput_tokens: {output_tokens}\n")
            if final_message:
                _ = f.write("\n[final message]\n")
                _ = f.write(final_message)
                if not final_message.endswith("\n"):
                    _ = f.write("\n")

        return {
            "status": _status_value(turn_result.status),
            "final_message": final_message,
            "thread_id": thread.id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    async def _run_cli(
        self,
        config: AgentConfig,
        env: dict[str, str],
        log_path: Path,
    ) -> _CodexRunResult:
        logger.info(f"Starting worker {config.name} via Codex CLI in {config.worktree}")
        last_message_path = log_path.with_suffix(".last-message.txt")
        cmd = _build_codex_command(config, env, last_message_path)
        process = await _run_subprocess(cmd, cwd=config.worktree, env=env)
        _append_process_output(log_path, process)

        if process.returncode != 0:
            error = (
                process.stderr.strip()
                or process.stdout.strip()
                or f"codex exec exited {process.returncode}"
            )
            return {"status": "failed", "error": error}

        final_message = ""
        if last_message_path.exists():
            final_message = last_message_path.read_text(encoding="utf-8")
            if final_message:
                with open(log_path, "a", encoding="utf-8") as f:
                    _ = f.write("\n[final message]\n")
                    _ = f.write(final_message)
                    if not final_message.endswith("\n"):
                        _ = f.write("\n")

        return {"status": "completed", "final_message": final_message}


register(CodexExecutor())
