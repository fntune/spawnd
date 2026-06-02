"""YAML plan spec parsing for spawnd.dev."""
from pathlib import Path
from uuid import uuid4
import yaml
from spawnd.models.specs import PlanSpec

def parse_plan_file(path: Path) -> PlanSpec:
    """Parse a YAML plan file.

    Args:
        path: Path to the YAML file

    Returns:
        Parsed PlanSpec
    """
    with open(path) as f:
        content = f.read()
    plan = parse_plan_yaml(content)
    if plan.shared_context:
        resolved = []
        for entry in plan.shared_context:
            entry_path = Path(entry)
            if entry_path.is_absolute():
                _ = resolved.append(str(entry_path))
            else:
                _ = resolved.append(str((path.parent / entry_path).resolve()))
        plan = plan.model_copy(update={'shared_context': resolved})
    return plan

def parse_plan_yaml(content: str) -> PlanSpec:
    """Parse YAML content into PlanSpec.

    Args:
        content: YAML content string

    Returns:
        Parsed PlanSpec
    """
    data = yaml.safe_load(content)
    return PlanSpec(**data)

def generate_run_id(plan_name: str) -> str:
    """Generate a unique run ID.

    Args:
        plan_name: Name of the plan

    Returns:
        Run ID string
    """
    return f'{plan_name}-{uuid4().hex[:8]}'
