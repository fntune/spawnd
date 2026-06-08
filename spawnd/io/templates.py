"""Durable run template rendering."""
from __future__ import annotations

from collections import UserDict
from typing import Any

from spawnd.io.parser import parse_plan_yaml
from spawnd.models.specs import PlanSpec


class _StrictFormatMap(UserDict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def render_template_text(template: str, parameters: dict[str, Any]) -> str:
    """Render a template with explicit named parameters."""

    safe_parameters = {key: str(value) for key, value in parameters.items()}
    return template.format_map(_StrictFormatMap(safe_parameters))


def render_plan_template(template: str, parameters: dict[str, Any]) -> PlanSpec:
    """Render template YAML into a concrete plan spec."""

    return parse_plan_yaml(render_template_text(template, parameters))
