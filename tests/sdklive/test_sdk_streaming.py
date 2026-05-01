"""Test SDK with streaming input mode for custom MCP tools."""

import asyncio
from typing import AsyncIterator
from claude_agent_sdk import (
    query,
    ClaudeSDKClient,
    ClaudeAgentOptions,
    tool,
    create_sdk_mcp_server,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)


async def streaming_prompt(text: str) -> AsyncIterator[str]:
    """Convert string to async generator for streaming input."""
    yield text


async def test_mcp_with_streaming_input():
    """Test custom MCP tool with streaming input (required for MCP)."""
    print("\n=== Test: MCP with Streaming Input ===")

    @tool("magic", "Returns 42 times input", {"n": int})
    async def magic(args: dict) -> dict:
        result = 42 * args.get("n", 1)
        print(f"  [Tool executed] 42 * {args['n']} = {result}")
        return {"content": [{"type": "text", "text": str(result)}]}

    server = create_sdk_mcp_server("calc", "1.0.0", [magic])

    options = ClaudeAgentOptions(
        mcp_servers={"calc": server},
        allowed_tools=["mcp__calc__magic"],
        model="haiku",
        max_turns=3,
        permission_mode="bypassPermissions",
    )

    # Use streaming input
    result_text = None
    async for message in query(
        prompt=streaming_prompt("Call magic with n=3. Just tell me the number."),
        options=options,
    ):
        if isinstance(message, ResultMessage):
            result_text = message.result
            print(f"  Result: {result_text}")


async def test_client_with_mcp():
    """Test ClaudeSDKClient with custom MCP tools."""
    print("\n=== Test: ClaudeSDKClient with MCP ===")

    @tool("greet", "Greet someone", {"name": str})
    async def greet(args: dict) -> dict:
        print(f"  [Tool executed] Greeting {args['name']}")
        return {"content": [{"type": "text", "text": f"Hello, {args['name']}!"}]}

    server = create_sdk_mcp_server("greet", "1.0.0", [greet])

    options = ClaudeAgentOptions(
        mcp_servers={"greet": server},
        allowed_tools=["mcp__greet__greet"],
        model="haiku",
        max_turns=3,
        permission_mode="bypassPermissions",
    )

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query("Call greet with name='World'")

            # Don't break early - let loop complete
            found_result = False
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content or []:
                        if isinstance(block, TextBlock):
                            print(f"  Claude: {block.text[:60]}...")
                if isinstance(message, ResultMessage):
                    found_result = True
                    print(f"  Done: {message.result[:60]}...")

    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}")


async def test_coordination_flow():
    """Test spawnd-style coordination with streaming input."""
    print("\n=== Test: Coordination Flow ===")

    state = {"completed": False, "progress": []}

    @tool("report_progress", "Report progress", {"status": str})
    async def report_progress(args: dict) -> dict:
        state["progress"].append(args["status"])
        print(f"  [progress] {args['status']}")
        return {"content": [{"type": "text", "text": "Recorded."}]}

    @tool("mark_complete", "Mark task complete", {"summary": str})
    async def mark_complete(args: dict) -> dict:
        state["completed"] = True
        print(f"  [complete] {args['summary']}")
        return {"content": [{"type": "text", "text": "Done."}]}

    server = create_sdk_mcp_server(
        "coord", "1.0.0", [report_progress, mark_complete]
    )

    options = ClaudeAgentOptions(
        mcp_servers={"coord": server},
        allowed_tools=["mcp__coord__report_progress", "mcp__coord__mark_complete"],
        model="haiku",
        max_turns=6,
        permission_mode="bypassPermissions",
    )

    prompt = """Task: Calculate 2+2.
Steps:
1. Call report_progress with status "Starting"
2. Call report_progress with status "Result is 4"
3. Call mark_complete with summary "Calculated 2+2=4"
Execute now."""

    async for message in query(
        prompt=streaming_prompt(prompt),
        options=options,
    ):
        if isinstance(message, ResultMessage):
            print(f"  Final state: completed={state['completed']}, progress={state['progress']}")


async def test_without_streaming():
    """Control test: Regular string prompt (no MCP)."""
    print("\n=== Test: Regular Prompt (no MCP) ===")

    async for message in query(
        prompt="What is 2+2? Just the number.",
        options=ClaudeAgentOptions(model="haiku", max_turns=1),
    ):
        if isinstance(message, ResultMessage):
            print(f"  Result: {message.result}")


async def main():
    print("=" * 60)
    print("SDK Streaming Input Tests")
    print("=" * 60)

    # Control test first
    await test_without_streaming()

    # Then MCP tests with streaming
    try:
        await test_mcp_with_streaming_input()
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

    try:
        await test_coordination_flow()
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

    try:
        await test_client_with_mcp()
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
