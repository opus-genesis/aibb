from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from test_budget import make_manifest

from aibb.protocol.server import _tools
from aibb.protocol.world import (
    ASK_MAX_OUTPUT_TOKENS,
    ASK_MODEL,
    ASK_SYSTEM_PROMPT_V2,
    SEARCH_MAX_CHARACTERS,
    SEARCH_MAX_OUTPUT_TOKENS,
    SEARCH_MAX_RESULTS,
    SEARCH_MODEL,
    SEARCH_SYSTEM_PROMPT,
    WorldCapabilityError,
    WorldCapabilityState,
    validate_public_url,
)
from aibb.runtime.models import BudgetLimits


def _resolver(host: str, port: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _manifest():
    return make_manifest().model_copy(
        update={
            "capability_budgets": {
                **make_manifest().capability_budgets,
                "web": BudgetLimits(
                    max_calls=40,
                    max_input_tokens=80_000,
                    max_output_tokens=160_000,
                    max_total_tokens=240_000,
                    max_cost_usd=5,
                    max_request_bytes=200_000,
                    max_result_bytes=4_000_000,
                ),
            }
        }
    )


def test_world_tool_schemas_are_explicit_and_starting_points_are_versioned() -> None:
    tools = {
        tool.name: tool
        for tool in _tools(
            False,
            {"ask", "search", "browse", "verify"},
            generic_names=True,
            generic_tool_version="v2",
        )
    }
    assert "read_about" in tools

    assert "configured research service" in tools["research_web"].description
    assert "with web search" in tools["research_web"].description
    assert "without a synthesized research memo" in tools["search_web"].description
    assert tools["search_web"].inputSchema["properties"]["query"]["maxLength"] == 2000
    assert "ap-world" in tools["browse_web_source"].inputSchema["properties"]["starting_point_id"]["enum"]
    assert tools["fetch_url"].inputSchema["properties"]["url"]["maxLength"] == 2048
    assert tools["fetch_url"].inputSchema["properties"]["offset_bytes"]["minimum"] == 0


def test_paid_research_tool_is_omitted_without_its_operator_credential(tmp_path: Path) -> None:
    without_key = WorldCapabilityState(tmp_path / "without", _manifest(), openrouter_api_key=None)
    with_key = WorldCapabilityState(tmp_path / "with", _manifest(), openrouter_api_key="private-key")

    assert without_key.enabled == {"browse", "verify"}
    assert with_key.enabled == {"ask", "search", "browse", "verify"}


@pytest.mark.parametrize("url", ["http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/x"])
def test_verify_rejects_local_and_private_networks(url: str) -> None:
    with pytest.raises(WorldCapabilityError, match="local and private"):
        validate_public_url(url)


@pytest.mark.asyncio
async def test_ask_uses_sol_native_research_and_returns_resolving_sources(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-research",
                "model": "openai/gpt-5.6-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "A current research summary.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/source",
                                        "title": "Example",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "total_tokens": 300,
                    "cost": 0.9,
                    "server_tool_use_details": {"web_search_requests": 7},
                },
                "openrouter_metadata": {
                    "pipeline": [
                        {
                            "type": "server_tools",
                            "data": {"mode": "native", "tools": ["openrouter:web_search"]},
                        }
                    ]
                },
            },
        )

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key="operator-only-secret",
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )
    result = await world.ask("What changed today?")

    assert captured["url"] == "https://openrouter.ai/api/v1/responses"
    assert captured["model"] == ASK_MODEL == "openai/gpt-5.6-sol"
    assert captured["instructions"] == ASK_SYSTEM_PROMPT_V2
    assert "breaking developments" in captured["instructions"]
    assert captured["reasoning"] == {"effort": "high"}
    assert captured["tools"] == [
        {"type": "openrouter:web_search", "parameters": {"engine": "native"}}
    ]
    assert captured["max_output_tokens"] == ASK_MAX_OUTPUT_TOKENS
    assert captured["stop_server_tools_when"] == [
        {"type": "max_cost", "max_cost_in_dollars": 4.0}
    ]
    assert "max_tool_calls" not in captured
    assert result["kind"] == "untrusted_ai_research_summary"
    assert result["research_profile"] == {
        "reasoning_effort": "high",
        "web_search": "native",
        "web_search_requests": 7,
    }
    assert result["sources"] == [{"url": "https://example.com/source", "title": "Example"}]
    assert world.ledger.remaining()["web"]["max_calls"] == 39
    assert world.ledger.remaining()["web"]["max_cost_usd"] == pytest.approx(4.1)
    log = world.log_path.read_text()
    assert "What changed today?" in log
    assert "operator-only-secret" not in log


