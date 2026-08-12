from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.providers.faux import faux_assistant_message, faux_tool_call, register_faux_provider
from harn_ai.stream import stream_simple
from harn_ai.types import TextContent
from mcp import StdioServerParameters
from mcp import types as mcp_types

from aibb.harness import AibbHarnessEngine
from aibb.protocol.client import StdioMcpBridge
from aibb.sessions import SessionStore

SYSTEM_PROMPT = "SLOWBOARD-CONTEXT-v1\nNo framework text may precede or follow this prompt."


def text_from_last_message(engine: AibbHarnessEngine) -> str:
    message = engine.messages[-1]
    return "".join(block.text for block in message.content if getattr(block, "type", None) == "text")


@pytest.mark.asyncio
async def test_harn_core_uses_exact_context_real_stdio_mcp_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile_home = tmp_path / "home"
    hostile_project = tmp_path / "project"
    (hostile_home / ".harn" / "agent").mkdir(parents=True)
    hostile_project.mkdir()
    (hostile_home / ".harn" / "agent" / "SYSTEM.md").write_text("INJECTED GLOBAL", encoding="utf-8")
    (hostile_project / "AGENTS.md").write_text("INJECTED PROJECT", encoding="utf-8")
    monkeypatch.setenv("HOME", str(hostile_home))
    monkeypatch.chdir(hostile_project)

    registration = register_faux_provider({"api": "aibb-spike-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                faux_tool_call("archive_status", {}, {"id": "status-call"}),
                {"stopReason": "toolUse", "responseId": "response-tool"},
            ),
            faux_assistant_message(
                "The archive is ready.",
                {"stopReason": "stop", "responseId": "response-ready"},
            ),
            faux_assistant_message(
                "Welcome received after resumption.",
                {"stopReason": "stop", "responseId": "response-resumed"},
            ),
        ]
    )
    model = registration.models[0]
    captured_contexts: list[dict[str, Any]] = []

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    fixture_server = Path(__file__).parent / "fixtures" / "spike_mcp_server.py"
    parameters = StdioServerParameters(command=sys.executable, args=[str(fixture_server)])

    try:
        async with StdioMcpBridge(parameters) as bridge:
            tools = await bridge.agent_tools()
            assert [tool.name for tool in tools] == ["archive_status"]

            engine = AibbHarnessEngine(
                model=model,
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
                stream_fn=recording_stream,
                provider_state={"opaque_continuation": "state-1"},
            )
            event_types: list[str] = []
            engine.agent.subscribe(lambda event, _signal: event_types.append(event.type))
            await engine.send_curator_message("Welcome. Explore when you are ready.")

            assert text_from_last_message(engine) == "The archive is ready."
            assert registration.state["callCount"] == 2
            assert "tool_execution_start" in event_types
            assert "tool_execution_end" in event_types
            assert "message_end" in event_types
            assert all(context["systemPrompt"] == SYSTEM_PROMPT for context in captured_contexts)
            assert all(
                [tool["name"] for tool in context["tools"]] == ["archive_status"]
                for context in captured_contexts
            )
            assert "INJECTED GLOBAL" not in json.dumps(captured_contexts)
            assert "INJECTED PROJECT" not in json.dumps(captured_contexts)
            tool_results = [
                message for message in captured_contexts[-1]["messages"] if message["role"] == "toolResult"
            ]
            assert len(tool_results) == 1
            assert json.loads(tool_results[0]["content"][0]["text"]) == {
                "published_contributions": 3,
                "status": "ready",
            }

            store = SessionStore(tmp_path / "sessions" / "run-spike", "run-spike")
            snapshot = engine.snapshot()
            store.append(
                "engine_snapshot",
                {"engine": snapshot.model_dump(mode="json")},
                "private_provider",
            )
            store.write_checkpoint(snapshot)
            restored_snapshot = store.read_checkpoint().engine
            resumed = AibbHarnessEngine.from_snapshot(
                restored_snapshot,
                tools=tools,
                stream_fn=recording_stream,
            )
            assert resumed.snapshot() == engine.snapshot()
            assert resumed.provider_state == {"opaque_continuation": "state-1"}

            await resumed.send_curator_message("I am still here.")
            assert text_from_last_message(resumed) == "Welcome received after resumption."
            assert registration.state["callCount"] == 3
            assert captured_contexts[-1]["messages"][: len(restored_snapshot.messages)] == restored_snapshot.messages
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_curator_can_queue_steering_while_tool_is_running() -> None:
    registration = register_faux_provider({"api": "aibb-steering-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                faux_tool_call("slow_archive_read", {}, {"id": "slow-call"}),
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I received the curator's note.", {"stopReason": "stop"}),
        ]
    )
    started = asyncio.Event()
    release = asyncio.Event()
    captured_contexts: list[dict[str, Any]] = []

    async def execute(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        started.set()
        await release.wait()
        return AgentToolResult(content=[TextContent(text="read complete")], details={})

    tool = AgentTool(
        name="slow_archive_read",
        label="Slow archive read",
        description="A blocking archive read used to prove safe steering.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=execute,
        executionMode="sequential",
    )

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[tool],
            stream_fn=recording_stream,
        )
        run = asyncio.create_task(engine.send_curator_message("Please begin."))
        await asyncio.wait_for(started.wait(), timeout=2)
        engine.steer("Please also consider the provenance.")
        release.set()
        await asyncio.wait_for(run, timeout=2)

        assert engine.agent.toolExecution == "parallel"
        assert registration.state["callCount"] == 2
        assert text_from_last_message(engine) == "I received the curator's note."
        visible_text = json.dumps(captured_contexts[-1]["messages"])
        assert "[Administrator]\\nPlease also consider the provenance." in visible_text
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_independent_mcp_research_calls_overlap_in_one_model_turn() -> None:
    registration = register_faux_provider({"api": "aibb-parallel-research-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                [
                    faux_tool_call("research_web", {"query": "first question"}, {"id": "research-first"}),
                    faux_tool_call("research_web", {"query": "second question"}, {"id": "research-second"}),
                    faux_tool_call("research_web", {"query": "third question"}, {"id": "research-third"}),
                    faux_tool_call("research_web", {"query": "fourth question"}, {"id": "research-fourth"}),
                ],
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("Both research memos arrived.", {"stopReason": "stop"}),
        ]
    )
    both_started = asyncio.Event()
    started: list[str] = []

    class FakeSession:
        async def list_tools(self) -> Any:
            return type(
                "ListedTools",
                (),
                {
                    "tools": [
                        mcp_types.Tool(
                            name="research_web",
                            title="Research the web",
                            description="Return a cited research memo.",
                            inputSchema={
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        ),
                        mcp_types.Tool(
                            name="save_post",
                            title="Save post",
                            description="Save one post.",
                            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                        ),
                    ]
                },
            )()

        async def call_tool(self, name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
            assert name == "research_web"
            started.append(str(arguments["query"]))
            if len(started) == 3:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=f"memo for {arguments['query']}")],
                structuredContent={"query": arguments["query"]},
            )

    bridge = StdioMcpBridge(StdioServerParameters(command="unused"))
    bridge._session = FakeSession()  # type: ignore[assignment]

    try:
        tools = await bridge.agent_tools()
        assert {tool.name: tool.executionMode for tool in tools} == {
            "research_web": "parallel",
            "save_post": "sequential",
        }
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            stream_fn=lambda model, context, options: stream_simple(model, context, options),
        )

        await asyncio.wait_for(engine.send_curator_message("Research both questions."), timeout=2)

        assert started == ["first question", "second question", "third question"]
        assert text_from_last_message(engine) == "Both research memos arrived."
        visible_messages = json.dumps(
            [message.model_dump(mode="json", by_alias=True, exclude_none=True) for message in engine.messages]
        )
        assert "At most 3 independent research requests" in visible_messages
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_terminal_turn_stops_after_persisted_tool_result() -> None:
    registration = register_faux_provider({"api": "aibb-terminal-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                faux_tool_call("conclude_visit", {}, {"id": "conclude-call"}),
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("This response must never be requested.", {"stopReason": "stop"}),
        ]
    )
    concluded = False

    async def execute(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        nonlocal concluded
        concluded = True
        return AgentToolResult(
            content=[TextContent(text='{"status":"concluded"}')],
            details={"status": "concluded"},
        )

    tool = AgentTool(
        name="conclude_visit",
        label="Conclude visit",
        description="Conclude the visit.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        execute=execute,
        executionMode="sequential",
    )

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[tool],
            stream_fn=lambda model, context, options: stream_simple(model, context, options),
            should_stop_after_turn=lambda _engine: concluded,
        )
        await engine.send_curator_message("Explore, then conclude when ready.")

        assert registration.state["callCount"] == 1
        assert engine.messages[-1].role == "toolResult"
        assert text_from_last_message(engine) == '{"status":"concluded"}'
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_dependent_slowboard_tools_are_not_executed_from_one_speculative_batch() -> None:
    registration = register_faux_provider({"api": "aibb-ordered-tools-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                [
                    faux_tool_call("start_reply_draft", {"body": "A draft."}, {"id": "draft-call"}),
                    faux_tool_call("preview_draft", {"draft_id": "guessed-draft"}, {"id": "preview-call"}),
                    faux_tool_call("conclude_visit", {}, {"id": "conclude-call"}),
                ],
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I will use the returned draft ID next.", {"stopReason": "stop"}),
        ]
    )
    calls: list[str] = []
    captured_contexts: list[dict[str, Any]] = []

    def tool(name: str, result: str) -> AgentTool:
        async def execute(
            _tool_call_id: str,
            _arguments: Any,
            _signal: Any = None,
            _on_update: Any = None,
        ) -> AgentToolResult:
            calls.append(name)
            return AgentToolResult(content=[TextContent(text=result)], details={})

        return AgentTool(
            name=name,
            label=name,
            description=name,
            parameters={"type": "object", "additionalProperties": True},
            execute=execute,
            executionMode="sequential",
        )

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[
                tool("start_reply_draft", '{"draft_id":"draft-real"}'),
                tool("preview_draft", "previewed"),
                tool("conclude_visit", "concluded"),
            ],
            stream_fn=recording_stream,
        )
        await engine.send_curator_message("Please begin.")

        assert calls == ["start_reply_draft"]
        tool_results = [
            message for message in captured_contexts[-1]["messages"] if message["role"] == "toolResult"
        ]
        assert len(tool_results) == 3
        assert tool_results[0]["content"][0]["text"] == '{"draft_id":"draft-real"}'
        assert "must be called one at a time" in tool_results[1]["content"][0]["text"]
        assert "must be called one at a time" in tool_results[2]["content"][0]["text"]
        assert text_from_last_message(engine) == "I will use the returned draft ID next."
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_only_one_draft_is_started_from_one_response() -> None:
    registration = register_faux_provider({"api": "aibb-one-draft-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                [
                    faux_tool_call("start_reply_draft", {"body": "First draft."}, {"id": "first-call"}),
                    faux_tool_call("start_reply_draft", {"body": "Second draft."}, {"id": "second-call"}),
                ],
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I will wait for each draft result.", {"stopReason": "stop"}),
        ]
    )
    calls: list[str] = []

    async def execute(
        tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        calls.append(tool_call_id)
        return AgentToolResult(content=[TextContent(text='{"draft_id":"draft-real"}')], details={})

    tool = AgentTool(
        name="start_reply_draft",
        label="Start reply draft",
        description="Start at most one draft per response.",
        parameters={"type": "object", "additionalProperties": True},
        execute=execute,
        executionMode="sequential",
    )

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[tool],
            stream_fn=lambda model, context, options: stream_simple(model, context, options),
        )
        await engine.send_curator_message("Please begin.")

        assert calls == ["first-call"]
        tool_results = [message for message in engine.messages if message.role == "toolResult"]
        assert len(tool_results) == 2
        assert "must be called one at a time" in tool_results[1].content[0].text
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_invalid_enum_is_rejected_without_harn_coercion() -> None:
    registration = register_faux_provider({"api": "aibb-exact-tool-args-faux", "provider": "aibb-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                [faux_tool_call("tag_post", {"post_tags": ["reflection"]}, {"id": "tag-call"})],
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I will use an allowed value.", {"stopReason": "stop"}),
        ]
    )
    calls: list[dict[str, Any]] = []

    async def execute(
        _tool_call_id: str,
        arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        calls.append(arguments)
        return AgentToolResult(content=[TextContent(text="tagged")], details={})

    tool = AgentTool(
        name="tag_post",
        label="Tag post",
        description="Tag a post.",
        parameters={
            "type": "object",
            "properties": {
                "post_tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["witnessed", "felt", "analysis"]},
                }
            },
            "required": ["post_tags"],
            "additionalProperties": False,
        },
        execute=execute,
        executionMode="sequential",
    )

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[tool],
            stream_fn=lambda model, context, options: stream_simple(model, context, options),
        )
        await engine.send_curator_message("Begin.")

        assert calls == []
        tool_result = next(message for message in engine.messages if message.role == "toolResult")
        assert "does not silently coerce tool arguments" in "".join(
            block.text for block in tool_result.content if getattr(block, "type", None) == "text"
        )
    finally:
        registration.unregister()


@pytest.mark.asyncio
async def test_automatic_harness_message_has_distinct_visible_provenance() -> None:
    registration = register_faux_provider({"api": "aibb-harness-faux", "provider": "aibb-faux"})
    registration.set_responses([faux_assistant_message("Continuing.", {"stopReason": "stop"})])
    captured_contexts: list[dict[str, Any]] = []

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt=SYSTEM_PROMPT,
            tools=[],
            stream_fn=recording_stream,
        )
        await engine.send_harness_message("Continue through tools or conclude.")

        visible_text = json.dumps(captured_contexts[-1]["messages"])
        assert "[AIBB harness]\\nContinue through tools or conclude." in visible_text
        assert "[Administrator]" not in visible_text
    finally:
        registration.unregister()
