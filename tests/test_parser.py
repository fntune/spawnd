"""Tests for YAML parsing."""

import pytest
from pydantic import ValidationError

from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
from spawnd.io.parser import parse_plan_file, parse_plan_yaml
from spawnd.io.plan_builder import create_inline_plan, infer_agent_name, load_shared_context, parse_inline_agents
from spawnd.io.validation import has_circular_deps, validate_plan


def test_parse_plan_yaml_minimal():
    """Parse minimal plan YAML."""
    yaml_content = """
name: test-plan
agents:
  - name: worker1
    prompt: Do something
"""
    plan = parse_plan_yaml(yaml_content)
    assert plan.name == "test-plan"
    assert len(plan.agents) == 1
    assert plan.agents[0].name == "worker1"


def test_parse_plan_yaml_with_deps():
    """Parse plan with dependencies."""
    yaml_content = """
name: dep-plan
agents:
  - name: first
    prompt: First task
  - name: second
    prompt: Second task
    depends_on:
      - first
"""
    plan = parse_plan_yaml(yaml_content)
    assert len(plan.agents) == 2
    assert plan.agents[1].depends_on == ["first"]


def test_parse_plan_yaml_rejects_invalid_agent_name():
    """Agent names should fail validation before runtime git operations."""
    yaml_content = """
name: bad-plan
agents:
  - name: "bad name"
    prompt: Do something
"""
    with pytest.raises(ValidationError):
        parse_plan_yaml(yaml_content)


def test_parse_plan_file_resolves_shared_context_relative_to_plan(tmp_path, monkeypatch):
    """shared_context entries should resolve relative to the plan file, not cwd."""
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    context = plan_dir / "ctx.txt"
    context.write_text("hello context")
    plan_file = plan_dir / "plan.yaml"
    plan_file.write_text(
        """
name: shared-context
shared_context:
  - ctx.txt
agents:
  - name: worker1
    prompt: Do something
"""
    )

    monkeypatch.chdir(tmp_path)

    plan = parse_plan_file(plan_file)

    assert plan.shared_context == [str(context.resolve())]
    assert "hello context" in load_shared_context(plan.shared_context)


def test_validate_plan_unknown_dep():
    """Detect unknown dependencies."""
    plan = PlanSpec(
        name="test",
        agents=[
            AgentSpec(name="a", prompt="task", depends_on=["unknown"]),
        ],
    )
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert "unknown" in errors[0]


def test_validate_plan_circular_dep():
    """Detect circular dependencies."""
    plan = PlanSpec(
        name="test",
        agents=[
            AgentSpec(name="a", prompt="task", depends_on=["b"]),
            AgentSpec(name="b", prompt="task", depends_on=["a"]),
        ],
    )
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert "Circular" in errors[0]


def test_validate_plan_unknown_role():
    """Detect typos in use_role instead of silently dropping role behavior."""
    plan = PlanSpec(
        name="test",
        agents=[AgentSpec(name="a", prompt="task", use_role="typo-role")],
    )
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert "unknown role" in errors[0]


def test_has_circular_deps():
    """Test circular dependency detection."""
    # No cycle
    agents = [
        AgentSpec(name="a", prompt="task"),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
    ]
    assert not has_circular_deps(agents)

    # Cycle
    agents = [
        AgentSpec(name="a", prompt="task", depends_on=["b"]),
        AgentSpec(name="b", prompt="task", depends_on=["a"]),
    ]
    assert has_circular_deps(agents)


def test_infer_agent_name():
    """Test agent name inference from prompt."""
    assert infer_agent_name("Implement caching") == "caching"
    assert infer_agent_name("Add authentication") == "authentication"
    assert infer_agent_name("Fix database bug") == "database"


def test_parse_inline_agents():
    """Test inline agent parsing."""
    prompts = ["worker: Do task", "another task"]
    agents = parse_inline_agents(prompts)

    assert len(agents) == 2
    assert agents[0].name == "worker"
    assert agents[0].prompt == "Do task"
    assert agents[1].prompt == "another task"


def test_parse_inline_agents_treats_natural_language_colon_as_prompt():
    """Natural-language prompts with colons should not become agent names."""
    agents = parse_inline_agents(["Fix bug: handle timeout"])

    assert agents[0].name == "bug"
    assert agents[0].prompt == "Fix bug: handle timeout"


def test_create_inline_plan_sequential():
    """Test sequential inline plan creation."""
    prompts = ["task1", "task2", "task3"]
    plan = create_inline_plan(prompts, sequential=True)

    assert len(plan.agents) == 3
    assert plan.agents[0].depends_on == []
    assert plan.agents[1].depends_on == [plan.agents[0].name]
    assert plan.agents[2].depends_on == [plan.agents[1].name]


def test_create_inline_plan_with_defaults():
    """Test inline plan with custom defaults."""
    prompts = ["task: Do something"]
    defaults = Defaults(check="make test", max_iterations=10)
    plan = create_inline_plan(prompts, defaults=defaults)

    assert plan.defaults.check == "make test"
    assert plan.defaults.max_iterations == 10
