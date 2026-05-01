"""Tests for dependency resolution."""

import pytest

from spawnd.core.deps import DependencyGraph, get_merge_order, resolve_dependencies
from spawnd.models.specs import AgentSpec


def test_dependency_graph_ready_agents():
    """Test finding ready agents."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
        AgentSpec(name="c", prompt="task"),
    ]
    graph = DependencyGraph(agents)

    # Initially a and c are ready
    ready = graph.get_ready_agents(completed=set(), failed=set())
    assert len(ready) == 2
    assert {a.name for a in ready} == {"a", "c"}

    # After a completes, b becomes ready
    ready = graph.get_ready_agents(completed={"a"}, failed=set())
    assert len(ready) == 2
    assert {a.name for a in ready} == {"b", "c"}


def test_dependency_graph_blocked_by_failure():
    """Test finding agents blocked by failures."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
        AgentSpec(name="c", prompt="task"),
    ]
    graph = DependencyGraph(agents)

    blocked = graph.get_blocked_by_failure(failed={"a"})
    assert blocked == ["b"]


def test_topological_order():
    """Test topological ordering."""
    agents = [
        AgentSpec(name="c", prompt="task", depends_on=["b"]),
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
    ]
    graph = DependencyGraph(agents)

    order = graph.topological_order()
    assert order.index("a") < order.index("b")
    assert order.index("b") < order.index("c")


def test_topological_order_circular():
    """Test circular dependency detection."""
    agents = [
        AgentSpec(name="a", prompt="task", depends_on=["b"]),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
    ]
    graph = DependencyGraph(agents)

    with pytest.raises(ValueError, match="Circular"):
        graph.topological_order()


def test_iter_layers():
    """Test layer iteration."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task"),
        AgentSpec(name="c", prompt="task", depends_on=["a", "b"]),
        AgentSpec(name="d", prompt="task", depends_on=["c"]),
    ]
    graph = DependencyGraph(agents)

    layers = list(graph.iter_layers())
    assert len(layers) == 3
    assert set(layers[0]) == {"a", "b"}
    assert layers[1] == ["c"]
    assert layers[2] == ["d"]


def test_get_dependents():
    """Test finding dependents."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
        AgentSpec(name="c", prompt="task", depends_on=["a"]),
    ]
    graph = DependencyGraph(agents)

    dependents = graph.get_dependents("a")
    assert set(dependents) == {"b", "c"}


def test_get_subtree():
    """Test subtree extraction."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
        AgentSpec(name="c", prompt="task", depends_on=["b"]),
    ]
    graph = DependencyGraph(agents)

    subtree = graph.get_subtree("a")
    assert subtree == {"a", "b", "c"}


def test_validate_unknown_deps():
    """Test validation of unknown dependencies."""
    agents = [
        AgentSpec(name="a", prompt="task", depends_on=["unknown"]),
    ]
    graph = DependencyGraph(agents)

    errors = graph.validate()
    assert len(errors) >= 1
    assert any("unknown" in e for e in errors)


def test_resolve_dependencies():
    """Test resolve_dependencies helper."""
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
    ]
    layers = resolve_dependencies(agents)
    assert layers == [["a"], ["b"]]


def test_get_merge_order():
    """Test get_merge_order helper."""
    agents = [
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
        AgentSpec(name="a", prompt="task"),
    ]
    order = get_merge_order(agents)
    assert order == ["a", "b"]
