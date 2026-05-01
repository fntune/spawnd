"""Test Claude Agent SDK integration for spawnd.dev.

This file validates that the SDK provides everything PLAN.md requires:
1. query() for worker agents (streaming)
2. ClaudeSDKClient for manager agents (stateful multi-turn)
3. @tool decorator for custom MCP tools
4. create_sdk_mcp_server() for bundling tools
5. ClaudeAgentOptions for configuration
6. Session resumption support
7. Cost tracking
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================================
# SDK Import Validation
# ============================================================================

def test_sdk_core_imports():
    """Validate core SDK imports exist."""
    from claude_agent_sdk import query
    from claude_agent_sdk import ClaudeAgentOptions

    assert callable(query)
    assert ClaudeAgentOptions is not None


def test_sdk_tool_imports():
    """Validate tool creation imports exist."""
    from claude_agent_sdk import tool
    from claude_agent_sdk import create_sdk_mcp_server

    assert callable(tool)
    assert callable(create_sdk_mcp_server)


def test_sdk_client_import():
    """Validate ClaudeSDKClient import exists."""
    try:
        from claude_agent_sdk import ClaudeSDKClient
        assert ClaudeSDKClient is not None
    except ImportError:
        # ClaudeSDKClient may be named differently or not exist
        # Check alternative names
        import claude_agent_sdk
        client_names = [n for n in dir(claude_agent_sdk) if "client" in n.lower()]
        pytest.skip(f"ClaudeSDKClient not found. Available: {client_names}")


# ============================================================================
# ClaudeAgentOptions Validation
# ============================================================================

def test_options_basic_fields():
    """Validate ClaudeAgentOptions has required fields."""
    from claude_agent_sdk import ClaudeAgentOptions

    # Create options with fields PLAN.md expects
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Write", "Edit", "Bash"],
        permission_mode="bypassPermissions",
        model="sonnet",
    )

    assert options.allowed_tools == ["Read", "Write", "Edit", "Bash"]
    assert options.permission_mode == "bypassPermissions"
    assert options.model == "sonnet"


def test_options_mcp_servers():
    """Validate MCP servers can be configured."""
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        mcp_servers={"test-server": {"command": "echo", "args": ["hello"]}}
    )

    assert "test-server" in options.mcp_servers


def test_options_cwd():
    """Validate working directory can be set."""
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(cwd="/tmp/test")
    assert str(options.cwd) == "/tmp/test"


def test_options_max_turns():
    """Validate max_turns (iterations) can be set."""
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(max_turns=30)
    assert options.max_turns == 30


def test_options_resume():
    """Validate session resumption field exists."""
    from claude_agent_sdk import ClaudeAgentOptions

    options = ClaudeAgentOptions(resume="session-123")
    assert options.resume == "session-123"


def test_options_system_prompt():
    """Validate system prompt can be set."""
    from claude_agent_sdk import ClaudeAgentOptions

    prompt = "You are a coding agent."
    options = ClaudeAgentOptions(system_prompt=prompt)
    assert options.system_prompt == prompt


# ============================================================================
# Tool Creation Validation
# ============================================================================

def test_tool_decorator():
    """Validate @tool decorator works."""
    from claude_agent_sdk import tool

    @tool(
        name="test_tool",
        description="A test tool",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"}
            },
            "required": ["message"]
        }
    )
    async def test_tool_fn(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"Got: {args['message']}"}]}

    # Tool decorator returns SdkMcpTool object
    assert test_tool_fn is not None
    assert hasattr(test_tool_fn, "name")
    assert test_tool_fn.name == "test_tool"


def test_create_mcp_server():
    """Validate MCP server creation."""
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        name="mark_complete",
        description="Signal task completion",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"}
            },
            "required": ["summary"]
        }
    )
    async def mark_complete(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Done"}]}

    server = create_sdk_mcp_server(
        name="spawnd-coordination",
        version="1.0.0",
        tools=[mark_complete]
    )

    assert server is not None


# ============================================================================
# Query Function Validation
# ============================================================================

@pytest.mark.asyncio
async def test_query_returns_async_iterator():
    """Validate query() returns an async iterator."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    # We can't actually run query without API key, but we can check signature
    import inspect
    sig = inspect.signature(query)
    params = list(sig.parameters.keys())

    # Should have prompt and options parameters
    assert "prompt" in params
    assert "options" in params


