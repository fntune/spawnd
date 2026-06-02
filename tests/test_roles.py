"""Tests for roles module."""
from spawnd.roles import BUILTIN_ROLES, apply_role, get_role, get_role_defaults, list_roles

def test_list_roles():
    """Test listing available roles."""
    roles = list_roles()
    assert len(roles) == 7
    assert 'architect' in roles
    assert 'implementer' in roles
    assert 'tester' in roles

def test_get_role():
    """Test getting a role by name."""
    role = get_role('architect')
    assert role is not None
    assert role.name == 'architect'
    assert 'architect' in role.description.lower()
    assert role.model == 'opus'
    assert get_role('unknown') is None

def test_get_role_defaults():
    """Test getting role defaults."""
    defaults = get_role_defaults('tester')
    assert 'check' in defaults
    assert 'pytest' in defaults['check']
    defaults = get_role_defaults('architect')
    assert defaults.get('model') == 'opus'
    defaults = get_role_defaults('unknown')
    assert defaults == {}

def test_apply_role():
    """Test applying role to prompt."""
    prompt = 'Implement user authentication'
    result = apply_role(prompt, 'implementer')
    assert 'Your Task' in result
    assert prompt in result
    assert 'software implementer' in result.lower()
    result = apply_role(prompt, 'unknown')
    assert result == prompt

def test_all_roles_have_required_fields():
    """Test all roles have required fields."""
    for name, role in BUILTIN_ROLES.items():
        assert role.name == name
        assert role.description
        assert role.system_prompt
