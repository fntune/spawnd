"""Policy for executing provider runtimes inside worker isolation boundaries."""
from __future__ import annotations

from collections.abc import Mapping

from spawnd.models.specs import RuntimeIsolation


class RuntimeIsolationError(ValueError):
    """A write-capable provider runtime would execute without isolation."""


def validate_runtime_isolation(
    *,
    runtime: str,
    agent_type: str,
    write_allowed: bool,
    use_mock: bool,
    policy: RuntimeIsolation | None,
    env: Mapping[str, str],
) -> None:
    """Require an explicit worker isolation boundary for mutable real runtimes."""

    if use_mock or not write_allowed:
        return
    effective = policy or RuntimeIsolation()
    mode = str(env.get('SPAWND_RUNTIME_ISOLATION') or '').strip().lower()
    accepted = {item.lower() for item in effective.accepted}
    if mode in accepted:
        return
    raise RuntimeIsolationError(
        f'{agent_type} agent runtime {runtime} requires SPAWND_RUNTIME_ISOLATION='
        f'{"/".join(sorted(accepted))} before write-capable execution'
    )