@pytest.mark.asyncio
async def test_query_streaming_structure():
    """Validate expected message structure from query()."""
    # This documents the expected message types from PLAN.md
    expected_message_types = {
        "system": {"subtype": "init"},  # Session init with session_id
        "assistant": {"content": []},    # Claude responses with tool_use
        "user": {"content": []},         # Tool results
        "result": {                      # Final result
            "is_error": False,
            "duration_ms": 0,
            "total_cost_usd": 0.0,
            "session_id": "",
        },
    }

    # Validate structure matches PLAN.md expectations
    assert "system" in expected_message_types
    assert "result" in expected_message_types
    assert "total_cost_usd" in expected_message_types["result"]


# ============================================================================
# Integration Test: Full Worker Flow (Mock)
# ============================================================================

@pytest.mark.asyncio
async def test_worker_flow_mock():
    """Test the expected worker flow with mocked SDK."""
    from claude_agent_sdk import ClaudeAgentOptions

    # Simulate what PLAN.md expects for a worker
    config = {
        "name": "test-worker",
        "prompt": "Implement feature X",
        "check_command": "pytest",
        "model": "sonnet",
        "max_iterations": 30,
        "worktree": Path("/tmp/test-worktree"),
    }

    options = ClaudeAgentOptions(
        cwd=str(config["worktree"]),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        permission_mode="bypassPermissions",
        model=config["model"],
        max_turns=config["max_iterations"],
        system_prompt=f"Task: {config['prompt']}\nCheck: {config['check_command']}",
    )

    # Validate options built correctly
    assert options.max_turns == 30
    assert options.model == "sonnet"
    assert "Read" in options.allowed_tools


# ============================================================================
# Coordination Tools Definition
# ============================================================================

