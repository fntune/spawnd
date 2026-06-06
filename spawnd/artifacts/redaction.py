"""Redaction helpers for durable deployed-mode provenance."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEY = re.compile(
    r'(token|secret|password|passwd|pwd|credential|auth|api[_-]?key|private[_-]?key|database_url|redis_url)',
    re.IGNORECASE,
)
ASSIGNMENT_SECRET = re.compile(
    r'(?P<key>[A-Z0-9_]*(TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTH|API_KEY|PRIVATE_KEY)[A-Z0-9_]*)\s*=\s*(?P<value>[^\s]+)',
    re.IGNORECASE,
)


def stable_hash(value: str | bytes) -> str:
    """Return a stable SHA-256 hex digest."""

    data = value if isinstance(value, bytes) else value.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def text_metadata(value: str) -> dict[str, Any]:
    """Return non-sensitive metadata for text without retaining the text."""

    encoded = value.encode('utf-8')
    return {'sha256': stable_hash(encoded), 'bytes': len(encoded), 'lines': value.count('\n') + (1 if value else 0)}


def redact_freeform_text(value: str, *, limit: int = 20000) -> str:
    """Redact obvious secret assignments while preserving useful output context."""

    redacted = ASSIGNMENT_SECRET.sub(lambda match: f"{match.group('key')}=<redacted>", value)
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + f'\n[truncated {len(redacted) - limit} chars]'


def redact_env(env: Mapping[str, str] | None) -> dict[str, Any]:
    """Describe environment variables without retaining secret values."""

    if not env:
        return {'count': 0, 'keys': [], 'sensitive_key_hashes': []}
    keys = []
    sensitive = []
    for key in sorted(env):
        if SENSITIVE_KEY.search(key):
            sensitive.append(stable_hash(key))
        else:
            keys.append(key)
    return {'count': len(env), 'keys': keys, 'sensitive_key_hashes': sensitive}


def redact_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return span/event attributes safe for Postgres and telemetry export."""

    if not attributes:
        return {}
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        if SENSITIVE_KEY.search(key):
            safe[key] = '<redacted>'
        elif isinstance(value, str):
            safe[key] = value if len(value) <= 500 else {'sha256': stable_hash(value), 'chars': len(value)}
        elif isinstance(value, bytes):
            safe[key] = {'sha256': stable_hash(value), 'bytes': len(value)}
        elif isinstance(value, Mapping):
            safe[key] = redact_attributes(value)
        elif isinstance(value, (list, tuple)):
            safe[key] = [redact_attributes({'value': item})['value'] for item in value[:50]]
        else:
            safe[key] = value
    return safe


def canonical_json_hash(value: Mapping[str, Any]) -> str:
    """Hash a JSON-serializable mapping deterministically."""

    return stable_hash(json.dumps(value, sort_keys=True, separators=(',', ':')))
