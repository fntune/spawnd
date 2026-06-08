"""Codex executor.

The Codex Python SDK is the preferred engine when available. It manages the
local app-server with an async context manager, which pairs startup and
shutdown explicitly. The CLI engine remains available as a fallback for
machines that only have the `codex` executable installed.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypedDict

from spawnd.core.budget import estimate_cost_usd
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
    model: str


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


def _apply_codex_config(config: AgentConfig, env: dict[str, str]) -> dict[str, str]:
    resolved = dict(env)
    if config.codex is None:
        return resolved
    resolved["SPAWND_CODEX_ENGINE"] = config.codex.engine
    resolved["SPAWND_CODEX_SANDBOX"] = config.codex.sandbox
    resolved["SPAWND_CODEX_APPROVAL_MODE"] = config.codex.approval_mode
    resolved["SPAWND_CODEX_EPHEMERAL"] = "true" if config.codex.ephemeral else "false"
    resolved["SPAWND_CODEX_DANGEROUS_BYPASS"] = "true" if config.codex.dangerous_bypass else "false"
    return resolved


def _apply_codex_mcp_env(config: AgentConfig, env: dict[str, str]) -> dict[str, str]:
    """Resolve Codex MCP stdio env refs into the Codex process environment."""

    resolved = dict(env)
    for server in config.mcp_servers or []:
        if server.type == 'stdio' and server.env_refs:
            resolved.update(_resolve_refs_from_env(server.env_refs, resolved, f"MCP server {server.name} env"))
    return resolved


def _resolve_refs_from_env(refs: dict[str, str], env: dict[str, str], label: str) -> dict[str, str]:
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for target, source in sorted(refs.items()):
        value = env.get(source)
        if value is None:
            missing.append(source)
            continue
        resolved[target] = value
    if missing:
        raise ValueError(f"Missing {label} secret refs: {', '.join(missing)}")
    return resolved


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
    if config.resume_thread_id:
        cmd = [codex_bin, "exec", "resume"]
    else:
        cmd = [codex_bin, "exec", "--cd", str(config.worktree)]
    cmd.extend(["--output-last-message", str(last_message_path), "--json"])
    model = _codex_model(config, env)
    if model:
        cmd.extend(["--model", model])

    if _env_truthy(env, "SPAWND_CODEX_EPHEMERAL", True):
        cmd.append("--ephemeral")

    if _env_truthy(env, "SPAWND_CODEX_DANGEROUS_BYPASS", False):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    elif not config.resume_thread_id:
        sandbox = env.get("SPAWND_CODEX_SANDBOX", "workspace-write")
        if sandbox:
            cmd.extend(["--sandbox", sandbox])

    extra_args = env.get("SPAWND_CODEX_EXTRA_ARGS")
    if extra_args:
        cmd.extend(shlex.split(extra_args))
    cmd.extend(_codex_mcp_config_options(config, env))

    if config.resume_thread_id:
        cmd.append(config.resume_thread_id)
    cmd.append(config.prompt)
    return cmd


def _codex_mcp_config_options(config: AgentConfig, env: dict[str, str]) -> list[str]:
    options = [
        "-c",
        'mcp_servers.spawnd.command="python"',
        "-c",
        'mcp_servers.spawnd.args=["-m","spawnd.tools.mcp"]',
    ]
    for key in ('SPAWND_RUN_ID', 'SPAWND_AGENT_NAME', 'SPAWND_AGENT_TYPE', 'SPAWND_PARENT_AGENT', 'SPAWND_TREE_PATH'):
        if env.get(key) is not None:
            options.extend(["-c", f"mcp_servers.spawnd.env.{key}={_toml_string(str(env[key]))}"])
    for server in config.mcp_servers or []:
        options.extend(_codex_external_mcp_options(server))
    return options


def _codex_external_mcp_options(server) -> list[str]:
    prefix = f"mcp_servers.{server.name}"
    if server.type == 'stdio':
        options = ["-c", f"{prefix}.command={_toml_string(str(server.command))}"]
        if server.args:
            options.extend(["-c", f"{prefix}.args={_toml_array(server.args)}"])
        return options
    if server.type == 'http':
        options = ["-c", f"{prefix}.url={_toml_string(str(server.url))}"]
        auth_ref = server.header_refs.get('Authorization')
        if auth_ref:
            options.extend(["-c", f"{prefix}.bearer_token_env_var={_toml_string(auth_ref)}"])
        return options
    return []


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_array(values: list[str]) -> str:
    return '[' + ','.join(_toml_string(str(value)) for value in values) + ']'


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
    timeout_seconds: int | None,
) -> subprocess.CompletedProcess:
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(args, process.returncode or 0, stdout, stderr)


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


def _extract_cli_facts(stdout: str) -> dict[str, Any]:
    """Extract honest thread and usage facts from Codex CLI JSONL output."""

    facts: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        _merge_cli_fact(facts, 'thread_id', _find_first(event, ('thread_id', 'threadId', 'session_id', 'sessionId', 'conversation_id', 'conversationId')))
        _merge_cli_fact(facts, 'model', _find_first(event, ('model',)))
        input_tokens = _find_first(event, ('input_tokens', 'inputTokens', 'prompt_tokens', 'promptTokens'))
        output_tokens = _find_first(event, ('output_tokens', 'outputTokens', 'completion_tokens', 'completionTokens'))
        reasoning_tokens = _find_first(event, ('reasoning_output_tokens', 'reasoningOutputTokens'))
        if input_tokens is not None:
            facts['input_tokens'] = int(facts.get('input_tokens') or 0) + int(input_tokens or 0)
        if output_tokens is not None or reasoning_tokens is not None:
            facts['output_tokens'] = (
                int(facts.get('output_tokens') or 0)
                + int(output_tokens or 0)
                + int(reasoning_tokens or 0)
            )
    return facts


def _merge_cli_fact(facts: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and facts.get(key) is None:
        facts[key] = value


def _find_first(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        for child in value.values():
            found = _find_first(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, keys)
            if found is not None:
                return found
    return None


class CodexExecutor(Executor):
    """Drive a Codex worker via SDK or CLI."""

    runtime = "codex"

    async def run(self, config: AgentConfig, toolset: Toolset) -> dict:
        is_manager = "mark_plan_complete" in toolset.coord

        try:
            config.observer.event("started", {"runtime": "codex"})

            env = _apply_codex_config(config, config.execution_env(os.environ))
            env = _apply_codex_mcp_env(config, env)
            engine = _codex_engine(env)
            if is_manager and engine == CODEX_ENGINE_SDK:
                error = "codex manager agents require Codex CLI MCP support; SDK engine was requested"
                config.observer.error("codex", error)
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}
            if config.mcp_servers and engine == CODEX_ENGINE_SDK:
                error = "codex configured MCP servers require Codex CLI engine; SDK engine was requested"
                config.observer.error("codex", error)
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}
            sdk_module = _load_codex_sdk()

            if engine == CODEX_ENGINE_SDK and sdk_module is None:
                error = _sdk_required_error()
                config.observer.error("codex_sdk", error)
                return {"success": False, "status": "failed", "cost": 0.0, "error": error}

            use_sdk = engine == CODEX_ENGINE_SDK or (
                engine == CODEX_ENGINE_AUTO and sdk_module is not None and not is_manager and not config.mcp_servers
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
            input_tokens = int(result.get("input_tokens", 0) or 0)
            output_tokens = int(result.get("output_tokens", 0) or 0)
            model = result.get("model") or _codex_model(config, env) or "gpt-5"
            cost = estimate_cost_usd(str(model), input_tokens, output_tokens) if input_tokens or output_tokens else 0.0
            cost_source = "estimated" if input_tokens or output_tokens else "codex"
            if cost > config.max_cost_usd:
                message = f"Cost exceeded: ${cost:.4f}"
                config.observer.error("codex", message, {"budget": config.max_cost_usd})
                return {
                    "success": False,
                    "status": "cost_exceeded",
                    "cost": cost,
                    "error": message,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "final_message": final_message,
                    "cost_source": cost_source,
                }
            config.observer.final(final_message)
            config.observer.usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                source=cost_source,
                raw={"thread_id": result.get("thread_id")},
            )
            return {
                "success": True,
                "status": "completed",
                "cost": cost,
                "vendor_session_id": result.get("thread_id"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "final_message": result.get("final_message", ""),
                "cost_source": cost_source,
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
            thread = await _sdk_start_or_resume_thread(codex, config, thread_kwargs)
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
            "model": model or "gpt-5",
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
        process = await _run_subprocess(cmd, cwd=config.worktree, env=env, timeout_seconds=config.runtime_timeout_seconds)
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

        return {"status": "completed", "final_message": final_message, **_extract_cli_facts(process.stdout)}


register(CodexExecutor())


async def _sdk_start_or_resume_thread(codex: Any, config: AgentConfig, thread_kwargs: dict[str, Any]) -> Any:
    """Start or resume a Codex SDK thread using the installed SDK surface."""

    if not config.resume_thread_id:
        return await codex.thread_start(**thread_kwargs)
    resume_id = config.resume_thread_id
    for method_name in ('thread_resume', 'resume_thread'):
        method = getattr(codex, method_name, None)
        if method is None:
            continue
        return await _call_sdk_resume(method, resume_id, thread_kwargs)
    raise RuntimeError('Codex SDK resume requested, but installed SDK exposes no thread_resume/resume_thread method')


async def _call_sdk_resume(method: Any, resume_id: str, thread_kwargs: dict[str, Any]) -> Any:
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        params = {}
    if 'thread_id' in params:
        return await method(thread_id=resume_id, **thread_kwargs)
    if 'threadId' in params:
        return await method(threadId=resume_id, **thread_kwargs)
    if 'thread' in params:
        return await method(thread=resume_id, **thread_kwargs)
    try:
        return await method(resume_id, **thread_kwargs)
    except TypeError:
        return await method(resume_id)
