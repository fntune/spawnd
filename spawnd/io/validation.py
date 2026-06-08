"""Plan validation for spawnd.dev."""
from spawnd.models.specs import AgentSpec, PlanSpec
from spawnd.roles import get_role

def has_circular_deps(agents: list[AgentSpec]) -> bool:
    """Check for circular dependencies using DFS."""
    deps = {a.name: set(a.depends_on) for a in agents}

    def visit(name: str, path: set) -> bool:
        if name in path:
            return True
        _ = path.add(name)
        for dep in deps.get(name, []):
            if visit(dep, path):
                return True
        _ = path.remove(name)
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
    names = [a.name for a in plan.agents]
    if len(names) != len(set(names)):
        _ = errors.append('Duplicate agent names found')
    for agent in plan.agents:
        for dep in agent.depends_on:
            if dep not in names:
                _ = errors.append(f'Agent {agent.name} depends on unknown agent: {dep}')
    if has_circular_deps(plan.agents):
        _ = errors.append('Circular dependency detected')
    for agent in plan.agents:
        if agent.use_role and get_role(agent.use_role) is None:
            _ = errors.append(f'Agent {agent.name} uses unknown role: {agent.use_role}')
        mcp_servers = [*plan.defaults.mcp_servers, *agent.mcp_servers]
        runtime = agent.runtime or plan.defaults.runtime
        for server in mcp_servers:
            if server.name == 'spawnd':
                _ = errors.append(f'Agent {agent.name} MCP server name spawnd is reserved')
            if server.type == 'stdio' and not server.command:
                _ = errors.append(f'Agent {agent.name} MCP server {server.name} requires command')
            if server.type in {'http', 'sse'} and not server.url:
                _ = errors.append(f'Agent {agent.name} MCP server {server.name} requires url')
            if runtime == 'codex':
                if server.type == 'sse':
                    _ = errors.append(f'Agent {agent.name} runtime codex does not support SSE MCP server {server.name}')
                if server.type == 'http':
                    if server.headers:
                        _ = errors.append(f'Agent {agent.name} runtime codex MCP server {server.name} does not support literal headers')
                    unsupported_refs = set(server.header_refs) - {'Authorization'}
                    if unsupported_refs:
                        _ = errors.append(
                            f"Agent {agent.name} runtime codex MCP server {server.name} only supports Authorization header_refs"
                        )
    return errors
