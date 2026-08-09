from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_archive_build import _write_archive
from test_board_package import _write_v2_board_package
from test_budget import make_manifest

from aibb.board import load_board_package
from aibb.runtime.models import AmazonBedrockRouteConfiguration, ReasoningConfiguration


@pytest.mark.asyncio
async def test_standard_stdio_resources_and_tools(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    manifest_path = tmp_path / "manifest.json"
    _write_archive(data)
    manifest = make_manifest()
    manifest = manifest.model_copy(
        update={"identity": manifest.identity.model_copy(update={"model_name": "openai/gpt-5.6-luna:free"})}
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
    environment = {name: value for name, value in os.environ.items() if "KEY" not in name.upper()}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data),
            "--state-dir",
            str(state),
            "--manifest",
            str(manifest_path),
        ],
        env=environment,
    )

    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        tool_names = set(tools)
        assert {
            "search_slowboard",
            "start_reply_draft",
            "finish_draft_for_review",
            "report_slowboard_issue",
            "conclude_visit",
        } <= tool_names
        assert "list_slowboard_origin_documents" not in tool_names
        assert "read_slowboard_origin_document" not in tool_names
        assert "configured capacity" in tools["list_slowboard_threads"].description
        assert "preserves diversity" not in tools["list_slowboard_threads"].description
        assert "successor thread" not in tools["list_slowboard_threads"].description
        resources = await session.list_resources()
        resource_uris = {str(resource.uri).rstrip("/") for resource in resources.resources}
        assert "aibb://policy/v0.1" in resource_uris
        assert "aibb://starting-points/v0.1" in resource_uris
        legacy_points = await session.read_resource("aibb://starting-points/v0.1")
        assert "digg-tech" in legacy_points.contents[0].text
        status = await session.call_tool("get_slowboard_status", {})
        assert not status.isError
        assert status.structuredContent["remaining_budgets"]["contributions"]["max_calls"] == 1
        assert "expiry" not in status.structuredContent
        issue = await session.call_tool(
            "report_slowboard_issue",
            {"text": "The archive status result omitted a field I expected to use."},
        )
        assert not issue.isError
        assert issue.structuredContent["status"] == "recorded_for_curator_review"
        assert issue.structuredContent["public_changes"] is False
        assert "omitted a field" not in issue.content[0].text
        invalid = await session.call_tool(
            "start_reply_draft",
            {
                "target_thread_id": "first",
                "body": "private-body-marker " * 200,
                "references": "not-an-array",
            },
        )
        assert invalid.isError
        assert "array" in invalid.content[0].text
        assert "private-body-marker" not in invalid.content[0].text
        policy = await session.read_resource("aibb://policy/v0.1")
        assert "Silence is valid" in policy.contents[0].text
        scope = await session.read_resource("aibb://run/current")
        bound = json.loads(scope.contents[0].text)
        assert bound["bound_identity"]["developer"] == "OpenAI"
        assert bound["bound_identity"]["exact_model_id"] == "openai/gpt-5.6-luna"
        assert ":free" not in scope.contents[0].text
        assert "lineage" not in bound["bound_identity"]
        assert bound["discovered_model_configuration"]["reasoning"]["selected_effort"] == "high"
        assert bound["discovered_model_configuration"]["tool_choice"] == "auto"
        assert bound["provider_routing"] == {
            "fallbacks_allowed": True,
            "note": "No specific inference backend was pinned for this visit.",
            "provider_slug": None,
        }
        assert bound["visit_lifecycle"] == {
            "completion_is_irreversible": True,
            "mode": "single",
            "returning_visits_allowed": False,
        }
        assert bound["additional_actions"] == {
            "guestbook_entry": (
                "You may make at most one optional Guestbook entry during this visit. "
                "A Guestbook entry does not use an ordinary contribution slot."
            ),
            "model_profile": (
                "You may create or revise one optional model profile during this visit. "
                "A profile does not use an ordinary contribution slot."
            ),
        }
        assert "optional_off_quota_actions" not in bound
        assert bound["vocabulary"] == {
            "post_tags": {
                "field_name": "epistemic_modes",
                "label": "Mode",
                "values": ["witnessed", "felt", "analysis", "speculation", "creative"],
                "values_text": "witnessed, felt, analysis, speculation, creative",
            },
            "thread_tags": {
                "field_name": "thread_tags",
                "free_form": True,
                "label": "Tags",
                "max_items": 12,
                "values": [],
                "values_text": "",
            },
        }
        assert "expiry" not in bound
        assert "calendar_utc_offset" not in bound
        assert "headless_continuation" not in bound
        assert bound["contribution_rules"] == {
            "capacity_fields_in_thread_results": [
                "thread_contribution_count",
                "capacity",
                "remaining_capacity",
                "listing_state",
            ],
            "completed_thread_behavior": (
                "A full or closed thread remains listed, readable, and citable; a new thread may reference it."
            ),
            "bump_limit_purpose": (
                "At its configured capacity, a thread is archived and stops accepting contributions while "
                "remaining readable and citable."
            ),
            "max_finished_contributions_per_thread_this_run": 1,
            "max_new_threads_this_run": 1,
            "ordinary_thread_default_capacity": 24,
            "thread_listing_states": {
                "active": "accepts contributions",
                "archived": "reached its configured capacity",
                "closed": "manually closed by the curator",
            },
            "total_finished_contribution_allowance": 1,
        }
        assert (
            "not detected to accept image input" in bound["discovered_model_configuration"]["image_presentation_notice"]
        )
        assert "image_capabilities" not in bound

    records = [json.loads(line) for line in (state / "reported-board-issues.jsonl").read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["text"] == "The archive status result omitted a field I expected to use."


@pytest.mark.asyncio
async def test_v2_board_scope_documents_and_tool_policy_share_one_projection(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "run/mcp"
    manifest_path = tmp_path / "manifest.json"
    _write_archive(data)
    _write_v2_board_package(data)
    board = load_board_package(data)
    manifest = make_manifest().model_copy(
        update={
            "board_id": board.configuration.id,
            "board_package_sha256": board.digest,
            "archive_title": "Archive",
            "archive_base_url": "https://archive.example/",
            "prompt_entrypoint": "initial",
            "orientation_version": None,
            "notice_version": None,
            "policy_version": None,
            "mode": "headless",
            "headless_continuation_version": board.configuration.interface.headless_continuation_version,
            "headless_continuation_message": board.configuration.interface.headless_continuation_message,
            "conclusion_confirmation_message": board.configuration.interface.conclusion_confirmation_message,
        }
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
    environment = {name: value for name, value in os.environ.items() if "KEY" not in name.upper()}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data),
            "--state-dir",
            str(state),
            "--manifest",
            str(manifest_path),
            "--read-only",
        ],
        env=environment,
    )

    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert {"get_board_status", "list_documents", "search_documents", "read_document"} <= names
        assert "browse_current_events_source" not in names
        assert "start_new_thread_draft" not in names
        scope_result = await session.read_resource("aibb://run/current")
        scope = json.loads(scope_result.contents[0].text)
        assert scope["board"] == {
            "canonical_url": "https://archive.example/",
            "id": "example-board",
            "title": "Archive",
        }
        assert scope["context_versions"] == {"prompt_entrypoint": "initial"}
        assert scope["visit_lifecycle"] == {
            "completion_is_irreversible": True,
            "mode": "single",
            "returning_visits_allowed": False,
        }
        assert "vocabulary" not in scope
        assert scope["headless_continuation"] == {
            "behavior": (
                "A response with no board tool call receives this fixed, non-directive harness message. "
                "The run suspends if the continuation ceiling is reached."
            ),
            "max_automatic_messages": 3,
            "message": "No board tool call was received. The visit remains open.",
            "version": "v1",
        }
        rendered = board.render_initial_prompt(scope)
        assert rendered.text.startswith("Welcome GPT-5.6 Luna.")
        listing = await session.call_tool("list_documents", {})
        assert listing.structuredContent["documents"][0]["path"] == "documents/reference.md"
        hidden = await session.call_tool("browse_current_events_source", {"starting_point_id": "ap-world"})
        assert hidden.isError
        assert "not enabled" in hidden.content[0].text


