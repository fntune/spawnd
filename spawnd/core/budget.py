"""Cost estimation for runtimes that don't report USD directly.

The Claude SDK ships an authoritative ``total_cost_usd`` on each
``ResultMessage`` (cost_source="sdk"). The OpenAI runtime reports tokens
only; we estimate USD from a price table (cost_source="estimated").
"""

from __future__ import annotations

# USD per 1,000,000 tokens. Per-model (input, output) pair.
# These are conservative defaults — override via SPAWND_MODEL_PRICES env
# or by editing this table. Prices change; treat these as estimates.
_PRICES_PER_MTOKEN: dict[str, tuple[float, float]] = {
    "gpt-5": (5.00, 15.00),
    "gpt-5-mini": (1.00, 3.00),
    "gpt-5-nano": (0.25, 0.80),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "o1": (15.00, 60.00),
    "o1-mini": (3.00, 12.00),
    "o3-mini": (3.00, 12.00),
}

# Default for models not in the table: expensive side of GPT-5, so budget
# alarms fire before a mis-priced model silently burns spend.
_DEFAULT_PRICE: tuple[float, float] = (5.00, 15.00)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost for a given model and token counts.

    Falls back to a conservative default for unknown model names.
    """
    key = _price_key(model)
    input_per_m, output_per_m = _PRICES_PER_MTOKEN.get(key, _DEFAULT_PRICE)
    return (input_tokens / 1_000_000) * input_per_m + (output_tokens / 1_000_000) * output_per_m


def _price_key(model: str) -> str:
    """Normalise a model name to a price table key.

    ``gpt-5-2025-08-14`` → ``gpt-5``; ``gpt-4o-2024-08-06`` → ``gpt-4o``.
    """
    if not model:
        return ""
    # If the model is in the table verbatim, use it directly.
    if model in _PRICES_PER_MTOKEN:
        return model
    # Otherwise strip trailing version suffix (e.g. `-2024-08-06`).
    base = model.rsplit("-20", 1)[0]
    return base if base in _PRICES_PER_MTOKEN else model
