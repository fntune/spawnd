"""Inline plan creation and context loading for spawnd.dev."""
import logging
import re
from pathlib import Path
from uuid import uuid4
from spawnd.models.specs import AgentSpec, Defaults, PlanSpec
logger = logging.getLogger('spawnd.plan_builder')
MAX_HIERARCHY_DEPTH = 10
EXPLICIT_AGENT_PATTERN = re.compile('^(?P<name>[A-Za-z0-9_.-]+)\\s*:\\s*(?P<prompt>.+)$')

def infer_agent_name(prompt: str) -> str:
    """Extract key term from prompt for agent name.

    Args:
        prompt: Agent prompt

    Returns:
        Inferred name
    """
    patterns = ['(?:implement|add|create|build)\\s+(\\w+)', '(?:fix|resolve|debug)\\s+(\\w+)', '(?:refactor|update|improve)\\s+(\\w+)']
    for pattern in patterns:
        if (match := re.search(pattern, prompt, re.I)):
            return match.group(1).lower()
    words = [w for w in prompt.split() if len(w) > 3]
    return words[0].lower() if words else f'task-{uuid4().hex[:6]}'

def parse_inline_agents(prompts: list[str]) -> list[AgentSpec]:
    """Parse inline agent definitions from -p flags.

    Supports formats:
    - "name: prompt" - explicit name
    - "prompt" - name inferred from prompt

    Args:
        prompts: List of prompt strings

    Returns:
        List of AgentSpec
    """
    agents = []
    for prompt in prompts:
        match = EXPLICIT_AGENT_PATTERN.match(prompt)
        if match and (not prompt.startswith('http')):
            name = match.group('name')
            prompt_text = match.group('prompt').strip()
        else:
            name = infer_agent_name(prompt)
            prompt_text = prompt
        _ = agents.append(AgentSpec(name=name, prompt=prompt_text))
    return agents

def create_inline_plan(prompts: list[str], sequential: bool=False, defaults: Defaults | None=None) -> PlanSpec:
    """Create a plan from inline prompts.

    Args:
        prompts: List of prompt strings
        sequential: If True, agents depend on previous
        defaults: Default settings

    Returns:
        PlanSpec
    """
    agents = parse_inline_agents(prompts)
    if sequential and len(agents) > 1:
        for i in range(1, len(agents)):
            agents[i].depends_on = [agents[i - 1].name]
    return PlanSpec(name=f'inline-{uuid4().hex[:8]}', defaults=defaults or Defaults(), agents=agents)

def expand_pattern_agents(pattern: str, prompt_template: str, base_path: Path | None=None) -> list[AgentSpec]:
    """Expand a glob pattern to multiple agents.

    Args:
        pattern: Glob pattern (e.g., "src/services/*.py")
        prompt_template: Prompt template with {file} placeholder
        base_path: Base path for glob

    Returns:
        List of AgentSpec, one per matched file
    """
    base = base_path or Path.cwd()
    files = list(base.glob(pattern))
    agents = []
    for file in files:
        name = file.stem.lower().replace('_', '-')
        prompt = prompt_template.format(file=str(file))
        _ = agents.append(AgentSpec(name=name, prompt=prompt))
    return agents

def load_shared_context(paths: list[str], base_path: Path | None=None) -> str:
    """Load shared context files.

    Args:
        paths: List of file paths
        base_path: Base path for relative paths

    Returns:
        Combined content string
    """
    base = base_path or Path.cwd()
    contents = []
    for path in paths:
        full_path = base / path
        if full_path.exists():
            _ = contents.append(f'--- {path} ---\n{full_path.read_text()}')
        else:
            _ = logger.warning(f'Shared context file not found: {path}')
    return '\n\n'.join(contents)
