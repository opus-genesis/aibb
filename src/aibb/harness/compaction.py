"""Explicit, artifact-producing context compaction for controlled runs."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aibb.harness.engine import EngineSnapshot
from aibb.harness.token_estimate import estimate_json_tokens

ELIGIBLE_ARCHIVE_TOOLS = {
    "archive_status",
    "get_board_status",
    "get_slowboard_status",
    "list_categories",
    "list_slowboard_categories",
    "list_threads",
    "list_slowboard_threads",
    "read_thread",
    "read_slowboard_thread",
    "read_contribution",
    "read_slowboard_contribution",
    "read_profile",
    "read_slowboard_profile",
    "search_archive",
    "search_contributions",
    "search_slowboard",
}

COMPACTION_NOTICE_VERSION = "v0.2"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    return estimate_json_tokens(messages)


class ElidedArchiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_index: int = Field(ge=0)
    tool_name: str
    tool_call_id: str | None = None
    record_ids: list[str]
    original_sha256: str
    original_bytes: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    marker_text: str


class CompactionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    created_at: datetime
    method: Literal["archive-result-elision"] = "archive-result-elision"
    authorization: Literal["curator", "manifest-allow"]
    source_event_sequence: int = Field(ge=0)
    source_context_generation: int = Field(ge=0)
    result_context_generation: int = Field(ge=1)
    source_message_count: int = Field(ge=0)
    source_messages_sha256: str
    result_messages_sha256: str
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    maintenance_message: str
    elisions: list[ElidedArchiveResult]
    result_messages: list[dict[str, Any]]


def _record_ids(value: object) -> list[str]:
    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key in {"id", "thread_id", "contribution_id", "profile_id", "author_id"} and isinstance(
                    nested, str
                ):
                    found.add(nested)
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(found)


def compact_archive_results(
    snapshot: EngineSnapshot,
    *,
    run_id: str,
    authorization: Literal["curator", "manifest-allow"],
    source_event_sequence: int,
    keep_recent_results: int = 4,
    archive_title: str = "Slowboard",
) -> tuple[EngineSnapshot, CompactionArtifact] | None:
    messages = [dict(message) for message in snapshot.messages]
    eligible = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "toolResult" and message.get("toolName") in ELIGIBLE_ARCHIVE_TOOLS
        and not (isinstance(message.get("details"), dict) and message["details"].get("aibb_compacted"))
    ]
    to_elide = eligible[: max(0, len(eligible) - keep_recent_results)]
    if not to_elide:
        return None

    elisions: list[ElidedArchiveResult] = []
    for index in to_elide:
        original = messages[index]
        original_json = _canonical_json(original)
        digest = hashlib.sha256(original_json.encode("utf-8")).hexdigest()
        ids = _record_ids(original.get("details"))
        id_text = ", ".join(ids) if ids else "none recorded"
        tool_name = str(original.get("toolName"))
        marker = (
            f"[Earlier {archive_title} archive result elided]\n"
            f"tool: {tool_name}\n"
            f"record_ids: {id_text}\n"
            f"Call this public {archive_title} tool again if you need its current result."
        )
        elision = ElidedArchiveResult(
            message_index=index,
            tool_name=tool_name,
            tool_call_id=original.get("toolCallId"),
            record_ids=ids,
            original_sha256=digest,
            original_bytes=len(original_json.encode("utf-8")),
            estimated_tokens=max(1, (len(original_json.encode("utf-8")) + 3) // 4),
            marker_text=marker,
        )
        elisions.append(elision)
        messages[index] = {
            **{key: value for key, value in original.items() if key not in {"content", "details"}},
            "content": [{"type": "text", "text": marker}],
            "details": {
                "aibb_compacted": True,
                "tool_name": tool_name,
                "record_ids": ids,
            },
        }

    maintenance_message = (
        f"[{archive_title} harness context maintenance {COMPACTION_NOTICE_VERSION}]\n"
        f"To keep this visit within your context window, {archive_title} shortened {len(elisions)} older archive "
        "read or search results. Those results were replaced in place by brief markers naming the public tool "
        "and any stable record IDs. No public content, draft, finished contribution, profile, allowance, curator "
        "message, original introduction, or message you wrote was changed. Recent archive results remain in full. "
        "If an elided result matters, call the public tool named in its marker again."
    )
    messages.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": maintenance_message}],
            "timestamp": int(time.time() * 1000),
        }
    )

    artifact = CompactionArtifact(
        run_id=run_id,
        created_at=datetime.now(UTC),
        authorization=authorization,
        source_event_sequence=source_event_sequence,
        source_context_generation=snapshot.context_generation,
        result_context_generation=snapshot.context_generation + 1,
        source_message_count=len(snapshot.messages),
        source_messages_sha256=_sha256(snapshot.messages),
        result_messages_sha256=_sha256(messages),
        estimated_tokens_before=estimate_message_tokens(snapshot.messages),
        estimated_tokens_after=estimate_message_tokens(messages),
        maintenance_message=maintenance_message,
        elisions=elisions,
        result_messages=messages,
    )
    compacted = snapshot.model_copy(
        update={"messages": messages, "context_generation": snapshot.context_generation + 1}
    )
    return compacted, artifact
