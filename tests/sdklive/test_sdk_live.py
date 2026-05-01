"""Live SDK tests - no mocking, real API calls."""

import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions, tool, create_sdk_mcp_server


async def test_basic_query():
    """Test basic query with streaming."""
    print("\n=== Test: Basic Query ===")

    session_id = None
    total_cost = 0.0
    result_text = None

    async for message in query(
        prompt="What is 2+2? Reply with just the number.",
        options=ClaudeAgentOptions(
            allowed_tools=[],
            model="haiku",
            max_turns=1,
        )
    ):
        print(f"  Message: {type(message).__name__}")

        if hasattr(message, "session_id"):
            session_id = message.session_id
        if hasattr(message, "total_cost_usd"):
            total_cost = message.total_cost_usd
        if hasattr(message, "result"):
            result_text = message.result

    print(f"  Result: {result_text}")
    print(f"  Session: {session_id}")
    print(f"  Cost: ${total_cost:.4f}")


async def test_query_with_bash():
    """Test query with Bash tool."""
    print("\n=== Test: Query with Bash ===")

    async for message in query(
        prompt="Run 'echo hello' and show me the output",
        options=ClaudeAgentOptions(
            allowed_tools=["Bash"],
            model="haiku",
            max_turns=3,
            permission_mode="bypassPermissions",
        )
    ):
        msg_type = type(message).__name__

        if hasattr(message, "content") and message.content:
            for block in message.content:
                if hasattr(block, "type"):
                    if block.type == "tool_use":
                        print(f"  {msg_type} -> Tool: {block.name}")
                    elif block.type == "text" and block.text:
                        text = block.text[:60].replace("\n", " ")
                        print(f"  {msg_type} -> {text}...")


async def test_custom_tool():
    """Test custom MCP tool."""
    print("\n=== Test: Custom MCP Tool ===")

    # Define tool with simplified API
    @tool("magic_number", "Returns 42 times the input", {"multiplier": int})
    async def magic_number(args: dict) -> dict:
        result = 42 * args.get("multiplier", 1)
        print(f"  [Tool executed] 42 * {args.get('multiplier')} = {result}")
        return {"content": [{"type": "text", "text": str(result)}]}

    server = create_sdk_mcp_server(
        name="test",
        version="1.0.0",
        tools=[magic_number]
    )

    result_text = None
    async for message in query(
        prompt="Call magic_number with multiplier=3. Just tell me the result number.",
        options=ClaudeAgentOptions(
            mcp_servers={"test": server},
            allowed_tools=["mcp__test__magic_number"],
            model="haiku",
            max_turns=3,
            permission_mode="bypassPermissions",
        )
    ):
        if hasattr(message, "result"):
            result_text = message.result

    print(f"  Final: {result_text}")


async def test_coordination_tools():
    """Test spawnd-style coordination tools."""
    print("\n=== Test: Coordination Tools ===")

    state = {"completed": False, "progress": []}

    @tool("mark_complete", "Signal task completion", {"summary": str})
    async def mark_complete(args: dict) -> dict:
        state["completed"] = True
        print(f"  [mark_complete] {args['summary']}")
        return {"content": [{"type": "text", "text": "Task marked complete."}]}

    @tool("report_progress", "Report progress", {"status": str})
    async def report_progress(args: dict) -> dict:
        state["progress"].append(args["status"])
        print(f"  [report_progress] {args['status']}")
        return {"content": [{"type": "text", "text": "Progress recorded."}]}

    server = create_sdk_mcp_server(
        name="coord",
        version="1.0.0",
        tools=[mark_complete, report_progress]
    )

    async for message in query(
        prompt="""Calculate 10+20.
1. Call report_progress with status "Starting calculation"
2. Call report_progress with status "Result is 30"
3. Call mark_complete with summary "Calculated 10+20=30"
Do these steps now.""",
        options=ClaudeAgentOptions(
            mcp_servers={"coord": server},
            allowed_tools=[
                "mcp__coord__mark_complete",
                "mcp__coord__report_progress",
            ],
            model="haiku",
            max_turns=6,
            permission_mode="bypassPermissions",
        )
    ):
        pass

    print(f"  State: completed={state['completed']}, progress={state['progress']}")


async def test_session_resume():
    """Test session persistence and resume."""
    print("\n=== Test: Session Resume ===")

    # Create session with a fact to remember
    session_id = None
    async for message in query(
        prompt="Remember: The secret code is ALPHA-7742. Just say OK.",
        options=ClaudeAgentOptions(model="haiku", max_turns=1)
    ):
        if hasattr(message, "session_id"):
            session_id = message.session_id

    print(f"  Session created: {session_id[:20]}...")

    if not session_id:
        print("  SKIP: No session ID")
        return

    # Resume and recall
    result = None
    async for message in query(
        prompt="What was the secret code I told you?",
        options=ClaudeAgentOptions(
            resume=session_id,
            model="haiku",
            max_turns=1,
        )
    ):
        if hasattr(message, "result"):
            result = message.result

    remembered = "7742" in (result or "") or "ALPHA" in (result or "")
    print(f"  Response: {result[:60] if result else 'None'}...")
    print(f"  Remembered: {remembered}")


async def test_message_structure():
    """Document message types and fields."""
    print("\n=== Test: Message Structure ===")

    @tool("noop", "Do nothing", {})
    async def noop(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Done"}]}

    server = create_sdk_mcp_server("t", "1.0.0", [noop])

    seen = {}
    async for message in query(
        prompt="Call the noop tool once.",
        options=ClaudeAgentOptions(
            mcp_servers={"t": server},
            allowed_tools=["mcp__t__noop"],
            model="haiku",
            max_turns=3,
            permission_mode="bypassPermissions",
        )
    ):
        msg_type = type(message).__name__
        fields = [a for a in dir(message) if not a.startswith("_") and not callable(getattr(message, a, None))]
        seen[msg_type] = fields[:8]

    for msg_type, fields in seen.items():
        print(f"  {msg_type}: {fields}")


async def main():
    print("=" * 60)
    print("Claude Agent SDK - Live Tests")
    print("=" * 60)

    tests = [
        test_basic_query,
        test_query_with_bash,
        test_custom_tool,
        test_coordination_tools,
        test_session_resume,
        test_message_structure,
    ]

    for test in tests:
        try:
            await test()
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("Tests complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
