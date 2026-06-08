"""Policy for plan-provided command execution."""
from __future__ import annotations

import re
import shlex

from spawnd.models.specs import CommandPolicy


class CommandPolicyError(ValueError):
    """A plan-provided command is not allowed by execution policy."""


_SHELL_CONTROL = re.compile(r'[\n\r;|<>`]|[$]\(')
_ENV_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=.*$')


def validate_plan_command(command: str | None, policy: CommandPolicy | None, *, purpose: str) -> None:
    """Validate a plan-provided shell command against an allowlist policy."""

    if not command:
        return
    effective = policy or CommandPolicy()
    if effective.mode == 'unrestricted':
        return
    if _SHELL_CONTROL.search(command):
        raise CommandPolicyError(f'{purpose} command uses shell control syntax not allowed by command policy')
    for segment in command.split('&&'):
        executable = _executable(segment)
        if executable not in set(effective.allowed_commands):
            raise CommandPolicyError(f'{purpose} command executable is not allowed: {executable}')


def _executable(segment: str) -> str:
    try:
        parts = shlex.split(segment)
    except ValueError as exc:
        raise CommandPolicyError(f'Invalid command syntax: {exc}') from exc
    if not parts:
        raise CommandPolicyError('Empty command segment')
    index = 0
    while index < len(parts) and _ENV_ASSIGNMENT.match(parts[index]):
        index += 1
    if index >= len(parts):
        raise CommandPolicyError('Command segment contains only environment assignments')
    return parts[index]
