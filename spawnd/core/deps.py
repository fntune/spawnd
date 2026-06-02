"""Dependency resolution for spawnd.dev."""
import logging
from typing import Iterator
from spawnd.models.specs import AgentSpec
logger = logging.getLogger('spawnd.deps')

class DependencyGraph:
    """Manages agent dependencies."""

    def __init__(self, agents: list[AgentSpec]):
        """Initialize with list of agents.

        Args:
            agents: List of agent specifications
        """
        self.agents = {a.name: a for a in agents}
        self.deps = {a.name: set(a.depends_on) for a in agents}

    def get_ready_agents(self, completed: set[str], failed: set[str]) -> list[AgentSpec]:
        """Get agents whose dependencies are satisfied.

        Args:
            completed: Set of completed agent names
            failed: Set of failed agent names

        Returns:
            List of agents ready to run
        """
        ready = []
        terminal = completed | failed
        for name, deps in self.deps.items():
            if name in terminal:
                continue
            if deps.issubset(completed):
                _ = ready.append(self.agents[name])
        return ready

    def get_blocked_by_failure(self, failed: set[str]) -> list[str]:
        """Get agents that are blocked due to failed dependencies.

        Args:
            failed: Set of failed agent names

        Returns:
            List of agent names that cannot run
        """
        blocked = []
        for name, deps in self.deps.items():
            if deps & failed:
                _ = blocked.append(name)
        return blocked

    def topological_order(self) -> list[str]:
        """Get execution/merge order (dependencies before dependents).

        Returns:
            List of agent names in topological order

        Raises:
            ValueError: If circular dependency detected
        """
        in_degree = {n: len(d) for n, d in self.deps.items()}
        queue = [n for n, d in in_degree.items() if d == 0]
        order = []
        while queue:
            node = queue.pop(0)
            _ = order.append(node)
            for name, deps in self.deps.items():
                if node in deps:
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        _ = queue.append(name)
        if len(order) != len(self.agents):
            raise ValueError('Circular dependency detected')
        return order

    def reverse_topological_order(self) -> list[str]:
        """Get reverse topological order (dependents before dependencies).

        Useful for cleanup operations.

        Returns:
            List of agent names in reverse topological order
        """
        return list(reversed(self.topological_order()))

    def get_dependents(self, name: str) -> list[str]:
        """Get agents that depend on the given agent.

        Args:
            name: Agent name

        Returns:
            List of dependent agent names
        """
        dependents = []
        for agent_name, deps in self.deps.items():
            if name in deps:
                _ = dependents.append(agent_name)
        return dependents

    def get_subtree(self, name: str) -> set[str]:
        """Get all agents in the subtree rooted at the given agent.

        Includes the agent itself and all its transitive dependents.

        Args:
            name: Root agent name

        Returns:
            Set of agent names in subtree
        """
        subtree = {name}
        queue = [name]
        while queue:
            current = queue.pop(0)
            for dependent in self.get_dependents(current):
                if dependent not in subtree:
                    _ = subtree.add(dependent)
                    _ = queue.append(dependent)
        return subtree

    def iter_layers(self) -> Iterator[list[str]]:
        """Iterate agents in dependency layers.

        Each layer contains agents that can run in parallel.
        Agents in layer N depend only on agents in layers 0..N-1.

        Yields:
            List of agent names that can run in parallel
        """
        remaining = set(self.agents.keys())
        completed = set()
        while remaining:
            layer = [name for name in remaining if self.deps[name].issubset(completed)]
            if not layer:
                raise ValueError('Circular dependency detected')
            yield layer
            _ = completed.update(layer)
            remaining -= set(layer)

    def validate(self) -> list[str]:
        """Validate the dependency graph.

        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        all_names = set(self.agents.keys())
        for name, deps in self.deps.items():
            unknown = deps - all_names
            if unknown:
                _ = errors.append(f'Agent {name} depends on unknown agents: {unknown}')
        try:
            _ = self.topological_order()
        except ValueError:
            _ = errors.append('Circular dependency detected')
        return errors

def resolve_dependencies(agents: list[AgentSpec]) -> list[list[str]]:
    """Resolve dependencies into execution layers.

    Args:
        agents: List of agent specifications

    Returns:
        List of layers, each layer is a list of agent names
        that can run in parallel
    """
    graph = DependencyGraph(agents)
    return list(graph.iter_layers())

def get_merge_order(agents: list[AgentSpec]) -> list[str]:
    """Get the order for merging agent branches.

    Dependencies are merged before dependents to ensure
    dependent agents have access to their deps' code.

    Args:
        agents: List of agent specifications

    Returns:
        List of agent names in merge order
    """
    graph = DependencyGraph(agents)
    return graph.topological_order()