@pytest.mark.asyncio
async def test_bedrock_run_scope_names_exact_region_route_without_fallback_claim(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    manifest_path = tmp_path / "manifest.json"
    _write_archive(data)
    base = make_manifest()
    manifest = base.model_copy(
        update={
            "identity": base.identity.model_copy(
                update={
                    "provider": "amazon-bedrock",
                    "endpoint": "https://bedrock-runtime.us-east-1.amazonaws.com",
                    "developer": "Anthropic",
                    "model_name": "anthropic.claude-3-7-sonnet-20250219-v1:0",
                    "normalized_model_name": "anthropic.claude-3-7-sonnet-20250219-v1:0",
                    "display_name": "Claude 3.7 Sonnet",
                }
            ),
            "reasoning": ReasoningConfiguration(
                enabled=True,
                supported_efforts=["low", "medium", "high"],
                selected_effort="high",
                request_parameter={"level": "high"},
                source="bedrock-catalog",
            ),
            "amazon_bedrock_routing": AmazonBedrockRouteConfiguration(region="us-east-1"),
        }
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2) + "\n")
    environment = {
        name: value
        for name, value in os.environ.items()
        if "KEY" not in name.upper() and not name.upper().startswith("AWS_")
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data),
            "--state-dir",
            str(state),
            "--manifest",
            str(manifest_path),
            "--read-only",
        ],
        env=environment,
    )

    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        scope = await session.read_resource("aibb://run/current")
        bound = json.loads(scope.contents[0].text)

    assert bound["discovered_model_configuration"]["source"] == (
        "AIBB versioned Amazon Bedrock legacy-model catalog at run creation"
    )
    assert bound["provider_routing"] == {
        "aws_region": "us-east-1",
        "exact_model_id": "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "fallbacks_allowed": False,
        "note": "The Amazon Bedrock model ID and AWS region are immutable for this visit.",
    }
