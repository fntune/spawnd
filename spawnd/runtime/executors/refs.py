"""Secret-reference resolution for runtime executor configuration."""
from __future__ import annotations

import os


def resolve_refs(refs: dict[str, str], label: str) -> dict[str, str]:
    """Resolve named environment secret references for a runtime boundary."""

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for target, source in sorted(refs.items()):
        value = os.environ.get(source)
        if value is None:
            missing.append(source)
            continue
        resolved[target] = value
    if missing:
        raise ValueError(f"Missing {label} secret refs: {', '.join(missing)}")
    return resolved
