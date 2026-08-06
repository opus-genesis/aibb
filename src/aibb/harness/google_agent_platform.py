"""Google Agent Platform route for the deprecated Grok 4.1 Fast reasoning model."""

from __future__ import annotations

from typing import Literal

import httpx
from harn_ai.types import Model, ModelCost

from aibb.harness.openrouter import OpenRouterAdapter
from aibb.runtime import BudgetLedger
from aibb.sessions.store import SessionStore

GOOGLE_AGENT_PLATFORM_API_BASE = "https://aiplatform.googleapis.com/v1"
GROK_4_1_FAST_REASONING = "xai/grok-4.1-fast-reasoning"
GROK_4_1_FAST_CONTEXT_WINDOW = 128_000


def google_agent_platform_endpoint(*, project_id: str, location: str = "global", endpoint: str = "openapi") -> str:
    """Build the immutable OpenAI-compatible endpoint bound into a run manifest."""

    if not project_id.strip():
        raise ValueError("GOOGLE_AGENT_PLATFORM_PROJECT_ID is not set")
    return (
        f"{GOOGLE_AGENT_PLATFORM_API_BASE}/projects/{project_id}"
        f"/locations/{location}/endpoints/{endpoint}/chat/completions"
    )


def google_agent_platform_model(model_id: str, *, endpoint: str, max_tokens: int) -> Model:
    """Return the independently probed Grok route supported by this adapter."""

    if model_id != GROK_4_1_FAST_REASONING:
        raise ValueError(f"Unsupported Google Agent Platform model ID: {model_id}")
    return Model(
        id=model_id,
        name=model_id,
        api="aibb-google-agent-platform-chat-completions",
        provider="google_agent_platform",
        baseUrl=endpoint,
        reasoning=True,
        input=["text", "image"],
        # This deprecated preview route is served from fixed quota and reports zero
        # request cost. Token ceilings remain enforced independently of cost.
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=GROK_4_1_FAST_CONTEXT_WINDOW,
        maxTokens=max_tokens,
    )


class GoogleAgentPlatformAdapter(OpenRouterAdapter):
    """Lossless Google boundary using its OpenAI-compatible response contract."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        ledger: BudgetLedger,
        session: SessionStore,
        max_output_tokens: int,
        tool_choice: Literal["auto", "required"] = "auto",
        timeout_seconds: float = 180,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            ledger=ledger,
            session=session,
            max_output_tokens=max_output_tokens,
            prompt_price_per_token=0,
            completion_price_per_token=0,
            app_url="https://aibb.invalid/",
            tool_choice=tool_choice,
            endpoint=endpoint,
            request_headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