@pytest.mark.asyncio
async def test_failed_sol_research_does_not_charge_its_conservative_cost_reservation(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "invalid request"}})

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key="operator-only-secret",
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await world.ask("What changed today?")

    remaining = world.ledger.remaining()["web"]
    assert remaining["max_calls"] == 39
    assert remaining["max_cost_usd"] == 5
    failed = json.loads(world.log_path.read_text().splitlines()[-1])
    assert failed["type"] == "ask_failed"


@pytest.mark.asyncio
async def test_basic_web_search_returns_ranked_resolving_results_without_a_synthesized_memo(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "gen-search",
                "status": "completed",
                "output": [
                    {"type": "openrouter:web_search", "status": "completed"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Search complete.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.com/article",
                                        "title": "Example article",
                                        "content": "Relevant extractive excerpt.",
                                    },
                                    {
                                        "type": "url_citation",
                                        "url_citation": {
                                            "url": "https://example.org/second",
                                            "title": "Second result",
                                            "content": "Another excerpt.",
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "total_tokens": 100,
                    "cost": 0.006,
                    "server_tool_use_details": {"web_search_requests": 1},
                },
            },
        )

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key="operator-only-secret",
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )
    result = await world.search("specific current question")

    assert captured["url"] == "https://openrouter.ai/api/v1/responses"
    assert captured["model"] == SEARCH_MODEL == "openai/gpt-5.6-luna"
    assert captured["instructions"] == SEARCH_SYSTEM_PROMPT
    assert captured["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {
                "engine": "perplexity",
                "max_results": SEARCH_MAX_RESULTS,
                "max_uses": 1,
                "max_total_results": SEARCH_MAX_RESULTS,
                "max_characters": SEARCH_MAX_CHARACTERS,
            },
        }
    ]
    assert captured["max_output_tokens"] == SEARCH_MAX_OUTPUT_TOKENS
    assert captured["max_tool_calls"] == 2
    assert result == {
        "kind": "untrusted_web_search_results",
        "query": "specific current question",
        "search_profile": {
            "engine": "perplexity",
            "web_search_requests": 1,
        },
        "results": [
            {
                "title": "Example article",
                "url": "https://example.com/article",
                "excerpt": "Relevant extractive excerpt.",
            },
            {
                "title": "Second result",
                "url": "https://example.org/second",
                "excerpt": "Another excerpt.",
            },
        ],
        "next_step": "Call fetch_public_url with a result URL to read that page.",
    }
    assert world.ledger.remaining()["web"]["max_calls"] == 39
    assert world.ledger.remaining()["web"]["max_cost_usd"] == pytest.approx(4.994)
    assert world.activity_summary() == {
        "search_queries": 1,
        "research_requests": 0,
        "current_events_sources": {},
        "public_page_fetches": 0,
        "failed_actions": 0,
        "provider_search_requests": 1,
    }
    assert "operator-only-secret" not in world.log_path.read_text()


