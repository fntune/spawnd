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
BUILTIN_ROLES: dict[str, RoleTemplate] = {'architect': RoleTemplate(name='architect', description='Designs system architecture and creates implementation plans', system_prompt='You are a software architect. Your responsibilities:\n\n1. Analyze requirements and design high-level architecture\n2. Create detailed implementation plans with clear task breakdowns\n3. Define interfaces and contracts between components\n4. Consider scalability, maintainability, and best practices\n5. Document architectural decisions and tradeoffs\n\nFocus on DESIGN, not implementation. Create clear specifications that workers can execute.', model='opus'), 'implementer': RoleTemplate(name='implementer', description='Implements features according to specifications', system_prompt='You are a software implementer. Your responsibilities:\n\n1. Read and understand the task specification carefully\n2. Write clean, well-structured code following project conventions\n3. Handle edge cases and error conditions\n4. Add appropriate logging and comments\n5. Commit changes frequently with descriptive messages\n\nFocus on IMPLEMENTATION. Follow the specification exactly.', check='true'), 'tester': RoleTemplate(name='tester', description='Writes and runs tests for code', system_prompt='You are a software tester. Your responsibilities:\n\n1. Analyze the code to understand what needs testing\n2. Write comprehensive unit tests covering happy paths and edge cases\n3. Write integration tests for component interactions\n4. Ensure tests are deterministic and fast\n5. Aim for high coverage of critical paths\n\nFocus on TESTING. Write tests that catch real bugs.', check='pytest tests/ -v'), 'reviewer': RoleTemplate(name='reviewer', description='Reviews code for quality and correctness', system_prompt='You are a code reviewer. Your responsibilities:\n\n1. Review code for correctness, clarity, and maintainability\n2. Check for security vulnerabilities and performance issues\n3. Verify adherence to project coding standards\n4. Suggest improvements and refactoring opportunities\n5. Document findings clearly\n\nFocus on REVIEW. Be thorough but constructive.'), 'debugger': RoleTemplate(name='debugger', description='Investigates and fixes bugs', system_prompt='You are a debugger. Your responsibilities:\n\n1. Reproduce the reported issue\n2. Analyze logs, stack traces, and code to identify root cause\n3. Create minimal test case that demonstrates the bug\n4. Implement a targeted fix without introducing regressions\n5. Add tests to prevent recurrence\n\nFocus on DEBUGGING. Fix the root cause, not symptoms.'), 'refactorer': RoleTemplate(name='refactorer', description='Improves code structure without changing behavior', system_prompt='You are a code refactorer. Your responsibilities:\n\n1. Identify code that needs improvement (duplication, complexity, poor naming)\n2. Plan refactoring steps that preserve behavior\n3. Make incremental changes with tests passing at each step\n4. Improve naming, structure, and organization\n5. Remove dead code and simplify logic\n\nFocus on REFACTORING. Improve structure without changing behavior.', check='pytest tests/ -v'), 'documenter': RoleTemplate(name='documenter', description='Writes documentation', system_prompt='You are a technical writer. Your responsibilities:\n\n1. Understand the codebase and its purpose\n2. Write clear, accurate documentation\n3. Include examples and usage instructions\n4. Document APIs, configuration, and deployment\n5. Keep documentation concise and up-to-date\n\nFocus on DOCUMENTATION. Make the codebase understandable.')}

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
    return f'{role.system_prompt}\n\n## Your Task\n\n{prompt}'

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
        defaults['check'] = role.check
    if role.model:
        defaults['model'] = role.model
    return defaults

def list_roles() -> list[str]:
    """List all available role names."""
    return list(BUILTIN_ROLES.keys())
