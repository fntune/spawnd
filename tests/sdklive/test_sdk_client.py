"""Test ClaudeSDKClient for manager-style multi-turn conversations."""

import asyncio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, tool, create_sdk_mcp_server


async def test_client_basic():
    """Test ClaudeSDKClient for multi-turn."""
    print("\n=== Test: ClaudeSDKClient Basic ===")

    options = ClaudeAgentOptions(
        model="haiku",
        max_turns=5,
        allowed_tools=["Bash"],
        permission_mode="bypassPermissions",
    )

    async with ClaudeSDKClient(options=options) as client:
        # First query
        print("  Turn 1: Asking about cwd")
        async for message in client.process_query(prompt="What directory are we in? Use pwd."):
            if hasattr(message, "result"):
                print(f"  Result: {message.result[:60]}...")

        # Second query in same session
        print("  Turn 2: Follow-up")
        async for message in client.process_query(prompt="List files there."):
            if hasattr(message, "result"):
                print(f"  Result: {message.result[:60]}...")


async def test_builtin_tools_work():
    """Confirm built-in tools work fine."""
    print("\n=== Test: Built-in Tools ===")

    tool_calls = []

    async for message in query(
        prompt="Run 'echo SDK_TEST_42' and tell me the output.",
        options=ClaudeAgentOptions(
            allowed_tools=["Bash"],
            model="haiku",
            max_turns=3,
            permission_mode="bypassPermissions",
        )
    ):
        if hasattr(message, "content") and message.content:
            for block in message.content:
                if hasattr(block, "type") and block.type == "tool_use":
                    tool_calls.append(block.name)

        if hasattr(message, "result"):
            has_output = "SDK_TEST_42" in message.result
            print(f"  Tools called: {tool_calls}")
            print(f"  Output contains SDK_TEST_42: {has_output}")


async def test_streaming_output_format():
    """Document the exact streaming output format for integration."""
    print("\n=== Test: Streaming Format for Integration ===")

    from claude_agent_sdk import query, ClaudeAgentOptions

    print("  Messages received:")
    async for message in query(
        prompt="Say exactly: 'Hello from SDK'",
        options=ClaudeAgentOptions(model="haiku", max_turns=1)
    ):
        msg_type = type(message).__name__

        # Document useful fields
        info = []
        if hasattr(message, "session_id"):
            info.append(f"session_id={message.session_id[:8]}...")
        if hasattr(message, "total_cost_usd"):
            info.append(f"cost=${message.total_cost_usd:.4f}")
        if hasattr(message, "result"):
            info.append(f"result='{message.result[:30]}...'")
        if hasattr(message, "duration_ms"):
            info.append(f"duration={message.duration_ms}ms")

        print(f"    {msg_type}: {', '.join(info) if info else '(streaming data)'}")


async def test_cost_tracking():
    """Test cost tracking from result messages."""
    print("\n=== Test: Cost Tracking ===")

    from claude_agent_sdk import query, ClaudeAgentOptions

    costs = []
    async for message in query(
        prompt="Count to 5.",
        options=ClaudeAgentOptions(model="haiku", max_turns=1)
    ):
        if hasattr(message, "total_cost_usd"):
            costs.append(message.total_cost_usd)

    print(f"  Costs captured: {costs}")
    print(f"  Final cost: ${costs[-1] if costs else 0:.4f}")


async def test_env_and_cwd():
    """Test working directory and environment."""
    print("\n=== Test: Env and CWD ===")

    from claude_agent_sdk import query, ClaudeAgentOptions
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test file
        test_file = os.path.join(tmpdir, "test.txt")
        with open(test_file, "w") as f:
            f.write("SPAWND_TEST_CONTENT")

        async for message in query(
            prompt=f"Read the file test.txt and tell me its contents.",
            options=ClaudeAgentOptions(
                cwd=tmpdir,
                allowed_tools=["Read"],
                model="haiku",
                max_turns=2,
                permission_mode="bypassPermissions",
            )
        ):
            if hasattr(message, "result"):
                has_content = "SPAWND_TEST_CONTENT" in message.result
                print(f"  CWD worked: {has_content}")
                print(f"  Result: {message.result[:60]}...")


# Import query here for the tests that need it
from claude_agent_sdk import query


async def main():
    print("=" * 60)
    print("ClaudeSDKClient & Integration Tests")
    print("=" * 60)

    tests = [
        test_streaming_output_format,
        test_cost_tracking,
        test_builtin_tools_work,
        test_env_and_cwd,
        test_client_basic,
    ]

    for test in tests:
        try:
            await test()
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print("Complete")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