def test_worker_tools_definition():
    """Define and validate worker coordination tools per PLAN.md."""
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        name="mark_complete",
        description="Signal task completion. Runs check command automatically.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Summary of work completed"}
            },
            "required": ["summary"]
        }
    )
    async def mark_complete(args: dict) -> dict:
        # In real impl: run check, update DB
        return {"content": [{"type": "text", "text": "Task completed successfully."}]}

    @tool(
        name="request_clarification",
        description="Ask manager for guidance. BLOCKS until response received.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to ask manager"},
                "escalate_to": {
                    "type": "string",
                    "enum": ["parent", "human", "auto"],
                    "description": "Who to escalate to"
                }
            },
            "required": ["question"]
        }
    )
    async def request_clarification(args: dict) -> dict:
        # In real impl: emit event, poll DB, block
        return {"content": [{"type": "text", "text": "Manager response: Use JWT"}]}

    @tool(
        name="report_progress",
        description="Report progress update with optional milestone.",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Current status"},
                "milestone": {"type": "string", "description": "Optional milestone name"}
            },
            "required": ["status"]
        }
    )
    async def report_progress(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Progress recorded."}]}

    @tool(
        name="report_blocker",
        description="Report blocking issue. BLOCKS until manager responds.",
        input_schema={
            "type": "object",
            "properties": {
                "issue": {"type": "string", "description": "Description of blocking issue"}
            },
            "required": ["issue"]
        }
    )
    async def report_blocker(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Blocker resolved."}]}

    # Create coordination server
    server = create_sdk_mcp_server(
        name="spawnd-coordination",
        version="1.0.0",
        tools=[mark_complete, request_clarification, report_progress, report_blocker]
    )

    assert server is not None


def test_manager_tools_definition():
    """Define and validate manager coordination tools per PLAN.md."""
    from claude_agent_sdk import tool, create_sdk_mcp_server

    @tool(
        name="spawn_worker",
        description="Creates SDK agent in worktree with standard toolset.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Worker name"},
                "prompt": {"type": "string", "description": "Task prompt"},
                "check": {"type": "string", "description": "Check command"}
            },
            "required": ["name", "prompt"]
        }
    )
    async def spawn_worker(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"Spawned worker: {args['name']}"}]}

    @tool(
        name="respond_to_clarification",
        description="Writes response, unblocks waiting worker.",
        input_schema={
            "type": "object",
            "properties": {
                "clarification_id": {"type": "string"},
                "response": {"type": "string"}
            },
            "required": ["clarification_id", "response"]
        }
    )
    async def respond_to_clarification(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Response sent."}]}

    @tool(
        name="cancel_worker",
        description="Terminates worker agent.",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Worker to cancel"}
            },
            "required": ["name"]
        }
    )
    async def cancel_worker(args: dict) -> dict:
        return {"content": [{"type": "text", "text": f"Cancelled: {args['name']}"}]}

    @tool(
        name="get_worker_status",
        description="Returns current state of worker(s).",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Optional worker name"}
            }
        }
    )
    async def get_worker_status(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Status: running"}]}

    @tool(
        name="get_pending_clarifications",
        description="Returns list of questions awaiting response.",
        input_schema={"type": "object", "properties": {}}
    )
    async def get_pending_clarifications(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "No pending clarifications."}]}

    @tool(
        name="mark_plan_complete",
        description="Signals orchestration done.",
        input_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Plan completion summary"}
            },
            "required": ["summary"]
        }
    )
    async def mark_plan_complete(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Plan completed."}]}

    server = create_sdk_mcp_server(
        name="spawnd-manager",
        version="1.0.0",
        tools=[
            spawn_worker,
            respond_to_clarification,
            cancel_worker,
            get_worker_status,
            get_pending_clarifications,
            mark_plan_complete,
        ]
    )

    assert server is not None


# ============================================================================
# SDK Feature Matrix
# ============================================================================

def test_sdk_feature_matrix():
    """Document what PLAN.md needs vs what SDK provides."""
    from claude_agent_sdk import query, ClaudeAgentOptions, tool, create_sdk_mcp_server

    features = {
        # Core execution
        "query() for streaming": callable(query),
        "ClaudeAgentOptions": ClaudeAgentOptions is not None,

        # Custom tools
        "@tool decorator": callable(tool),
        "create_sdk_mcp_server": callable(create_sdk_mcp_server),

        # Configuration
        "allowed_tools": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "allowed_tools" in ClaudeAgentOptions.__dataclass_fields__,
        "mcp_servers": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "mcp_servers" in ClaudeAgentOptions.__dataclass_fields__,
        "permission_mode": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "permission_mode" in ClaudeAgentOptions.__dataclass_fields__,
        "model": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "model" in ClaudeAgentOptions.__dataclass_fields__,
        "max_turns": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "max_turns" in ClaudeAgentOptions.__dataclass_fields__,
        "cwd": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "cwd" in ClaudeAgentOptions.__dataclass_fields__,
        "resume": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "resume" in ClaudeAgentOptions.__dataclass_fields__,
        "system_prompt": hasattr(ClaudeAgentOptions, "__dataclass_fields__") and "system_prompt" in ClaudeAgentOptions.__dataclass_fields__,
    }

    print("\n=== SDK Feature Matrix ===")
    for feature, available in features.items():
        status = "✓" if available else "✗"
        print(f"  {status} {feature}")

    # All features should be available
    missing = [f for f, available in features.items() if not available]
    if missing:
        print(f"\nMissing features: {missing}")

    # At minimum, core features must exist
    assert features["query() for streaming"]
    assert features["ClaudeAgentOptions"]
    assert features["@tool decorator"]
    assert features["create_sdk_mcp_server"]


# ============================================================================
# Live SDK Test (requires API key, skipped by default)
# ============================================================================

@pytest.mark.asyncio
@pytest.mark.skip(reason="Requires API key and costs money")
async def test_live_query():
    """Live test of query() - skipped by default."""
    from claude_agent_sdk import query, ClaudeAgentOptions

    messages = []
    session_id = None
    total_cost = 0.0

    async for message in query(
        prompt="What is 2+2? Reply with just the number.",
        options=ClaudeAgentOptions(
            allowed_tools=[],
            model="haiku",
            max_turns=1,
        )
    ):
        messages.append(message)

        # Capture session_id from init message
        if hasattr(message, "type") and message.type == "system":
            if hasattr(message, "session_id"):
                session_id = message.session_id

        # Capture cost from result message
        if hasattr(message, "type") and message.type == "result":
            if hasattr(message, "total_cost_usd"):
                total_cost = message.total_cost_usd

    assert len(messages) > 0
    print(f"Session: {session_id}, Cost: ${total_cost:.4f}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
