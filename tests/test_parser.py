"""Tests for YAML parsing."""
import pytest
from pydantic import ValidationError
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
from spawnd.io.parser import parse_plan_file, parse_plan_yaml
from spawnd.io.plan_builder import create_inline_plan, infer_agent_name, load_shared_context, parse_inline_agents
from spawnd.io.validation import has_circular_deps, validate_plan

def test_parse_plan_yaml_minimal():
    """Parse minimal plan YAML."""
    yaml_content = '\nname: test-plan\nagents:\n  - name: worker1\n    prompt: Do something\n'
    plan = parse_plan_yaml(yaml_content)
    assert plan.name == 'test-plan'
    assert len(plan.agents) == 1
    assert plan.agents[0].name == 'worker1'

def test_parse_plan_yaml_with_deps():
    """Parse plan with dependencies."""
    yaml_content = '\nname: dep-plan\nagents:\n  - name: first\n    prompt: First task\n  - name: second\n    prompt: Second task\n    depends_on:\n      - first\n'
    plan = parse_plan_yaml(yaml_content)
    assert len(plan.agents) == 2
    assert plan.agents[1].depends_on == ['first']

def test_parse_plan_yaml_with_worktree_setup():
    """Parse worktree setup orchestration config."""
    yaml_content = '\nname: setup-plan\norchestration:\n  worktree_source:\n    fetch: true\n    base_ref: origin/HEAD\n  worktree_setup:\n    command: bash scripts/worktree/setup.sh\n    timeout_seconds: 120\n    env:\n      WORKTREE_INSTALL_BROWSERS: "0"\nagents:\n  - name: worker1\n    prompt: Do something\n'
    plan = parse_plan_yaml(yaml_content)
    assert plan.orchestration is not None
    assert plan.orchestration.worktree_source is not None
    assert plan.orchestration.worktree_source.fetch is True
    assert plan.orchestration.worktree_source.base_ref == 'origin/HEAD'
    assert plan.orchestration.worktree_setup is not None
    assert plan.orchestration.worktree_setup.command == 'bash scripts/worktree/setup.sh'
    assert plan.orchestration.worktree_setup.timeout_seconds == 120
    assert plan.orchestration.worktree_setup.env == {'WORKTREE_INSTALL_BROWSERS': '0'}

def test_parse_plan_yaml_accepts_codex_runtime():
    """Codex is a valid runtime at defaults and per-agent scope."""
    yaml_content = '\nname: codex-plan\ndefaults:\n  runtime: codex\nagents:\n  - name: worker1\n    prompt: Do something\n  - name: worker2\n    prompt: Do something else\n    runtime: codex\n'
    plan = parse_plan_yaml(yaml_content)
    assert plan.defaults.runtime == 'codex'
    assert plan.agents[0].runtime is None
    assert plan.agents[1].runtime == 'codex'

def test_parse_plan_yaml_rejects_invalid_agent_name():
    """Agent names should fail validation before runtime git operations."""
    yaml_content = '\nname: bad-plan\nagents:\n  - name: "bad name"\n    prompt: Do something\n'
    with pytest.raises(ValidationError):
        _ = parse_plan_yaml(yaml_content)

def test_parse_plan_file_resolves_shared_context_relative_to_plan(tmp_path, monkeypatch):
    """shared_context entries should resolve relative to the plan file, not cwd."""
    plan_dir = tmp_path / 'plans'
    _ = plan_dir.mkdir()
    context = plan_dir / 'ctx.txt'
    _ = context.write_text('hello context')
    plan_file = plan_dir / 'plan.yaml'
    _ = plan_file.write_text('\nname: shared-context\nshared_context:\n  - ctx.txt\nagents:\n  - name: worker1\n    prompt: Do something\n')
    _ = monkeypatch.chdir(tmp_path)
    plan = parse_plan_file(plan_file)
    assert plan.shared_context == [str(context.resolve())]
    assert 'hello context' in load_shared_context(plan.shared_context)

def test_validate_plan_unknown_dep():
    """Detect unknown dependencies."""
    plan = PlanSpec(name='test', agents=[AgentSpec(name='a', prompt='task', depends_on=['unknown'])])
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert 'unknown' in errors[0]

def test_validate_plan_circular_dep():
    """Detect circular dependencies."""
    plan = PlanSpec(name='test', agents=[AgentSpec(name='a', prompt='task', depends_on=['b']), AgentSpec(name='b', prompt='task', depends_on=['a'])])
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert 'Circular' in errors[0]

def test_validate_plan_unknown_role():
    """Detect typos in use_role instead of silently dropping role behavior."""
    plan = PlanSpec(name='test', agents=[AgentSpec(name='a', prompt='task', use_role='typo-role')])
    errors = validate_plan(plan)
    assert len(errors) == 1
    assert 'unknown role' in errors[0]

def test_has_circular_deps():
    """Test circular dependency detection."""
    agents = [AgentSpec(name='a', prompt='task'), AgentSpec(name='b', prompt='task', depends_on=['a'])]
    assert not has_circular_deps(agents)
    agents = [AgentSpec(name='a', prompt='task', depends_on=['b']), AgentSpec(name='b', prompt='task', depends_on=['a'])]
    assert has_circular_deps(agents)

def test_infer_agent_name():
    """Test agent name inference from prompt."""
    assert infer_agent_name('Implement caching') == 'caching'
    assert infer_agent_name('Add authentication') == 'authentication'
    assert infer_agent_name('Fix database bug') == 'database'

def test_parse_inline_agents():
    """Test inline agent parsing."""
    prompts = ['worker: Do task', 'another task']
    agents = parse_inline_agents(prompts)
    assert len(agents) == 2
    assert agents[0].name == 'worker'
    assert agents[0].prompt == 'Do task'
    assert agents[1].prompt == 'another task'

def test_parse_inline_agents_treats_natural_language_colon_as_prompt():
    """Natural-language prompts with colons should not become agent names."""
    agents = parse_inline_agents(['Fix bug: handle timeout'])
    assert agents[0].name == 'bug'
    assert agents[0].prompt == 'Fix bug: handle timeout'

def test_create_inline_plan_sequential():
    """Test sequential inline plan creation."""
    prompts = ['task1', 'task2', 'task3']
    plan = create_inline_plan(prompts, sequential=True)
    assert len(plan.agents) == 3
    assert plan.agents[0].depends_on == []
    assert plan.agents[1].depends_on == [plan.agents[0].name]
    assert plan.agents[2].depends_on == [plan.agents[1].name]

def test_create_inline_plan_with_defaults():
    """Test inline plan with custom defaults."""
    prompts = ['task: Do something']
    defaults = Defaults(check='make test', max_iterations=10)
    plan = create_inline_plan(prompts, defaults=defaults)
    assert plan.defaults.check == 'make test'
    assert plan.defaults.max_iterations == 10
