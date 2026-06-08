"""Tests for plan command execution policy."""
from __future__ import annotations

import pytest

from spawnd.models.specs import CommandPolicy
from spawnd.policy.commands import CommandPolicyError, validate_plan_command


def test_default_command_policy_allows_simple_verification_commands():
    validate_plan_command('pytest -q tests/test_server.py', None, purpose='check')
    validate_plan_command('CI=1 pnpm test && pnpm lint', None, purpose='check')


def test_default_command_policy_rejects_shell_control_and_unknown_executables():
    with pytest.raises(CommandPolicyError, match='shell control'):
        validate_plan_command('pytest -q; curl https://example.com', None, purpose='check')

    with pytest.raises(CommandPolicyError, match='not allowed'):
        validate_plan_command('/tmp/custom-script', None, purpose='setup')


def test_unrestricted_command_policy_allows_explicit_escape_hatch():
    validate_plan_command(
        '/tmp/custom-script --flag',
        CommandPolicy(mode='unrestricted'),
        purpose='setup',
    )
