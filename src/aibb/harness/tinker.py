"""Tinker Anthropic-compatible inference support for Inkling models."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

from anthropic import AsyncAnthropic
from harn_ai.models import calculate_cost
from harn_ai.providers.anthropic import build_params, map_stop_reason
from harn_ai.types import (
    AnthropicMessagesCompat,
    AssistantMessage,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    ModelCost,
    StartEvent,
    TextContent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingContent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageCost,
)
from harn_ai.utils.event_stream import AssistantMessageEventStream

from aibb.harness.anthropic import AnthropicAdapter
from aibb.runtime import BudgetLedger
from aibb.sessions.store import SessionStore

TINKER_ANTHROPIC_BASE_URL = "https://tinker.thinkingmachines.dev/services/tinker-prod/anthropic/api"
TINKER_ANTHROPIC_ENDPOINT = f"{TINKER_ANTHROPIC_BASE_URL}/v1/messages"
TINKER_INKLING_SMALL = "thinkingmachines/Inkling-Small"
TINKER_INKLING_SMALL_SERVERLESS_256K = f"{TINKER_INKLING_SMALL}:peft:262144:sampling-nvfp4"
TINKER_INKLING_SMALL_CONTEXT_WINDOW = 262_144
TINKER_INKLING_SMALL_MAX_TOKENS = 65_536

# Serverless inference prices per million tokens on 2026-07-31. Tinker reports
# uncached prompt tokens as cache-creation usage and discounted prefix hits as
# cache-read usage through its Anthropic-compatible endpoint.
TINKER_INKLING_SMALL_INPUT_PRICE = 0.30
TINKER_INKLING_SMALL_CACHE_READ_PRICE = 0.06
TINKER_INKLING_SMALL_OUTPUT_PRICE = 1.20

_UNSIGNED_THINKING_SENTINEL = "aibb-tinker-unsigned-thinking"


def public_tinker_model_id(model_id: str) -> str:
    """Return the route-independent public Inkling model identity."""

    if model_id == TINKER_INKLING_SMALL_SERVERLESS_256K:
        return TINKER_INKLING_SMALL
    raise ValueError(f"Unsupported Tinker model ID: {model_id}")


def tinker_model(model_id: str) -> Model:
    """Return the pinned Harn model record for Tinker's Inkling Small route."""

    public_id = public_tinker_model_id(model_id)
    return Model(
        id=model_id,
        name="Inkling-Small",
        api="anthropic-messages",
        provider="tinker",
        baseUrl=TINKER_ANTHROPIC_BASE_URL,
        reasoning=True,
        thinkingLevelMap={
            "off": None,
            "minimal": "low",
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
        },
        input=["text", "image"],
        cost=ModelCost(
            input=TINKER_INKLING_SMALL_INPUT_PRICE,
            output=TINKER_INKLING_SMALL_OUTPUT_PRICE,
            cacheRead=TINKER_INKLING_SMALL_CACHE_READ_PRICE,
            cacheWrite=TINKER_INKLING_SMALL_INPUT_PRICE,
        ),
        contextWindow=TINKER_INKLING_SMALL_CONTEXT_WINDOW,
        maxTokens=TINKER_INKLING_SMALL_MAX_TOKENS,
        compat=AnthropicMessagesCompat(
            supportsEagerToolInputStreaming=False,
            supportsLongCacheRetention=False,
            sendSessionAffinityHeaders=False,
            supportsCacheControlOnTools=False,
            forceAdaptiveThinking=True,
        ),
    ).model_copy(update={"name": public_id.rsplit("/", 1)[-1]})


async def probe_tinker_model(model_id: str, *, api_key: str, timeout_seconds: float = 30) -> int:
    """Verify the exact compatible-API route without invoking inference."""

    public_tinker_model_id(model_id)
    client = AsyncAnthropic(
        base_url=TINKER_ANTHROPIC_BASE_URL,
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    )
    try:
        result = await client.messages.count_tokens(
            model=model_id,
            messages=[{"role": "user", "content": "AIBB route probe."}],
        )
        return int(result.input_tokens)
    finally:
        await client.close()


