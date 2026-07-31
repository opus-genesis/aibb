from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harn_ai.providers.anthropic import build_params
from harn_ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    StartEvent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
)
from harn_ai.utils.event_stream import AssistantMessageEventStream
from test_budget import make_manifest

from aibb.harness.tinker import (
    TINKER_ANTHROPIC_BASE_URL,
    TINKER_ANTHROPIC_ENDPOINT,
    TINKER_INKLING_SMALL,
    TINKER_INKLING_SMALL_CONTEXT_WINDOW,
    TINKER_INKLING_SMALL_SERVERLESS_256K,
    TinkerAdapter,
    public_tinker_model_id,
    tinker_model,
)
from aibb.runtime import BudgetLedger
from aibb.sessions import SessionStore


def test_tinker_inkling_small_route_is_pinned_and_public_identity_omits_route_suffix() -> None:
    model = tinker_model(TINKER_INKLING_SMALL_SERVERLESS_256K)

    assert model.id == TINKER_INKLING_SMALL_SERVERLESS_256K
    assert model.provider == "tinker"
    assert model.api == "anthropic-messages"
    assert model.baseUrl == TINKER_ANTHROPIC_BASE_URL
    assert model.contextWindow == TINKER_INKLING_SMALL_CONTEXT_WINDOW
    assert model.reasoning is True
    assert model.input == ["text", "image"]
    assert model.compat is not None
    assert model.compat.forceAdaptiveThinking is True
    assert model.compat.supportsEagerToolInputStreaming is False
    assert public_tinker_model_id(model.id) == TINKER_INKLING_SMALL

    with pytest.raises(ValueError, match="Unsupported Tinker model ID"):
        tinker_model("thinkingmachines/Inkling-Small")


