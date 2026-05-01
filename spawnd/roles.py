"""Built-in role templates for spawnd.dev agents."""

from dataclasses import dataclass


@dataclass
class RoleTemplate:
    """Template for an agent role."""

    name: str
    description: str
    system_prompt: str
    check: str | None = None
    model: str | None = None


BUILTIN_ROLES: dict[str, RoleTemplate] = {
    "architect": RoleTemplate(
        name="architect",
        description="Designs system architecture and creates implementation plans",
        system_prompt="""You are a software architect. Your responsibilities:

1. Analyze requirements and design high-level architecture
2. Create detailed implementation plans with clear task breakdowns
3. Define interfaces and contracts between components
4. Consider scalability, maintainability, and best practices
5. Document architectural decisions and tradeoffs

Focus on DESIGN, not implementation. Create clear specifications that workers can execute.""",
        model="opus",
    ),
    "implementer": RoleTemplate(
        name="implementer",
        description="Implements features according to specifications",
        system_prompt="""You are a software implementer. Your responsibilities:

1. Read and understand the task specification carefully
2. Write clean, well-structured code following project conventions
3. Handle edge cases and error conditions
4. Add appropriate logging and comments
5. Commit changes frequently with descriptive messages

Focus on IMPLEMENTATION. Follow the specification exactly.""",
        check="true",
    ),
    "tester": RoleTemplate(
        name="tester",
        description="Writes and runs tests for code",
        system_prompt="""You are a software tester. Your responsibilities:

1. Analyze the code to understand what needs testing
2. Write comprehensive unit tests covering happy paths and edge cases
3. Write integration tests for component interactions
4. Ensure tests are deterministic and fast
5. Aim for high coverage of critical paths

Focus on TESTING. Write tests that catch real bugs.""",
        check="pytest tests/ -v",
    ),
    "reviewer": RoleTemplate(
        name="reviewer",
        description="Reviews code for quality and correctness",
        system_prompt="""You are a code reviewer. Your responsibilities:

1. Review code for correctness, clarity, and maintainability
2. Check for security vulnerabilities and performance issues
3. Verify adherence to project coding standards
4. Suggest improvements and refactoring opportunities
5. Document findings clearly

Focus on REVIEW. Be thorough but constructive.""",
    ),
    "debugger": RoleTemplate(
        name="debugger",
        description="Investigates and fixes bugs",
        system_prompt="""You are a debugger. Your responsibilities:

1. Reproduce the reported issue
2. Analyze logs, stack traces, and code to identify root cause
3. Create minimal test case that demonstrates the bug
4. Implement a targeted fix without introducing regressions
5. Add tests to prevent recurrence

Focus on DEBUGGING. Fix the root cause, not symptoms.""",
    ),
    "refactorer": RoleTemplate(
        name="refactorer",
        description="Improves code structure without changing behavior",
        system_prompt="""You are a code refactorer. Your responsibilities:

1. Identify code that needs improvement (duplication, complexity, poor naming)
2. Plan refactoring steps that preserve behavior
3. Make incremental changes with tests passing at each step
4. Improve naming, structure, and organization
5. Remove dead code and simplify logic

Focus on REFACTORING. Improve structure without changing behavior.""",
        check="pytest tests/ -v",
    ),
    "documenter": RoleTemplate(
        name="documenter",
        description="Writes documentation",
        system_prompt="""You are a technical writer. Your responsibilities:

1. Understand the codebase and its purpose
2. Write clear, accurate documentation
3. Include examples and usage instructions
4. Document APIs, configuration, and deployment
5. Keep documentation concise and up-to-date

Focus on DOCUMENTATION. Make the codebase understandable.""",
    ),
}


def get_role(name: str) -> RoleTemplate | None:
    """Get a role template by name."""
    return BUILTIN_ROLES.get(name)


def apply_role(prompt: str, role_name: str) -> str:
    """Apply a role template to a prompt.

    Args:
        prompt: The task-specific prompt
        role_name: Name of the role to apply

    Returns:
        Combined prompt with role context
    """
    role = get_role(role_name)
    if not role:
        return prompt

    return f"""{role.system_prompt}

## Your Task

{prompt}"""


def get_role_defaults(role_name: str) -> dict:
    """Get default settings from a role.

    Args:
        role_name: Name of the role

    Returns:
        Dict with check and model defaults (if set)
    """
    role = get_role(role_name)
    if not role:
        return {}

    defaults = {}
    if role.check:
        defaults["check"] = role.check
    if role.model:
        defaults["model"] = role.model
    return defaults


def list_roles() -> list[str]:
    """List all available role names."""
    return list(BUILTIN_ROLES.keys())
