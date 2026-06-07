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


def _cli_scratch_dir(config: AgentConfig, env: dict[str, str]) -> Path:
    """Return Codex CLI scratch outside the git worktree."""

    runtime_root = env.get("SPAWND_RUNTIME_SCRATCH_ROOT")
    if runtime_root:
        return Path(runtime_root).expanduser() / config.run_id / config.name

    scratch_root = env.get("SPAWND_SCRATCH_ROOT")
    if scratch_root:
        return Path(scratch_root).expanduser() / "runtime" / config.run_id / config.name

    worktree = config.worktree.resolve()
    if worktree.parent.name == config.run_id and worktree.parent.parent.name == "worktrees":
        return worktree.parent.parent.parent / "runtime" / config.run_id / config.name
    return worktree.parent / ".spawnd-runtime-scratch" / config.run_id / config.name


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
        is_manager = "mark_plan_complete" in toolset.coord

        try:
            config.observer.event("started", {"runtime": "codex"})

            if is_manager:
                error = "codex runtime currently supports worker agents only"
                config.observer.error("codex", error)
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}

            env = config.execution_env(os.environ)
            engine = _codex_engine(env)
            sdk_module = _load_codex_sdk()

            if engine == CODEX_ENGINE_SDK and sdk_module is None:
                error = _sdk_required_error()
                config.observer.error("codex_sdk", error)
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}

            use_sdk = engine == CODEX_ENGINE_SDK or (
                engine == CODEX_ENGINE_AUTO and sdk_module is not None
            )
            if use_sdk:
                assert sdk_module is not None
                result = await self._run_sdk(config, env, sdk_module)
            else:
                result = await self._run_cli(config, env)

            status = result.get("status", "failed")
            if status != "completed":
                error = result.get("error", "Codex run failed")
                config.observer.error("codex", error[:1000])
                return {
                    "success": False,
                    "status": "failed",
                    "cost": 0.0,
                    "error": error[:1000],
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "final_message": result.get("final_message", ""),
                    "cost_source": "codex",
                }

            final_message = result.get("final_message", "")
            config.observer.final(final_message)
            config.observer.usage(
                input_tokens=result.get("input_tokens", 0),
                output_tokens=result.get("output_tokens", 0),
                cost_usd=0.0,
                source="codex",
                raw={"thread_id": result.get("thread_id")},
            )
            return {
                "success": True,
                "status": "completed",
                "cost": 0.0,
                "vendor_session_id": result.get("thread_id"),
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "final_message": result.get("final_message", ""),
                "cost_source": "codex",
            }

        except Exception as e:
            logger.error(f"Codex worker {config.name} failed: {e}")
            config.observer.error("codex", str(e))
            return {"success": False, "status": "failed", "cost": 0.0, "error": str(e)}

    async def _run_sdk(
        self,
        config: AgentConfig,
        env: dict[str, str],
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
        config.observer.invocation(
            "codex_sdk_turn",
            {
                "thread_id": thread.id,
                "turn_id": turn_result.id,
                "status": _status_value(turn_result.status),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

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
    ) -> _CodexRunResult:
        logger.info(f"Starting worker {config.name} via Codex CLI in {config.worktree}")
        scratch_dir = _cli_scratch_dir(config, env)
        scratch_dir.mkdir(parents=True, exist_ok=True)
        last_message_path = scratch_dir / "last-message.txt"
        cmd = _build_codex_command(config, env, last_message_path)
        process = await _run_subprocess(cmd, cwd=config.worktree, env=env)
        config.observer.invocation(
            "codex_cli_subprocess",
            {
                "argv_preview": " ".join(shlex.quote(part) for part in cmd[:6]),
                "returncode": process.returncode,
                "stdout_bytes": len(process.stdout.encode("utf-8")),
                "stderr_bytes": len(process.stderr.encode("utf-8")),
            },
        )

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

        return {"status": "completed", "final_message": final_message}


register(CodexExecutor())
