"""Plan validation for spawnd.dev."""

from spawnd.models.specs import AgentSpec, PlanSpec
from spawnd.roles import get_role


def has_circular_deps(agents: list[AgentSpec]) -> bool:
    """Check for circular dependencies using DFS."""
    deps = {a.name: set(a.depends_on) for a in agents}

    def visit(name: str, path: set) -> bool:
        if name in path:
            return True
        path.add(name)
        for dep in deps.get(name, []):
            if visit(dep, path):
                return True
        path.remove(name)
        return False

    for agent in agents:
        if visit(agent.name, set()):
            return True
    return False


def validate_plan(plan: PlanSpec) -> list[str]:
    """Validate a plan spec.

    Args:
        plan: Plan to validate

    Returns:
        List of validation errors (empty if valid)
    """
    errors = []

    # Check for duplicate agent names
    names = [a.name for a in plan.agents]
    if len(names) != len(set(names)):
        errors.append("Duplicate agent names found")

    # Check dependencies exist
    for agent in plan.agents:
        for dep in agent.depends_on:
            if dep not in names:
                errors.append(f"Agent {agent.name} depends on unknown agent: {dep}")

    # Check for circular dependencies
    if has_circular_deps(plan.agents):
        errors.append("Circular dependency detected")

    # Check roles exist
    for agent in plan.agents:
        if agent.use_role and get_role(agent.use_role) is None:
            errors.append(f"Agent {agent.name} uses unknown role: {agent.use_role}")

    return errors
