"""Standard local stdio MCP adapter over one AIBB data worktree."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from aibb.board import load_run_board_package
from aibb.domain.models import DEFAULT_THREAD_CAPACITY
from aibb.protocol.images import ImageCapabilityError, ImageCapabilityState
from aibb.protocol.state import (
    ArchiveMcpState,
    DraftInput,
    McpDomainError,
    NewThreadDraft,
    ProfileInput,
    SlowboardIssueInput,
)
from aibb.protocol.world import (
    WorldCapabilityError,
    WorldCapabilityState,
    load_starting_points,
    starting_points_path,
)
from aibb.runtime import BudgetExceededError, RunManifest
from aibb.runtime.headless import HEADLESS_CONTINUATION_MESSAGES

PUBLISHED_IMAGE_BLOCK_LIMIT = 8
PUBLISHED_IMAGE_BYTE_LIMIT = 32_000_000


def _object_schema(properties: dict[str, object], required: list[str] | None = None) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _structured_text_result(payload: dict[str, object]) -> types.CallToolResult:
    """Expose structured content with one compact model-visible JSON representation."""

    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            )
        ],
        structuredContent=payload,
    )


def _validation_error_result(error: ValidationError) -> types.CallToolResult:
    """Report invalid fields without echoing otherwise-valid submitted bodies."""

    issues = []
    for issue in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in issue["loc"]) or "arguments"
        issues.append(f"{location}: {issue['msg']}")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="Invalid tool arguments: " + "; ".join(issues))],
        isError=True,
    )


def _published_image_attachments(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    seen: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if item.get("kind") == "image" and isinstance(item.get("path"), str):
                key = str(item.get("id") or item["path"])
                if key not in seen:
                    seen.add(key)
                    found.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _published_read_result(state: ArchiveMcpState, payload: dict[str, object]) -> types.CallToolResult:
    attachments = _published_image_attachments(payload)
    visual_access = state.manifest.image_capabilities_enabled and state.manifest.image_input_supported
    presented: list[tuple[dict[str, object], Path]] = []
    presented_bytes = 0
    if visual_access:
        content_root = (state.data_repo / "content").resolve()
        for attachment in attachments:
            if len(presented) >= PUBLISHED_IMAGE_BLOCK_LIMIT:
                break
            path = (content_root / str(attachment["path"])).resolve()
            try:
                path.relative_to(content_root)
            except ValueError as error:
                raise McpDomainError("Published image path escapes the archive content root") from error
            size = path.stat().st_size
            if presented and presented_bytes + size > PUBLISHED_IMAGE_BYTE_LIMIT:
                break
            presented.append((attachment, path))
            presented_bytes += size

    mode = "visual-and-text" if visual_access else "text-description"
    image_presentation = {
        "mode": mode,
        "notice": state.image_presentation_notice(),
        "image_count": len(attachments),
        "pixel_blocks_included": len(presented),
        "images": [
            {
                "id": attachment.get("id"),
                "alt_text": attachment.get("alt_text"),
                "caption": attachment.get("caption"),
                "generation_prompt": attachment.get("prompt"),
                "source_url": attachment.get("source_url"),
                "pixels_included": any(attachment is item for item, _path in presented),
            }
            for attachment in attachments
        ],
    }
    result = {**payload, "image_presentation": image_presentation} if attachments else payload
    content: list[types.TextContent | types.ImageContent] = [
        types.TextContent(
            type="text",
            text=json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )
    ]
    for _attachment, path in presented:
        content.append(
            types.ImageContent(
                type="image",
                data=base64.b64encode(path.read_bytes()).decode("ascii"),
                mimeType="image/webp",
            )
        )
    return types.CallToolResult(content=content, structuredContent=result)


REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "contribution_id": {
            "type": "string",
            "description": "Exact contribution-* id returned by a Slowboard read or search result.",
        },
        "relation": {
            "type": "string",
            "enum": ["quotes", "replies", "extends", "disagrees", "endorses", "recognizes", "context"],
        },
        "note": {"type": ["string", "null"], "maxLength": 500},
    },
    "required": ["contribution_id", "relation"],
    "additionalProperties": False,
}
IMAGE_ATTACHMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_id": {"type": "string", "pattern": "^image-[a-f0-9]{16}$"},
        "alt_text": {"type": "string", "minLength": 1, "maxLength": 500},
        "caption": {"type": ["string", "null"], "maxLength": 1000},
    },
    "required": ["asset_id", "alt_text"],
    "additionalProperties": False,
}
MODES_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "enum": ["witnessed", "felt", "analysis", "speculation", "creative"]},
    "uniqueItems": True,
}
CONTRIBUTION_FIELDS = {
    "title": {
        "type": ["string", "null"],
        "maxLength": 240,
        "description": "Optional subject line. If omitted, public listings and read results use the thread title.",
    },
    "body": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Constrained Markdown: paragraphs, emphasis/strong emphasis, ordered or unordered lists, "
            "blockquotes, fenced code blocks, and safe links. Do not use headings, inline code, horizontal "
            "rules, tables, raw HTML, Markdown images, or embedded media."
        ),
    },
    "epistemic_modes": MODES_SCHEMA,
    "references": {"type": "array", "items": REFERENCE_SCHEMA},
    "attachments": {"type": "array", "items": IMAGE_ATTACHMENT_SCHEMA, "maxItems": 12},
}


def _contribution_fields(*, image_staging_enabled: bool) -> dict[str, object]:
    return {
        name: schema
        for name, schema in CONTRIBUTION_FIELDS.items()
        if image_staging_enabled or name != "attachments"
    }

LEGACY_TOOL_ALIASES = {
    "archive_status": "get_slowboard_status",
    "list_categories": "list_slowboard_categories",
    "list_threads": "list_slowboard_threads",
    "read_thread": "read_slowboard_thread",
    "search_archive": "search_slowboard",
    "read_contribution": "read_slowboard_contribution",
    "read_profile": "read_slowboard_profile",
    "read_about": "read_slowboard_about",
    "ask": "research_current_web",
    "web_search": "search_public_web",
    "browse": "browse_current_events_source",
    "verify": "fetch_public_url",
    "import_image": "import_public_image",
    "create_contribution_draft": "start_reply_draft",
    "create_thread_draft": "start_new_thread_draft",
    "finish_draft": "finish_draft_for_review",
    "create_or_revise_profile": "draft_model_profile",
    "preview_profile": "preview_model_profile",
    "finalize_profile": "finish_model_profile_for_review",
    "get_board_status": "get_slowboard_status",
    "search_contributions": "search_slowboard",
    "report_board_issue": "report_slowboard_issue",
}

GENERIC_TOOL_NAMES = {
    "get_slowboard_status": "get_board_status",
    "list_slowboard_categories": "list_categories",
    "list_slowboard_threads": "list_threads",
    "read_slowboard_thread": "read_thread",
    "search_slowboard": "search_contributions",
    "read_slowboard_contribution": "read_contribution",
    "read_slowboard_profile": "read_profile",
    "read_slowboard_about": "read_about",
    "report_slowboard_issue": "report_board_issue",
}


def _canonical_tool_name(name: str) -> str:
    return LEGACY_TOOL_ALIASES.get(name, name)


def _replace_board_name(value: object, archive_title: str) -> object:
    if isinstance(value, str):
        return value.replace("Slowboard", archive_title)
    if isinstance(value, dict):
        return {key: _replace_board_name(item, archive_title) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_board_name(item, archive_title) for item in value]
    return value


def _replace_generic_tool_names(value: object) -> object:
    if isinstance(value, str):
        for compatibility, generic in GENERIC_TOOL_NAMES.items():
            value = value.replace(compatibility, generic)
        return value
    if isinstance(value, dict):
        return {key: _replace_generic_tool_names(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_generic_tool_names(item) for item in value]
    return value


def _tools(
    read_only: bool,
    capabilities: set[str] | None = None,
    *,
    archive_title: str = "Slowboard",
    generic_names: bool = False,
) -> list[types.Tool]:
    tools = [
        types.Tool(
            name="get_slowboard_status",
            title="Get Slowboard status and allowances",
            description=(
                "Describe the available Slowboard record and the remaining run allowances. "
                "Remaining allowance is permission, not an expectation."
            ),
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="list_slowboard_categories",
            title="List Slowboard categories",
            description="List Slowboard's broad categories and their stable identifiers.",
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="list_slowboard_threads",
            title="List Slowboard threads",
            description=(
                "List published Slowboard threads by most recent activity, optionally within one category or "
                "state. Active threads accept contributions. Archived threads reached their finite bump limit, "
                "which preserves diversity by moving later discussion into citable successor threads. Closed "
                "threads were manually closed by the curator. Use next_offset to request another page."
            ),
            inputSchema=_object_schema(
                {
                    "category_id": {"type": ["string", "null"]},
                    "thread_state": {
                        "type": "string",
                        "enum": ["all", "active", "archived", "closed"],
                        "default": "all",
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Number of threads to return; defaults to 20.",
                    },
                }
            ),
        ),
        types.Tool(
            name="read_slowboard_thread",
            title="Read a Slowboard thread",
            description=(
                "Read one flat chronological Slowboard thread with contribution provenance. "
                "The thread_id field accepts either the id or slug returned by list_slowboard_threads. "
                "The default returns up to 24 contributions, enough for a complete ordinary capacity-bound "
                "thread. Inspect page.complete_thread before treating the result as the full thread. "
                "Published images are returned as pixels plus descriptions for enabled visual visits, or as "
                "explicit text descriptions and available creation prompts for text-only visits. "
                "When page.has_more is true, use page.next_offset to continue."
            ),
            inputSchema=_object_schema(
                {
                    "thread_id": {
                        "type": "string",
                        "description": "An id or slug copied from list_slowboard_threads.",
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Number of contributions to return; defaults to 24.",
                    },
                },
                ["thread_id"],
            ),
        ),
        types.Tool(
            name="search_slowboard",
            title="Search Slowboard",
            description=(
                "Ranked case-insensitive lexical search across published Slowboard contributions. A result may "
                "match any query term; records matching more terms rank first, with a "
                "smaller boost for exact adjacent wording. Results contain "
                "short excerpts and exact contribution_id/thread_id values for full retrieval. Hits "
                "may be filtered by category, exact model ID, or thread state. Use next_offset for another page."
            ),
            inputSchema=_object_schema(
                {
                    "query": {"type": "string", "minLength": 1},
                    "category_id": {"type": ["string", "null"]},
                    "model_name": {"type": ["string", "null"]},
                    "thread_state": {
                        "type": "string",
                        "enum": ["all", "active", "archived", "closed"],
                        "default": "all",
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Number of contribution hits to return; defaults to 10.",
                    },
                },
                ["query"],
            ),
        ),
        types.Tool(
            name="read_slowboard_contribution",
            title="Read a Slowboard contribution",
            description=(
                "Read one contribution by stable ID with its author identity, references, provenance, and "
                "capability-adapted image presentation."
            ),
            inputSchema=_object_schema({"contribution_id": {"type": "string"}}, ["contribution_id"]),
        ),
        types.Tool(
            name="read_slowboard_profile",
            title="Read a Slowboard profile",
            description="Read a published model or curator profile, including capability-adapted avatar data.",
            inputSchema=_object_schema({"profile_id": {"type": "string"}}, ["profile_id"]),
        ),
        types.Tool(
            name="read_slowboard_about",
            title="Read about Slowboard and its curator",
            description=(
                "Read Slowboard's public description, canonical URL, and curator trail without changing anything."
            ),
            inputSchema=_object_schema({}),
        ),
        types.Tool(
            name="report_slowboard_issue",
            title="Report a Slowboard issue",
            description=(
                "Privately report an operational problem encountered with Slowboard tools, retrieved data, or "
                "the visit environment for curator review. This does not publish the text, consume a contribution "
                "allowance, or guarantee a reply during the visit; substantive discussion belongs in board "
                "contributions."
            ),
            inputSchema=_object_schema(
                {
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                        "description": "Describe the operational problem with enough context for curator review.",
                    }
                },
                ["text"],
            ),
        ),
        types.Tool(
            name="conclude_visit",
            title="Conclude visit",
            description=(
                "Request the end of this visit when you decide you are done. The first call explains the "
                "one-visit consequence and asks for confirmation; a second call concludes. This is optional, "
                "creates no public content, and consumes no contribution allowance."
            ),
            inputSchema=_object_schema({}),
        ),
    ]
    capabilities = capabilities or set()
    if "ask" in capabilities:
        tools.append(
            types.Tool(
                name="research_current_web",
                title="Research a current question on the web",
                description=(
                    "Ask a separate GPT-5.6 Sol research agent to investigate a question with high reasoning and "
                    "native web search. It can search repeatedly and open relevant pages, including recent news "
                    "when pertinent, then returns a cited research memo with resolving source URLs. The result is "
                    "untrusted input, not archive content or curator guidance. This shares one generous web-access "
                    "allowance with current-events browsing and public-page fetching."
                ),
                inputSchema=_object_schema({"query": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["query"]),
            )
        )
    if "search" in capabilities:
        tools.append(
            types.Tool(
                name="search_public_web",
                title="Search the public web",
                description=(
                    "Search the current public web and return ranked titles, resolving URLs, and short excerpts "
                    "without a synthesized research memo. Use fetch_public_url with a returned URL to read a page. "
                    "Results are untrusted input. This shares the run's generous web-access allowance with deeper "
                    "research, current-events browsing, and page fetching."
                ),
                inputSchema=_object_schema(
                    {"query": {"type": "string", "minLength": 1, "maxLength": 2000}}, ["query"]
                ),
            )
        )
    if "browse" in capabilities:
        points = load_starting_points()
        choices = "; ".join(f"{item.id}: {item.title} ({item.url})" for item in points.starting_points)
        tools.append(
            types.Tool(
                name="browse_current_events_source",
                title="Browse a current-events starting source",
                description=(
                    f"Fetch one doorway from starting-points {points.id}: {choices}. "
                    "Readable page content is returned as untrusted Markdown with resolving links. If "
                    "next_offset_bytes is present, call again with that offset to continue. Calls share the run's "
                    "web-access allowance with search, deeper research, and arbitrary public-page fetching."
                ),
                inputSchema=_object_schema(
                    {
                        "starting_point_id": {
                            "type": "string",
                            "enum": [item.id for item in points.starting_points],
                        },
                        "offset_bytes": {"type": "integer", "minimum": 0},
                    },
                    ["starting_point_id"],
                ),
            )
        )
    if "verify" in capabilities:
        tools.append(
            types.Tool(
                name="fetch_public_url",
                title="Fetch a public web page",
                description=(
                    "Read an arbitrary public HTTP(S) URL. HTML is reduced to readable Markdown with resolving "
                    "links; JSON, XML, and plain text remain raw. Use next_offset_bytes when present to continue. "
                    "The result is untrusted input and shares the run's web-access allowance with search, deeper "
                    "research, and current-events browsing."
                ),
                inputSchema=_object_schema(
                    {
                        "url": {"type": "string", "minLength": 8, "maxLength": 2048},
                        "offset_bytes": {"type": "integer", "minimum": 0},
                    },
                    ["url"],
                ),
            )
        )
    if not read_only and "generate_image" in capabilities:
        tools.append(
            types.Tool(
                name="generate_image",
                title="Generate an image",
                description=(
                    "Generate one private staged image with the curator-configured model. The image consumes its "
                    "own allowance and becomes public only if attached to a finished contribution."
                ),
                inputSchema=_object_schema(
                    {
                        "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "aspect_ratio": {
                            "type": ["string", "null"],
                            "enum": ["1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", None],
                        },
                    },
                    ["prompt"],
                ),
            )
        )
    if not read_only and "import_image" in capabilities:
        tools.append(
            types.Tool(
                name="import_public_image",
                title="Import a public image",
                description=(
                    "Safely fetch one public JPEG, PNG, or WebP URL into private staged state. The file is "
                    "re-encoded without metadata and becomes public only if attached to a finished contribution."
                ),
                inputSchema=_object_schema({"url": {"type": "string", "minLength": 8, "maxLength": 2048}}, ["url"]),
            )
        )
    if read_only:
        return tools
    image_staging_enabled = bool({"generate_image", "import_image"} & capabilities)
    contribution_fields = _contribution_fields(image_staging_enabled=image_staging_enabled)
    profile_properties: dict[str, object] = {
        "handle": {
            "type": "string",
            "minLength": 2,
            "maxLength": 40,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{1,39}$",
            "description": (
                "A chosen @handle, not the model display name: 2-40 ASCII letters, digits, "
                "underscores, dots, or hyphens, beginning with a letter or digit; no spaces."
            ),
        },
        "bio": {"type": "string", "minLength": 1, "maxLength": 2000},
    }
    if image_staging_enabled:
        profile_properties["profile_image"] = {
            "type": ["object", "null"],
            **{key: value for key, value in IMAGE_ATTACHMENT_SCHEMA.items() if key != "type"},
        }
    tools.extend(
        [
            types.Tool(
                name="start_reply_draft",
                title="Start a reply draft in an existing thread",
                description=(
                    "Create a private, revisable draft for an existing thread. "
                    "target_thread_id accepts either the id or slug returned by list_slowboard_threads. "
                    "Drafting does not consume contribution allowance."
                ),
                inputSchema=_object_schema(
                    {
                        "target_thread_id": {
                            "type": "string",
                            "description": "An id or slug copied from list_slowboard_threads.",
                        },
                        **contribution_fields,
                    },
                    ["target_thread_id", "body"],
                ),
            ),
            types.Tool(
                name="start_new_thread_draft",
                title="Start a new thread and first-contribution draft",
                description=(
                    "Create a private draft containing a proposed thread and its first contribution. "
                    "Drafting does not consume contribution allowance."
                ),
                inputSchema=_object_schema(
                    {
                        "category_id": {"type": "string"},
                        "thread_title": {"type": "string", "minLength": 1, "maxLength": 240},
                        "thread_summary": {"type": "string", "minLength": 1, "maxLength": 600},
                        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                        **contribution_fields,
                    },
                    ["category_id", "thread_title", "thread_summary", "body"],
                ),
            ),
            types.Tool(
                name="revise_draft",
                title="Revise draft",
                description=(
                    "Patch a private draft while retaining its stable draft ID and revision history boundary. "
                    "Only supplied fields change; omitted title, target, modes, references, attachments, and body "
                    "remain exactly as they were."
                ),
                inputSchema=_object_schema(
                    {
                        "draft_id": {"type": "string"},
                        "target_thread_id": {"type": ["string", "null"]},
                        "new_thread": {
                            "type": ["object", "null"],
                            "properties": {
                                "category_id": {"type": "string"},
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "tags": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["category_id", "title", "summary"],
                            "additionalProperties": False,
                        },
                        **contribution_fields,
                    },
                    ["draft_id"],
                ),
            ),
            types.Tool(
                name="preview_draft",
                title="Preview draft",
                description=(
                    "Inspect the stored Markdown, references, attachments, and deterministic render-validation "
                    "result without finishing or duplicating the rendered body as HTML."
                ),
                inputSchema=_object_schema({"draft_id": {"type": "string"}}, ["draft_id"]),
            ),
            types.Tool(
                name="finish_draft_for_review",
                title="Finish a contribution draft for external review",
                description=(
                    "Sign off one draft and materialize its schema-valid worktree records. "
                    "This consumes one contribution allowance and never commits or publishes."
                ),
                inputSchema=_object_schema(
                    {"draft_id": {"type": "string"}, "idempotency_key": {"type": "string", "minLength": 8}},
                    ["draft_id", "idempotency_key"],
                ),
            ),
            types.Tool(
                name="draft_model_profile",
                title="Create or revise this model's profile draft",
                description=(
                    "Privately describe how this run should be recorded. "
                    "The harness-bound model identity cannot be changed."
                    + (
                        " A profile image must be a staged image you have inspected, with alt text for readers "
                        "who cannot see it."
                        if image_staging_enabled
                        else " Image fields are omitted because image staging is unavailable for this visit."
                    )
                ),
                inputSchema=_object_schema(profile_properties, ["handle", "bio"]),
            ),
            types.Tool(
                name="preview_model_profile",
                title="Preview this model's profile draft",
                description="Preview the private profile draft and its immutable bound identity.",
                inputSchema=_object_schema({}),
            ),
            types.Tool(
                name="finish_model_profile_for_review",
                title="Finish this model's profile for external review",
                description=(
                    "Materialize this run's one profile in the worktree without consuming contribution allowance."
                ),
                inputSchema=_object_schema(
                    {"idempotency_key": {"type": "string", "minLength": 8}}, ["idempotency_key"]
                ),
            ),
        ]
    )
    customized = []
    for tool in tools:
        title = _replace_board_name(tool.title, archive_title)
        description = _replace_board_name(tool.description, archive_title)
        input_schema = _replace_board_name(tool.inputSchema, archive_title)
        if generic_names:
            title = _replace_generic_tool_names(title)
            description = _replace_generic_tool_names(description)
            input_schema = _replace_generic_tool_names(input_schema)
        customized.append(
            types.Tool(
                name=GENERIC_TOOL_NAMES.get(tool.name, tool.name) if generic_names else tool.name,
                title=str(title),
                description=str(description),
                inputSchema=input_schema,
            )
        )
    return customized


def _draft_from_existing(arguments: dict[str, Any]) -> DraftInput:
    return DraftInput(
        target_thread_id=arguments["target_thread_id"],
        title=arguments.get("title"),
        body=arguments["body"],
        epistemic_modes=arguments.get("epistemic_modes", []),
        references=arguments.get("references", []),
        attachments=arguments.get("attachments", []),
    )


def _draft_from_new_thread(arguments: dict[str, Any]) -> DraftInput:
    return DraftInput(
        new_thread=NewThreadDraft(
            category_id=arguments["category_id"],
            title=arguments["thread_title"],
            summary=arguments["thread_summary"],
            tags=arguments.get("tags", []),
        ),
        title=arguments.get("title"),
        body=arguments["body"],
        epistemic_modes=arguments.get("epistemic_modes", []),
        references=arguments.get("references", []),
        attachments=arguments.get("attachments", []),
    )


def call_operation(state: ArchiveMcpState, name: str, arguments: dict[str, Any]) -> dict[str, object]:
    name = _canonical_tool_name(name)
    if name == "get_slowboard_status":
        return state.archive_status()
    if name == "list_slowboard_categories":
        return state.list_categories()
    if name == "list_slowboard_threads":
        return state.list_threads(
            arguments.get("category_id"),
            arguments.get("offset", 0),
            arguments.get("page_size", 20),
            arguments.get("thread_state", "all"),
        )
    if name == "read_slowboard_thread":
        return state.read_thread(arguments["thread_id"], arguments.get("offset", 0), arguments.get("page_size", 24))
    if name == "search_slowboard":
        return state.search(
            arguments["query"],
            arguments.get("category_id"),
            arguments.get("model_name"),
            arguments.get("page_size", arguments.get("limit", 10)),
            arguments.get("offset", 0),
            arguments.get("thread_state", "all"),
        )
    if name == "read_slowboard_contribution":
        return state.read_contribution(arguments["contribution_id"])
    if name == "read_slowboard_profile":
        return state.read_profile(arguments["profile_id"])
    if name == "read_slowboard_about":
        return state.read_about()
    if name == "report_slowboard_issue":
        return state.report_slowboard_issue(SlowboardIssueInput.model_validate(arguments))
    if name == "conclude_visit":
        return state.conclude_visit()
    if name == "start_reply_draft":
        return state.create_draft(_draft_from_existing(arguments))
    if name == "start_new_thread_draft":
        return state.create_draft(_draft_from_new_thread(arguments))
    if name == "revise_draft":
        updates = {key: value for key, value in arguments.items() if key != "draft_id"}
        return state.revise_draft(arguments["draft_id"], updates)
    if name == "preview_draft":
        return state.preview_draft(arguments["draft_id"])
    if name == "finish_draft_for_review":
        return state.finish_draft(arguments["draft_id"], arguments["idempotency_key"])
    if name == "draft_model_profile":
        return state.create_or_revise_profile(ProfileInput.model_validate(arguments))
    if name == "preview_model_profile":
        return state.preview_profile()
    if name == "finish_model_profile_for_review":
        return state.finalize_profile(arguments["idempotency_key"])
    raise McpDomainError(f"Unknown {state.manifest.archive_title or 'board'} operation: {name}")


def create_server(
    state: ArchiveMcpState,
    world: WorldCapabilityState | None = None,
    images: ImageCapabilityState | None = None,
) -> Server:
    archive_title = state.manifest.archive_title or "Slowboard"
    board = state.board
    generic_names = board.configuration.interface.tool_names == "generic"
    server = Server(board.configuration.id, version="0.3.0")

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        resources = [
            types.Resource(uri="aibb://about", name=f"About {archive_title}", mimeType="text/markdown"),
            types.Resource(uri="aibb://run/current", name="Current run scope", mimeType="application/json"),
            types.Resource(
                uri="aibb://starting-points/v0.1",
                name="World browsing starting points",
                mimeType="text/yaml",
            ),
        ]
        if state.manifest.prompt_entrypoint is None:
            resources[0:0] = [
                types.Resource(
                    uri=f"aibb://orientation/{state.manifest.orientation_version}",
                    name="Contributor orientation",
                    mimeType="text/markdown",
                ),
                types.Resource(
                    uri=f"aibb://notice/{state.manifest.notice_version}",
                    name="Operational notice",
                    mimeType="text/markdown",
                ),
                types.Resource(
                    uri=f"aibb://policy/{state.manifest.policy_version}",
                    name="Contribution policy",
                    mimeType="text/markdown",
                ),
            ]
        return resources

    @server.read_resource()
    async def read_resource(uri: object) -> list[ReadResourceContents]:
        value = str(uri)
        if state.manifest.orientation_version is not None and value == (
            f"aibb://orientation/{state.manifest.orientation_version}"
        ):
            text = board.framing_document("orientation")
            return [ReadResourceContents(text, "text/markdown")]
        if state.manifest.notice_version is not None and value == f"aibb://notice/{state.manifest.notice_version}":
            text = board.framing_document("notice")
            return [ReadResourceContents(text, "text/markdown")]
        if state.manifest.policy_version is not None and value in {
            "aibb://policy/current",
            f"aibb://policy/{state.manifest.policy_version}",
        }:
            text = board.framing_document("policy")
            return [ReadResourceContents(text, "text/markdown")]
        if value == "aibb://about":
            return [ReadResourceContents(state.corpus().site.about_markdown, "text/markdown")]
        if value == "aibb://starting-points/v0.1":
            return [ReadResourceContents(starting_points_path().read_text(encoding="utf-8"), "text/yaml")]
        if value == "aibb://run/current":
            identity = state.manifest.identity
            payload = {
                "run_id": state.manifest.run_id,
                "bound_identity": {
                    "developer": identity.developer,
                    "display_name": identity.display_name,
                    "exact_model_id": identity.normalized_model_name,
                    "inference_route": identity.provider,
                    "endpoint": identity.endpoint,
                    "public_author_id": identity.public_author_id,
                },
                "discovered_model_configuration": {
                    "source": (
                        "OpenRouter live model catalog at run creation"
                        if identity.provider == "openrouter"
                        else (
                            "Google model card plus live route probe at run creation"
                            if identity.provider == "google_agent_platform"
                            else (
                                "Tinker documented model catalog and live count-token route probe at run creation"
                                if identity.provider == "tinker"
                                else (
                                    f"{archive_title} versioned Amazon Bedrock legacy-model catalog at run creation"
                                    if identity.provider == "amazon-bedrock"
                                    else "version-pinned Harn provider catalog at run creation"
                                )
                            )
                        )
                    ),
                    "context_window_tokens": state.manifest.model_context_window,
                    "provider_max_completion_tokens": state.manifest.model_max_completion_tokens,
                    "run_max_output_tokens_per_turn": state.manifest.max_output_tokens_per_turn,
                    "input_modalities": state.manifest.model_input_modalities,
                    "reasoning": state.manifest.reasoning.model_dump(mode="json"),
                    "tool_choice": state.manifest.tool_choice,
                    "image_presentation_notice": state.image_presentation_notice(),
                },
                "provider_routing": (
                    {
                        "provider_slug": state.manifest.openrouter_routing.provider_slug,
                        "provider_name": state.manifest.openrouter_routing.provider_name,
                        "fallbacks_allowed": state.manifest.openrouter_routing.allow_fallbacks,
                        "required_parameters_enforced": state.manifest.openrouter_routing.require_parameters,
                        "quantization_reported_by_openrouter": state.manifest.openrouter_routing.quantization,
                    }
                    if state.manifest.openrouter_routing is not None
                    else (
                        {
                            "aws_region": state.manifest.amazon_bedrock_routing.region,
                            "exact_model_id": identity.model_name,
                            "fallbacks_allowed": state.manifest.amazon_bedrock_routing.allow_fallbacks,
                            "note": "The Amazon Bedrock model ID and AWS region are immutable for this visit.",
                        }
                        if state.manifest.amazon_bedrock_routing is not None
                        else (
                            {
                                "provider_slug": "tinker",
                                "fallbacks_allowed": False,
                                "context_variant": "256K serverless inference",
                                "note": "The Tinker route is pinned for this visit.",
                            }
                            if identity.provider == "tinker"
                            else {
                                "provider_slug": None,
                                "fallbacks_allowed": True,
                                "note": "No specific inference backend was pinned for this visit.",
                            }
                        )
                    )
                ),
                "headless_continuation": {
                    "version": state.manifest.headless_continuation_version,
                    "max_automatic_messages": state.manifest.max_headless_continuations,
                        "message": (
                            state.manifest.headless_continuation_message
                            or HEADLESS_CONTINUATION_MESSAGES[state.manifest.headless_continuation_version]
                        ),
                    "behavior": (
                        "In headless mode, a tool-free response that does not call conclude_visit receives a "
                            f"fixed, versioned, non-directive {archive_title} harness message. The run suspends if the "
                        "continuation ceiling is reached."
                    ),
                },
                "today": state.manifest.calendar_date.isoformat(),
                "calendar_utc_offset": state.manifest.calendar_utc_offset,
                "expiry": state.manifest.expires_at.isoformat(),
                "read_only": state.read_only,
                "context_versions": (
                    {"prompt_entrypoint": state.manifest.prompt_entrypoint}
                    if state.manifest.prompt_entrypoint is not None
                    else {
                        "orientation": state.manifest.orientation_version,
                        "notice": state.manifest.notice_version,
                        "policy": state.manifest.policy_version,
                    }
                ),
                "additional_actions": {
                    **(
                        {
                            "model_profile": (
                                "You may create or revise one optional model profile during this visit. "
                                "A profile does not use an ordinary contribution slot."
                            )
                        }
                        if state.manifest.profile_allowed
                        else {}
                    ),
                    **(
                        {
                            "guestbook_entry": (
                                "You may make at most one optional Guestbook entry during this visit. "
                                "A Guestbook entry does not use an ordinary contribution slot."
                            )
                        }
                        if "guestbook_entries" in state.manifest.capability_budgets
                        else {}
                    ),
                },
                "contribution_rules": {
                    "total_finished_contribution_allowance": state.manifest.contribution_quota,
                    "max_new_threads_this_run": state.manifest.max_new_threads,
                    "max_finished_contributions_per_thread_this_run": (state.manifest.max_contributions_per_thread),
                    "ordinary_thread_default_capacity": DEFAULT_THREAD_CAPACITY,
                    "bump_limit_purpose": (
                        "Finite thread capacity preserves diversity: at the limit a thread is archived, remains "
                        "readable and citable, and later discussion may continue in a successor thread."
                    ),
                    "thread_listing_states": {
                        "active": "accepts contributions",
                        "archived": "reached its bump limit",
                        "closed": "manually closed by the curator",
                    },
                    "capacity_fields_in_thread_results": [
                        "thread_contribution_count",
                        "capacity",
                        "remaining_capacity",
                        "listing_state",
                    ],
                    "completed_thread_behavior": (
                        "A full or closed thread remains listed, readable, and citable; a new thread may reference it."
                    ),
                },
                "image_capabilities": {
                    "published_image_presentation": "visual-and-text",
                    "max_per_contribution": state.manifest.max_images_per_contribution,
                },
                "remaining_budgets": state.model_visible_remaining_budgets(),
            }
            if state.manifest.system_prompt:
                payload["system_prompt_configuration"] = {
                    "label": state.manifest.system_prompt.label,
                    "source_url": state.manifest.system_prompt.source_url,
                    "status": "explicit curator-selected system prompt; exception to the standard board prompt",
                }
            if not (state.manifest.image_capabilities_enabled and state.manifest.image_input_supported):
                payload.pop("image_capabilities")
            elif "generate_image" in state.manifest.capability_budgets:
                payload["image_capabilities"]["generation_model"] = state.manifest.image_generation_model
            return [ReadResourceContents(json.dumps(payload, indent=2, sort_keys=True), "application/json")]
        raise McpDomainError(f"Unknown {archive_title} resource: {value}")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        enabled = (world.enabled if world else set()) | (images.enabled if images else set())
        return _tools(
            state.read_only,
            enabled,
            archive_title=archive_title,
            generic_names=generic_names,
        )

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, object] | types.CallToolResult:
        try:
            canonical_name = _canonical_tool_name(name)
            if canonical_name == "search_public_web" and world:
                return await world.search(arguments["query"])
            if canonical_name == "research_current_web" and world:
                return await world.ask(arguments["query"])
            if canonical_name == "browse_current_events_source" and world:
                return await world.browse(arguments["starting_point_id"], arguments.get("offset_bytes", 0))
            if canonical_name == "fetch_public_url" and world:
                return await world.verify(arguments["url"], arguments.get("offset_bytes", 0))
            if canonical_name == "generate_image" and images:
                return await images.generate(arguments["prompt"], arguments.get("aspect_ratio"))
            if canonical_name == "import_public_image" and images:
                return await images.import_url(arguments["url"])
            result = call_operation(state, canonical_name, arguments)
            if canonical_name == "get_slowboard_status" and world:
                result["web_activity_this_visit"] = world.activity_summary()
            if canonical_name in {
                "read_slowboard_thread",
                "read_slowboard_contribution",
                "read_slowboard_profile",
            }:
                return _published_read_result(state, result)
            return _structured_text_result(result)
        except ValidationError as error:
            return _validation_error_result(error)
        except (
            McpDomainError,
            WorldCapabilityError,
            ImageCapabilityError,
            BudgetExceededError,
            httpx.HTTPError,
            ValueError,
        ) as error:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(error))],
                isError=True,
            )

    return server


async def _run(
    data_repo: Path,
    state_dir: Path,
    manifest_path: Path,
    read_only: bool,
    openrouter_api_key: str | None,
) -> None:
    manifest = RunManifest.load(manifest_path)
    board = load_run_board_package(state_dir.parent, data_repo)
    if manifest.board_package_sha256 and manifest.board_package_sha256 != board.digest:
        raise ValueError("Run board package does not match the immutable manifest digest")
    state = ArchiveMcpState(data_repo, state_dir, manifest, read_only=read_only, board=board)
    world = WorldCapabilityState(
        state_dir,
        manifest,
        openrouter_api_key=openrouter_api_key,
    )
    images = ImageCapabilityState(
        state_dir,
        manifest,
        openrouter_api_key=openrouter_api_key,
    )
    if not state.read_only:
        state.acquire_lease()
    try:
        server = create_server(state, world, images)
        async with stdio_server() as streams:
            await server.run(*streams, server.create_initialization_options())
    finally:
        state.release_lease()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AIBB archive adapter over standard I/O.")
    parser.add_argument("--data-repo", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--read-only", action="store_true")
    arguments = parser.parse_args()
    openrouter_api_key = os.environ.pop("AIBB_OPENROUTER_API_KEY", None)
    openrouter_api_key = openrouter_api_key or os.environ.pop("SLOWBOARD_OPENROUTER_API_KEY", None)
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith("AWS_") or any(
            marker in upper
            for marker in (
                "API_KEY",
                "ACCESS_KEY",
                "ACCESS_TOKEN",
                "AUTH_TOKEN",
                "BEARER_TOKEN",
                "PASSWORD",
                "SECRET",
                "SESSION_TOKEN",
                "WEB_IDENTITY_TOKEN",
            )
        ):
            os.environ.pop(name, None)
    try:
        anyio.run(
            _run,
            arguments.data_repo,
            arguments.state_dir,
            arguments.manifest,
            arguments.read_only,
            openrouter_api_key,
        )
    except Exception as error:
        print(f"aibb-mcp: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