def stream_tinker_messages(
    model: Model,
    context: Context,
    options: dict[str, Any] | None = None,
) -> AssistantMessageEventStream:
    """Normalize Tinker's non-streaming response without dropping reasoning.

    Tinker's beta streaming endpoint currently omits Inkling thinking blocks
    even though the equivalent non-streaming response includes them and bills
    their tokens. AIBB prefers the complete provider-visible trace over
    partial token delivery, so this adapter emits Harn events after receiving
    the complete Messages response.
    """

    stream = AssistantMessageEventStream()
    resolved_options = options or {}

    async def run() -> None:
        output = AssistantMessage(
            content=[],
            api=model.api,
            provider=model.provider,
            model=model.id,
            usage=Usage(
                input=0,
                output=0,
                cacheRead=0,
                cacheWrite=0,
                totalTokens=0,
                cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
            ),
            stopReason="stop",
            timestamp=time.time_ns() // 1_000_000,
        )
        try:
            params = build_params(model, context, False, resolved_options)
            params["stream"] = False
            on_payload = resolved_options.get("onPayload")
            if callable(on_payload):
                prepared = on_payload(params, model)
                if hasattr(prepared, "__await__"):
                    prepared = await prepared
                if prepared is not None:
                    params = prepared
            client = resolved_options["client"]
            response = await client.messages.create(**params)
            output.responseId = response.id
            output.responseModel = response.model
            usage = response.usage
            output.usage.input = int(usage.input_tokens or 0)
            output.usage.output = int(usage.output_tokens or 0)
            output.usage.cacheRead = int(usage.cache_read_input_tokens or 0)
            output.usage.cacheWrite = int(usage.cache_creation_input_tokens or 0)
            output.usage.totalTokens = (
                output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite
            )
            calculate_cost(model, output.usage)
            stream.push(StartEvent(partial=output))

            for block in response.content:
                content_index = len(output.content)
                if block.type == "thinking":
                    content = ThinkingContent(
                        thinking=block.thinking,
                        thinkingSignature=block.signature or "",
                    )
                    output.content.append(content)
                    stream.push(ThinkingStartEvent(contentIndex=content_index, partial=output))
                    if content.thinking:
                        stream.push(
                            ThinkingDeltaEvent(
                                contentIndex=content_index,
                                delta=content.thinking,
                                partial=output,
                            )
                        )
                    stream.push(
                        ThinkingEndEvent(
                            contentIndex=content_index,
                            content=content.thinking,
                            partial=output,
                        )
                    )
                elif block.type == "text":
                    content = TextContent(text=block.text)
                    output.content.append(content)
                    stream.push(TextStartEvent(contentIndex=content_index, partial=output))
                    if content.text:
                        stream.push(
                            TextDeltaEvent(
                                contentIndex=content_index,
                                delta=content.text,
                                partial=output,
                            )
                        )
                    stream.push(
                        TextEndEvent(
                            contentIndex=content_index,
                            content=content.text,
                            partial=output,
                        )
                    )
                elif block.type == "tool_use":
                    content = ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                    output.content.append(content)
                    stream.push(ToolCallStartEvent(contentIndex=content_index, partial=output))
                    encoded_arguments = json.dumps(content.arguments, ensure_ascii=False, separators=(",", ":"))
                    if encoded_arguments:
                        stream.push(
                            ToolCallDeltaEvent(
                                contentIndex=content_index,
                                delta=encoded_arguments,
                                partial=output,
                            )
                        )
                    stream.push(
                        ToolCallEndEvent(
                            contentIndex=content_index,
                            toolCall=content,
                            partial=output,
                        )
                    )

            output.stopReason = map_stop_reason(response.stop_reason or "end_turn")
            stream.push(DoneEvent(reason=output.stopReason, message=output))
        except Exception as error:  # noqa: BLE001
            output.stopReason = "error"
            output.errorMessage = str(error)
            stream.push(ErrorEvent(reason="error", error=output))
        finally:
            stream.end()

    asyncio.create_task(run())
    return stream


class TinkerAdapter(AnthropicAdapter):
    """Use Harn's Messages event normalizer against Tinker's compatible API."""

    def __init__(
        self,
        *,
        api_key: str,
        ledger: BudgetLedger,
        session: SessionStore,
        max_output_tokens: int,
        tool_choice: Literal["auto", "required"] = "auto",
        reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "high",
        timeout_seconds: float = 180,
        stream_fn: Any = None,
        client_factory: Any = None,
    ) -> None:
        options: dict[str, Any] = {
            "api_key": api_key,
            "ledger": ledger,
            "session": session,
            "max_output_tokens": max_output_tokens,
            "tool_choice": tool_choice,
            "reasoning_effort": reasoning_effort,
            "timeout_seconds": timeout_seconds,
            "endpoint": TINKER_ANTHROPIC_ENDPOINT,
            "client_base_url": TINKER_ANTHROPIC_BASE_URL,
            "provider_error_type": "TinkerProviderError",
        }
        options["stream_fn"] = stream_fn or stream_tinker_messages
        if client_factory is not None:
            options["client_factory"] = client_factory
        super().__init__(**options)

    def _prepare_context(self, context: Context) -> Context:
        # Tinker deliberately returns empty thinking signatures. Harn otherwise
        # serializes unsigned reasoning as ordinary assistant text on the next
        # turn. A temporary sentinel keeps it typed until _prepare_payload
        # restores the exact empty signature accepted by Tinker.
        prepared = context.model_copy(deep=True)
        for message in prepared.messages:
            if message.role != "assistant":
                continue
            for block in message.content:
                if block.type == "thinking" and not (block.thinkingSignature or "").strip():
                    block.thinkingSignature = _UNSIGNED_THINKING_SENTINEL
        return prepared

    def _prepare_payload(self, payload: dict[str, Any], _model: Model) -> dict[str, Any]:
        # Tinker controls effort with output_config and does not require
        # Anthropic's adaptive-thinking selector.
        payload.pop("thinking", None)
        for message in payload.get("messages", []):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "thinking"
                    and block.get("signature") == _UNSIGNED_THINKING_SENTINEL
                ):
                    block["signature"] = ""
        return payload
