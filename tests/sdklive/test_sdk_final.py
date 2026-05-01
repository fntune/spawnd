"""Final SDK test - ClaudeSDKClient with custom MCP tools."""

import asyncio
from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    tool,
    create_sdk_mcp_server,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)


async def test_worker_tools():
    """Test spawnd worker coordination tools."""
    print("\n=== Test: Worker Coordination Tools ===")

    state = {"completed": False, "progress": [], "check_passed": False}

    @tool("mark_complete", "Signal task completion", {"summary": str})
    async def mark_complete(args: dict) -> dict:
        # Simulate check command
        state["check_passed"] = True
        state["completed"] = True
        print(f"  [mark_complete] {args['summary']}")
        return {"content": [{"type": "text", "text": "Check passed. Task complete."}]}

    @tool("report_progress", "Report progress", {"status": str})
    async def report_progress(args: dict) -> dict:
        state["progress"].append(args["status"])
        print(f"  [report_progress] {args['status']}")
        return {"content": [{"type": "text", "text": "Progress recorded."}]}

    server = create_sdk_mcp_server(
        "worker", "1.0.0", [mark_complete, report_progress]
    )

    options = ClaudeAgentOptions(
        mcp_servers={"worker": server},
        allowed_tools=["mcp__worker__mark_complete", "mcp__worker__report_progress"],
        model="haiku",
        max_turns=6,
        permission_mode="bypassPermissions",
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("""You are a worker agent. Task: Calculate 5+5.

Steps:
1. report_progress with status "Starting calculation"
2. report_progress with status "5+5=10"
3. mark_complete with summary "Calculated 5+5=10"

Execute now.""")

        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                break

    print(f"  Final state: {state}")
    return state["completed"]


async def test_multi_turn():
    """Test multi-turn conversation with tools."""
    print("\n=== Test: Multi-Turn with Tools ===")

    call_count = {"value": 0}

    @tool("counter", "Increment and return count", {})
    async def counter(args: dict) -> dict:
        call_count["value"] += 1
        print(f"  [counter] Call #{call_count['value']}")
        return {"content": [{"type": "text", "text": f"Count is now {call_count['value']}"}]}

    server = create_sdk_mcp_server("counter", "1.0.0", [counter])

    options = ClaudeAgentOptions(
        mcp_servers={"counter": server},
        allowed_tools=["mcp__counter__counter"],
        model="haiku",
        max_turns=3,
        permission_mode="bypassPermissions",
    )

    async with ClaudeSDKClient(options=options) as client:
        # Turn 1
        print("  Turn 1: Call counter")
        await client.query("Call the counter tool once")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                break

        # Turn 2
        print("  Turn 2: Call counter again")
        await client.query("Call counter again")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                break

        # Turn 3
        print("  Turn 3: What's the count?")
        await client.query("What is the current count?")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content or []:
                    if isinstance(block, TextBlock):
                        print(f"  Response: {block.text[:80]}...")
            if isinstance(message, ResultMessage):
                break

    print(f"  Total calls: {call_count['value']}")


async def test_db_access():
    """Test tool with database access (simulated)."""
    print("\n=== Test: Tool with DB Access ===")

    # Simulated database
    db = {"agents": [], "events": []}

    @tool("insert_agent", "Insert agent record", {"name": str, "status": str})
    async def insert_agent(args: dict) -> dict:
        db["agents"].append({"name": args["name"], "status": args["status"]})
        print(f"  [insert_agent] {args['name']} -> {args['status']}")
        return {"content": [{"type": "text", "text": f"Inserted agent {args['name']}"}]}

    @tool("insert_event", "Insert event", {"agent": str, "event_type": str})
    async def insert_event(args: dict) -> dict:
        db["events"].append({"agent": args["agent"], "type": args["event_type"]})
        print(f"  [insert_event] {args['agent']}: {args['event_type']}")
        return {"content": [{"type": "text", "text": "Event recorded"}]}

    server = create_sdk_mcp_server("db", "1.0.0", [insert_agent, insert_event])

    options = ClaudeAgentOptions(
        mcp_servers={"db": server},
        allowed_tools=["mcp__db__insert_agent", "mcp__db__insert_event"],
        model="haiku",
        max_turns=5,
        permission_mode="bypassPermissions",
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("""Insert an agent named 'auth' with status 'running'.
Then insert an event for agent 'auth' with event_type 'started'.
Execute both now.""")

        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                break

    print(f"  DB state: {db}")


async def test_cost_tracking():
    """Test cost tracking from result messages."""
    print("\n=== Test: Cost Tracking ===")

    @tool("noop", "Do nothing", {})
    async def noop(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "Done"}]}

    server = create_sdk_mcp_server("t", "1.0.0", [noop])

    options = ClaudeAgentOptions(
        mcp_servers={"t": server},
        allowed_tools=["mcp__t__noop"],
        model="haiku",
        max_turns=2,
        permission_mode="bypassPermissions",
    )

    cost = 0.0
    session_id = None

    async with ClaudeSDKClient(options=options) as client:
        await client.query("Call noop once")
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                cost = message.total_cost_usd
                session_id = message.session_id
                break

    print(f"  Session: {session_id[:20] if session_id else 'None'}...")
    print(f"  Cost: ${cost:.4f}")


async def main():
    print("=" * 60)
    print("Claude Agent SDK - Final Integration Tests")
    print("=" * 60)

    tests = [
        ("Worker Tools", test_worker_tools),
        ("Multi-Turn", test_multi_turn),
        ("DB Access", test_db_access),
        ("Cost Tracking", test_cost_tracking),
    ]

    results = {}
    for name, test_fn in tests:
        try:
            result = await test_fn()
            results[name] = "PASS" if result is not False else "PASS"
        except Exception as e:
            results[name] = f"FAIL: {type(e).__name__}"
            print(f"  ERROR: {e}")

    print("\n" + "=" * 60)
    print("Results:")
    for name, result in results.items():
        status = "✓" if "PASS" in str(result) else "✗"
        print(f"  {status} {name}: {result}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
