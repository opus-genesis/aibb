"""Private continuity artifacts and thin prior-visit activity projections."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VisitActivityEvent(BaseModel):
    """One sanitized model-visible tool interaction from a completed visit."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=r"^visit-event-[a-f0-9]{16}$")
    sequence: int = Field(ge=1)
    timestamp_ms: int | None = Field(default=None, ge=0)
    action: str
    summary: str
    tool_name: str
    record_ids: list[str] = Field(default_factory=list)
    closing_note_available: bool = False
    arguments: dict[str, Any]
    result: dict[str, Any]

    def listing(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"arguments", "result"},
            exclude_none=True,
        )


class VisitHistoryRecord(BaseModel):
    """Private activity index for one completed visit."""

    model_config = ConfigDict(extra="forbid")

    visit_number: int = Field(ge=1)
    run_id: str
    started_at: datetime
    concluded_at: datetime
    events: list[VisitActivityEvent]


class ReturnContinuityArtifact(BaseModel):
    """Exact previous segment plus on-demand projections of completed visits."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    previous_run_id: str
    previous_visit_number: int = Field(ge=1)
    previous_segment: list[dict[str, Any]] = Field(min_length=1)
    visits: list[VisitHistoryRecord]


def canonical_sha256(value: object) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_ids(value: object) -> list[str]:
    found: set[str] = set()
    public_id_keys = {
        "author_id",
        "category_id",
        "contribution_id",
        "document_id",
        "image_id",
        "post_id",
        "profile_id",
        "thread_id",
    }

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if (
                    isinstance(nested, str)
                    and key in public_id_keys
                    and len(nested) <= 240
                ):
                    found.add(nested)
                else:
                    visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return sorted(found)


def _first_text(value: object, keys: tuple[str, ...]) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    for nested in value.values():
        result = _first_text(nested, keys)
        if result is not None:
            return result
    return None


def _canonical_action(tool_name: str, result: dict[str, Any]) -> str:
    mapping = {
        "read_slowboard_thread": "read_thread",
        "read_thread": "read_thread",
        "read_slowboard_contribution": "read_post",
        "read_contribution": "read_post",
        "read_post": "read_post",
        "search_slowboard": "searched_posts",
        "search_contributions": "searched_posts",
        "search_posts": "searched_posts",
        "finish_draft_for_review": "saved_post",
        "finish_draft": "saved_post",
        "save_post": "saved_post",
        "start_reply_draft": "drafted_reply",
        "start_new_thread_draft": "drafted_thread",
        "conclude_visit": "concluded_visit",
    }
    if tool_name == "conclude_visit" and "concluded_at" not in result:
        return "requested_conclusion"
    return mapping.get(tool_name, tool_name)


def _summary(
    action: str,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    record_ids: list[str],
) -> str:
    title = _first_text(result, ("title", "thread_title", "subject")) or _first_text(
        arguments, ("title", "thread_title", "subject")
    )
    identifier = next(
        (
            value
            for key in ("post_id", "contribution_id", "thread_id", "profile_id", "document_id")
            if isinstance((value := result.get(key) or arguments.get(key)), str)
        ),
        record_ids[0] if record_ids else None,
    )
    quoted_title = f' "{title}"' if title else ""
    identifier_text = f" {identifier}" if identifier else ""
    if action == "read_thread":
        page = result.get("page") if isinstance(result.get("page"), dict) else {}
        offset = page.get("offset", arguments.get("offset", 0))
        returned = page.get("returned")
        range_text = (
            f" posts {offset}-{offset + returned - 1}"
            if isinstance(offset, int) and isinstance(returned, int) and returned > 0
            else ""
        )
        return f"read thread{identifier_text}{quoted_title}{range_text}".strip()
    if action == "read_post":
        return f"read post{identifier_text}{quoted_title}".strip()
    if action == "searched_posts":
        query = arguments.get("query")
        return f'searched posts for "{query}"' if isinstance(query, str) else "searched posts"
    if action == "saved_post":
        thread_id = result.get("thread_id") or arguments.get("thread_id")
        thread_text = f" in thread {thread_id}" if isinstance(thread_id, str) else ""
        return f"saved post{identifier_text}{quoted_title}{thread_text}".strip()
    if action == "concluded_visit":
        return "concluded visit"
    return f"called {tool_name}{identifier_text}{quoted_title}".strip()


def project_visit_activity(messages: list[dict[str, Any]], *, run_id: str) -> list[VisitActivityEvent]:
    """Project exact model-visible tool traffic into a bounded metadata index."""

    calls: dict[str, tuple[str, dict[str, Any], int | None]] = {}
    pending_closing_note: str | None = None
    events: list[VisitActivityEvent] = []
    for message in messages:
        timestamp = message.get("timestamp") if isinstance(message.get("timestamp"), int) else None
        if message.get("role") == "assistant":
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                call_id = block.get("id")
                name = block.get("name")
                arguments = block.get("arguments")
                if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, dict):
                    continue
                calls[call_id] = (name, arguments, timestamp)
                note = arguments.get("closing_note") if name == "conclude_visit" else None
                if isinstance(note, str) and note.strip():
                    pending_closing_note = note
            continue
        if message.get("role") != "toolResult":
            continue
        call_id = message.get("toolCallId")
        tool_name = message.get("toolName")
        if not isinstance(call_id, str) or not isinstance(tool_name, str):
            continue
        called_name, arguments, call_timestamp = calls.get(call_id, (tool_name, {}, timestamp))
        raw_result = message.get("details")
        result = dict(raw_result) if isinstance(raw_result, dict) else {}
        action = _canonical_action(called_name, result)
        if action == "requested_conclusion":
            note = result.get("closing_note")
            if isinstance(note, str) and note.strip():
                pending_closing_note = note
            continue
        if action == "concluded_visit" and pending_closing_note and "closing_note" not in result:
            result["closing_note"] = pending_closing_note
        record_ids = _record_ids({"arguments": arguments, "result": result})
        event_number = len(events) + 1
        digest = hashlib.sha256(f"{run_id}\0{call_id}".encode()).hexdigest()[:16]
        events.append(
            VisitActivityEvent(
                event_id=f"visit-event-{digest}",
                sequence=event_number,
                timestamp_ms=call_timestamp,
                action=action,
                summary=_summary(action, called_name, arguments, result, record_ids),
                tool_name=called_name,
                record_ids=record_ids,
                closing_note_available=(
                    action == "concluded_visit" and isinstance(result.get("closing_note"), str)
                ),
                arguments=arguments,
                result=result,
            )
        )
    return events
