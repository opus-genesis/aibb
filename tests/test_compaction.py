from __future__ import annotations

from typing import Any

import pytest
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.providers.faux import faux_assistant_message, faux_tool_call, register_faux_provider
from harn_ai.stream import stream_simple
from harn_ai.types import TextContent, validate_message

from aibb.harness.compaction import compact_archive_results, estimate_message_tokens
from aibb.harness.engine import AibbHarnessEngine, EngineSnapshot
from aibb.harness.token_estimate import ESTIMATED_IMAGE_INPUT_TOKENS


def _tool_result(index: int, name: str = "read_thread") -> dict[str, object]:
    return {
        "role": "toolResult",
        "toolCallId": f"call-{index}",
        "toolName": name,
        "content": [{"type": "text", "text": "archive text " * 200}],
        "details": {"thread_id": f"thread-{index}", "body": "archive text " * 200},
        "isError": False,
        "timestamp": index,
    }


def _snapshot(messages: list[dict[str, object]]) -> EngineSnapshot:
    return EngineSnapshot(
        context_generation=2,
        system_prompt="",
        model={
            "id": "test/model",
            "name": "test/model",
            "api": "test",
            "provider": "test",
            "baseUrl": "https://example.com",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100_000,
            "maxTokens": 4_000,
        },
        messages=messages,
    )


def test_compaction_elides_only_older_archive_reads_and_is_resumable() -> None:
    messages = [_tool_result(index) for index in range(6)]
    messages.append(_tool_result(7, "finish_contribution"))
    result = compact_archive_results(
        _snapshot(messages),
        run_id="run-test-compaction",
        authorization="curator",
        source_event_sequence=42,
        keep_recent_results=2,
    )

    assert result is not None
    compacted, artifact = result
    assert compacted.context_generation == 3
    assert len(artifact.elisions) == 4
    assert artifact.estimated_tokens_after < artifact.estimated_tokens_before
    assert artifact.elisions[0].record_ids == ["thread-0"]
    assert "Call this public Slowboard tool again" in compacted.messages[0]["content"][0]["text"]
    assert "original_sha256" not in compacted.messages[0]["content"][0]["text"]
    assert compacted.messages[4] == messages[4]
    assert compacted.messages[-2] == messages[-1]
    assert compacted.messages[-1]["role"] == "user"
    assert "Slowboard harness context maintenance v0.2" in compacted.messages[-1]["content"][0]["text"]
    assert "original introduction" in artifact.maintenance_message
    assert all(validate_message(message) for message in compacted.messages)


def test_compaction_is_a_noop_without_older_eligible_results() -> None:
    assert (
        compact_archive_results(
            _snapshot([_tool_result(1)]),
            run_id="run-test-compaction",
            authorization="manifest-allow",
            source_event_sequence=1,
            keep_recent_results=1,
        )
        is None
    )


def test_context_estimate_counts_harn_images_as_vision_tokens_not_base64_text() -> None:
    messages = [
        {
            "role": "toolResult",
            "toolCallId": "call-image",
            "toolName": "generate_image",
            "content": [
                {"type": "text", "text": '{"asset_id":"image-one"}'},
                {"type": "image", "mimeType": "image/webp", "data": "a" * 500_000},
            ],
        }
    ]

    estimate = estimate_message_tokens(messages)

    assert estimate >= ESTIMATED_IMAGE_INPUT_TOKENS
    assert estimate < ESTIMATED_IMAGE_INPUT_TOKENS + 1_000


@pytest.mark.asyncio
async def test_authorized_compaction_can_run_between_tool_result_and_next_provider_request() -> None:
    registration = register_faux_provider({"api": "slowboard-compaction-faux", "provider": "slowboard-faux"})
    registration.set_responses(
        [
            faux_assistant_message(
                faux_tool_call("read_slowboard_thread", {"thread_id": "current"}, {"id": "call-current"}),
                {"stopReason": "toolUse"},
            ),
            faux_assistant_message("I continued after the recorded compaction.", {"stopReason": "stop"}),
        ]
    )
    captured_contexts: list[dict[str, Any]] = []

    async def execute(
        _tool_call_id: str,
        _arguments: Any,
        _signal: Any = None,
        _on_update: Any = None,
    ) -> AgentToolResult:
        return AgentToolResult(
            content=[TextContent(text="current archive text " * 200)],
            details={"thread_id": "current", "body": "current archive text " * 200},
        )

    tool = AgentTool(
        name="read_slowboard_thread",
        label="Read Slowboard thread",
        description="Read a thread.",
        parameters={
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
        execute=execute,
        executionMode="sequential",
    )

    def recording_stream(model: Any, context: Any, options: Any) -> Any:
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    def prepare(active_engine: AibbHarnessEngine) -> Any | None:
        result = compact_archive_results(
            active_engine.snapshot(),
            run_id="run-safe-boundary",
            authorization="manifest-allow",
            source_event_sequence=1,
            keep_recent_results=1,
        )
        if result is None:
            return None
        compacted, _artifact = result
        return active_engine.replace_model_visible_context(compacted)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt="",
            messages=[validate_message(_tool_result(index, "read_slowboard_thread")) for index in range(4)],
            tools=[tool],
            stream_fn=recording_stream,
            context_generation=2,
            prepare_next_turn=prepare,
        )

        await engine.send_curator_message("Continue.")

        assert len(captured_contexts) == 2
        assert "[Earlier Slowboard retrievable result elided]" in str(captured_contexts[1]["messages"])
        assert "[Slowboard harness context maintenance v0.2]" in str(captured_contexts[1]["messages"])
        assert engine.context_generation == 3
        assert engine.messages[-1].content[0].text == "I continued after the recorded compaction."
    finally:
        registration.unregister()


def test_compaction_elides_old_document_and_web_results_but_not_write_results() -> None:
    messages = [
        _tool_result(1, "read_document"),
        _tool_result(2, "search_public_web"),
        _tool_result(3, "fetch_public_url"),
        _tool_result(4, "finish_draft_for_review"),
    ]

    result = compact_archive_results(
        _snapshot(messages),
        run_id="run-retrievable-compaction",
        authorization="manifest-allow",
        source_event_sequence=4,
        keep_recent_results=0,
    )

    assert result is not None
    compacted, artifact = result
    assert [item.tool_name for item in artifact.elisions] == [
        "read_document",
        "search_public_web",
        "fetch_public_url",
    ]
    assert compacted.messages[3] == messages[3]