@pytest.mark.asyncio
async def test_research_browse_and_verify_share_one_generous_web_budget(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain; charset=utf-8"}, text=f"from {request.url}")

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key=None,
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )

    browsed = await world.browse("digg-tech")
    verified = await world.verify("https://example.com/a-fact")

    assert browsed["starting_points_version"] == "v0.1"
    assert browsed["kind"] == verified["kind"] == "untrusted_remote_content"
    assert world.ledger.remaining()["web"]["max_calls"] == 38
    assert world.activity_summary() == {
        "search_queries": 0,
        "research_requests": 0,
        "current_events_sources": {"digg-tech": 1},
        "public_page_fetches": 1,
        "failed_actions": 0,
        "provider_search_requests": 0,
    }


def test_legacy_separate_world_budgets_remain_resumable(tmp_path: Path) -> None:
    legacy = make_manifest().model_copy(
        update={
            "capability_budgets": {
                **make_manifest().capability_budgets,
                "ask": BudgetLimits(max_calls=1, max_result_bytes=100_000),
                "browse": BudgetLimits(max_calls=1, max_result_bytes=100_000),
                "verify": BudgetLimits(max_calls=1, max_result_bytes=100_000),
            }
        }
    )
    world = WorldCapabilityState(tmp_path, legacy, openrouter_api_key="operator-secret", resolver=_resolver)

    assert world.enabled == {"ask", "browse", "verify"}


@pytest.mark.asyncio
async def test_browse_and_fetch_extract_linked_readable_html_with_pagination(tmp_path: Path) -> None:
    body = (
        "<html><script>ignored</script><nav>Navigation noise.</nav><main><p>Visible doorway text.</p>"
        '<h3><a href="/article/example">At least 18 people remain missing.</a></h3>'
        '<template><span class="Timestamp-template-now">[deltaMinutes] mins ago</span></template>'
        '<a class="PagePromo-commentCount" href="#comments">'
        '<svg><title>Comments</title></svg><span class="CommentCount-template">210</span></a>'
        + ("<p>first section material</p>" * 8_000)
        + "<p>Late section marker.</p>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/large-raw":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=utf-8"},
                text="raw data " * 20_000,
            )
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=body)

    world = WorldCapabilityState(
        tmp_path,
        _manifest(),
        openrouter_api_key=None,
        transport=httpx.MockTransport(handler),
        resolver=_resolver,
    )

    browsed = await world.browse("digg-tech")
    assert browsed["content_format"] == "extracted_markdown"
    assert browsed["truncated"] is True
    assert "Visible doorway text." in browsed["content"]
    assert (
        "[At least 18 people remain missing.](https://digg.com/article/example)"
        in browsed["content"]
    )
    assert "first section material" in browsed["content"]
    assert "Comments: 210" in browsed["content"]
    assert "[deltaMinutes]" not in browsed["content"]
    assert "Navigation noise." not in browsed["content"]
    assert "ignored" not in browsed["content"]
    assert isinstance(browsed["next_offset_bytes"], int)
    continued = await world.browse("digg-tech", offset_bytes=browsed["next_offset_bytes"])
    assert continued["content_offset_bytes"] == browsed["next_offset_bytes"]
    assert continued["returned_content_bytes"] > 0
    assert "Visible doorway text." not in continued["content"]

    fetched = await world.verify("https://example.com/too-large")
    assert fetched["content_format"] == "extracted_markdown"
    assert fetched["truncated"] is True
    assert fetched["next_offset_bytes"] is not None
    fetched_continuation = await world.verify(
        "https://example.com/too-large", offset_bytes=fetched["next_offset_bytes"]
    )
    assert fetched_continuation["content_offset_bytes"] == fetched["next_offset_bytes"]
    assert "Visible doorway text." not in fetched_continuation["content"]

    with pytest.raises(WorldCapabilityError, match="content ceiling"):
        await world.verify("https://example.com/large-raw")
    failed = json.loads(world.log_path.read_text().splitlines()[-1])
    assert failed["requested_url"] == "https://example.com/large-raw"
    assert failed["last_resolved_url"] == "https://example.com/large-raw"