@pytest.mark.asyncio
async def test_tinker_adapter_preserves_unsigned_reasoning_and_budgeted_tool_history(tmp_path: Path) -> None:
    native_requests: list[dict[str, Any]] = []
    client_options: list[dict[str, Any]] = []

    class FakeClient:
        async def close(self) -> None:
            return None

    def fake_client_factory(**options: Any) -> FakeClient:
        client_options.append(options)
        return FakeClient()

    def fake_native_stream(model: Any, context: Context, options: dict[str, Any]) -> AssistantMessageEventStream:
        native = AssistantMessageEventStream()

        async def emit() -> None:
            payload = build_params(model, context, False, options)
            payload = await options["onPayload"](payload, model)
            native_requests.append({"payload": payload, "options": options})
            await options["onResponse"](
                {"status": 200, "headers": {"request-id": "tinker-request", "x-secret": "omit"}},
                model,
            )
            usage = Usage(
                input=20,
                output=30,
                cacheRead=100,
                cacheWrite=50,
                totalTokens=200,
                cost=UsageCost(
                    input=0.000006,
                    output=0.000036,
                    cacheRead=0.000006,
                    cacheWrite=0.000015,
                    total=0.000063,
                ),
            )
            output = AssistantMessage(
                content=[],
                api=model.api,
                provider=model.provider,
                model=model.id,
                responseId="tinker-message",
                usage=usage,
                stopReason="stop",
                timestamp=1,
            )
            native.push(StartEvent(partial=output))
            native.push(DoneEvent(reason="stop", message=output))
            native.end()

        asyncio.create_task(emit())
        return native

    base = make_manifest()
    manifest = base.model_copy(
        update={
            "identity": base.identity.model_copy(
                update={
                    "provider": "tinker",
                    "endpoint": TINKER_ANTHROPIC_ENDPOINT,
                    "developer": "Thinking Machines Lab",
                    "model_name": TINKER_INKLING_SMALL_SERVERLESS_256K,
                    "normalized_model_name": TINKER_INKLING_SMALL,
                }
            ),
            "model_context_window": TINKER_INKLING_SMALL_CONTEXT_WINDOW,
        }
    )
    ledger = BudgetLedger(tmp_path / "mcp/budgets.json", manifest)
    session = SessionStore(tmp_path / "session", manifest.run_id)
    adapter = TinkerAdapter(
        api_key="private-tinker-key",
        ledger=ledger,
        session=session,
        max_output_tokens=500,
        tool_choice="required",
        reasoning_effort="high",
        stream_fn=fake_native_stream,
        client_factory=fake_client_factory,
    )
    prior = AssistantMessage(
        content=[
            ThinkingContent(thinking="private prior reasoning", thinkingSignature=""),
            ToolCall(id="archive:1", name="read_slowboard_thread", arguments={"thread_id": "thread-1"}),
        ],
        api="anthropic-messages",
        provider="tinker",
        model=TINKER_INKLING_SMALL_SERVERLESS_256K,
        usage=Usage(
            input=1,
            output=1,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=2,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="toolUse",
        timestamp=1,
    )
    result = ToolResultMessage(
        toolCallId="archive:1",
        toolName="read_slowboard_thread",
        content=[],
        isError=False,
        timestamp=2,
    )

    events = [
        event
        async for event in adapter(
            tinker_model(TINKER_INKLING_SMALL_SERVERLESS_256K),
            Context(systemPrompt="Slowboard", messages=[prior, result], tools=[]),
            None,
        )
    ]

    assert events[-1].type == "done"
    assert client_options == [
        {
            "api_key": "private-tinker-key",
            "timeout_seconds": 180,
            "base_url": TINKER_ANTHROPIC_BASE_URL,
        }
    ]
    request = native_requests[0]
    assert request["options"]["thinkingEnabled"] is True
    assert request["options"]["effort"] == "high"
    assert request["options"]["toolChoice"] == "any"
    assert request["options"]["cacheRetention"] == "none"
    assert "thinking" not in request["payload"]
    assert request["payload"]["output_config"] == {"effort": "high"}
    assistant = next(message for message in request["payload"]["messages"] if message["role"] == "assistant")
    thinking = next(block for block in assistant["content"] if block["type"] == "thinking")
    assert thinking == {
        "type": "thinking",
        "thinking": "private prior reasoning",
        "signature": "",
    }
    inference = ledger.read().accounts["inference"]
    assert inference.used.calls == 1
    assert inference.used.total_tokens == 200
    event_text = (tmp_path / "session/events.jsonl").read_text()
    assert "private-tinker-key" not in event_text
    assert "tinker-request" in event_text
    assert "x-secret" not in event_text
    assert TINKER_ANTHROPIC_ENDPOINT in event_text


@pytest.mark.asyncio
async def test_tinker_complete_response_emits_reasoning_before_tool_call(tmp_path: Path) -> None:
    sent_payloads: list[dict[str, Any]] = []

    class FakeMessages:
        async def create(self, **payload: Any) -> Any:
            sent_payloads.append(payload)
            return SimpleNamespace(
                id="tinker-response",
                model=TINKER_INKLING_SMALL_SERVERLESS_256K,
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    input_tokens=0,
                    output_tokens=40,
                    cache_read_input_tokens=100,
                    cache_creation_input_tokens=20,
                ),
                content=[
                    SimpleNamespace(
                        type="thinking",
                        thinking="Use the requested archive tool.",
                        signature="",
                    ),
                    SimpleNamespace(
                        type="tool_use",
                        id="archive:2",
                        name="read_slowboard_thread",
                        input={"thread_id": "thread-2"},
                    ),
                ],
            )

    class FakeClient:
        def __init__(self) -> None:
            self.messages = FakeMessages()

        async def close(self) -> None:
            return None

    base = make_manifest()
    manifest = base.model_copy(
        update={
            "identity": base.identity.model_copy(
                update={
                    "provider": "tinker",
                    "endpoint": TINKER_ANTHROPIC_ENDPOINT,
                    "model_name": TINKER_INKLING_SMALL_SERVERLESS_256K,
                    "normalized_model_name": TINKER_INKLING_SMALL,
                }
            ),
            "model_context_window": TINKER_INKLING_SMALL_CONTEXT_WINDOW,
        }
    )
    adapter = TinkerAdapter(
        api_key="private-tinker-key",
        ledger=BudgetLedger(tmp_path / "mcp/budgets.json", manifest),
        session=SessionStore(tmp_path / "session", manifest.run_id),
        max_output_tokens=500,
        tool_choice="required",
        reasoning_effort="high",
        client_factory=lambda **_options: FakeClient(),
    )

    events = [
        event
        async for event in adapter(
            tinker_model(TINKER_INKLING_SMALL_SERVERLESS_256K),
            Context(systemPrompt="Slowboard", messages=[], tools=[]),
            None,
        )
    ]

    assert [event.type for event in events] == [
        "start",
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "toolcall_start",
        "toolcall_delta",
        "toolcall_end",
        "done",
    ]
    assert sent_payloads[0]["stream"] is False
    assert sent_payloads[0]["output_config"] == {"effort": "high"}
    assert "thinking" not in sent_payloads[0]
    terminal = events[-1].message
    assert terminal.content[0].type == "thinking"
    assert terminal.content[0].thinking == "Use the requested archive tool."
    assert terminal.content[1].type == "toolCall"
    assert terminal.content[1].arguments == {"thread_id": "thread-2"}
    assert terminal.usage.cacheRead == 100
    assert terminal.usage.cacheWrite == 20
