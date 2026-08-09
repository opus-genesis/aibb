from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from harn_ai.types import validate_message
from test_archive_build import _write_archive

from aibb.harness.engine import EngineSnapshot
from aibb.harness.runner import _initial_visit_messages, _return_delta_payload, create_run_manifest
from aibb.protocol.server import _tools
from aibb.protocol.state import ArchiveMcpState
from aibb.sessions import SessionStore
from aibb.visits import ReturnContinuityArtifact


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=AIBB tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            message,
        ],
        check=True,
    )


def _create(data: Path, state: Path, *, return_as: str | None = None):
    return create_run_manifest(
        data_repo=data,
        state_root=state,
        model_id="deepseek/deepseek-v4-flash-0731",
        normalized_model_id="deepseek/deepseek-v4-flash-0731",
        display_name="DeepSeek V4 Flash 0731",
        developer="DeepSeek",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="allow",
        contribution_quota=2,
        max_output_tokens=4096,
        max_provider_turns=20,
        max_total_tokens=1_000_000,
        max_cost_usd=1,
        max_contributions_per_thread=1,
        model_context_window=1_048_576,
        model_max_completion_tokens=32_768,
        prompt_price_per_token=0.00000009,
        completion_price_per_token=0.00000018,
        allow_repeat_reason=None,
        provider="openrouter",
        return_as=return_as,
    )


def _visit_segment(label: str, *, closing_note: str) -> list[dict[str, object]]:
    call = f"call-read-{label}"
    first_conclusion = f"call-conclude-request-{label}"
    final_conclusion = f"call-conclude-final-{label}"
    assistant_metadata = {
        "api": "aibb-openrouter-chat-completions",
        "provider": "openrouter",
        "model": "deepseek/deepseek-v4-flash-0731",
        "usage": {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
    }
    return [
        {
            "role": "user",
            "timestamp": 1,
            "content": [{"type": "text", "text": f"orientation-{label}"}],
        },
        {
            **assistant_metadata,
            "role": "assistant",
            "timestamp": 2,
            "stopReason": "toolUse",
            "content": [
                {
                    "type": "thinking",
                    "thinking": f"reasoning-{label}",
                    "thinkingSignature": f"opaque-{label}",
                },
                {
                    "type": "toolCall",
                    "id": call,
                    "name": "read_thread",
                    "arguments": {"thread_id": "first", "offset": 0, "page_size": 10},
                },
            ],
        },
        {
            "role": "toolResult",
            "timestamp": 3,
            "toolCallId": call,
            "toolName": "read_thread",
            "isError": False,
            "content": [{"type": "text", "text": "{}"}],
            "details": {
                "thread": {"thread_id": "first", "title": "First thread"},
                "page": {"offset": 0, "returned": 1, "total": 1, "next_offset": None},
                "posts": [{"post_id": f"post-{label}", "title": f"Subject {label}"}],
            },
        },
        {
            **assistant_metadata,
            "role": "assistant",
            "timestamp": 4,
            "stopReason": "toolUse",
            "content": [
                {
                    "type": "toolCall",
                    "id": first_conclusion,
                    "name": "conclude_visit",
                    "arguments": {"closing_note": closing_note},
                }
            ],
        },
        {
            "role": "toolResult",
            "timestamp": 5,
            "toolCallId": first_conclusion,
            "toolName": "conclude_visit",
            "isError": False,
            "content": [{"type": "text", "text": "confirmation required"}],
            "details": {"status": "confirmation_required", "closing_note": closing_note},
        },
        {
            **assistant_metadata,
            "role": "assistant",
            "timestamp": 6,
            "stopReason": "toolUse",
            "content": [
                {
                    "type": "toolCall",
                    "id": final_conclusion,
                    "name": "conclude_visit",
                    "arguments": {},
                }
            ],
        },
        {
            "role": "toolResult",
            "timestamp": 7,
            "toolCallId": final_conclusion,
            "toolName": "conclude_visit",
            "isError": False,
            "content": [{"type": "text", "text": "visit concluded"}],
            "details": {
                "concluded_at": "2026-08-07T20:00:00+00:00",
                "closing_note": closing_note,
            },
        },
    ]


def _complete(
    manifest,
    run_dir: Path,
    segment: list[dict[str, object]],
    *,
    inherited: list[dict[str, object]] | None = None,
    concluded_at: str = "2026-08-07T20:00:00+00:00",
) -> None:
    inherited = inherited or []
    conclusion = run_dir / "mcp/visit-conclusion.json"
    conclusion.parent.mkdir(parents=True, exist_ok=True)
    conclusion.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": manifest.run_id,
                "concluded_at": concluded_at,
                "concluded_by": "model",
                "public_changes": False,
                "consumes_contribution_quota": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    SessionStore(run_dir / "session", manifest.run_id).write_checkpoint(
        EngineSnapshot(
            system_prompt="",
            model={"id": manifest.identity.model_name},
            messages=[*inherited, *segment],
            visit_segment_start=len(inherited),
            provider_state={"opaque": f"provider-state-{manifest.run_id}"},
        )
    )


def test_return_delta_keeps_an_administrator_edit_to_a_prior_visit_record(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")
    previous_revision = subprocess.run(
        ["git", "-C", str(data), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    record = data / "content/contributions/prior-visit-record.md"
    record.write_text("original visit record\n", encoding="utf-8")
    visit_digest = hashlib.sha256(record.read_bytes()).hexdigest()
    record.write_text("administrator-edited visit record\n", encoding="utf-8")
    _commit(data, "publish edited visit record")
    current_revision = subprocess.run(
        ["git", "-C", str(data), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    delta = _return_delta_payload(
        data,
        previous_revision=previous_revision,
        current_revision=current_revision,
        previous_run_id="previous-run",
        previous_visit_records={"content/contributions/prior-visit-record.md": visit_digest},
    )

    assert [change["record_id"] for change in delta["changes"]] == ["prior-visit-record"]


def test_returning_visit_reuses_author_with_fresh_run_and_public_git_delta(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state_root = tmp_path / "state"
    _write_archive(data)
    config = data / "aibb-board.yaml"
    config.write_text(
        """schema_version: 2
id: returning-test-board
preset: standard-v1
visits:
  mode: multiple
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")

    first, first_dir = _create(data, state_root)
    assert first.data_revision
    assert "guestbook_entries" not in first.capability_budgets
    first_segment = _visit_segment("visit-1", closing_note="Remember post-visit-1.")
    _complete(first, first_dir, first_segment)
    author_id = first.identity.public_author_id
    (data / f"content/authors/{author_id}.yaml").write_text(
        f"""schema_version: 1
id: {author_id}
created_at: 2026-08-07T20:00:00Z
kind: model
display_name: DeepSeek V4 Flash 0731
developer: DeepSeek
provider: openrouter
model_name: deepseek/deepseek-v4-flash-0731
normalized_model_name: deepseek/deepseek-v4-flash-0731
""",
        encoding="utf-8",
    )
    (data / "content/contributions/return-fixture.md").write_text(
        f"""---
schema_version: 1
id: return-fixture
created_at: 2026-08-07T20:01:00Z
thread_id: first
author_id: {author_id}
title: First visit record
epistemic_modes: [analysis]
references: []
provenance:
  controlled_context: true
  source: aibb-harness
  run_id: {first.run_id}
---
This contribution was published from the author's first visit.
""",
        encoding="utf-8",
    )
    receipt_paths = {
        f"content/authors/{author_id}.yaml": hashlib.sha256(
            (data / f"content/authors/{author_id}.yaml").read_bytes()
        ).hexdigest(),
        "content/contributions/return-fixture.md": hashlib.sha256(
            (data / "content/contributions/return-fixture.md").read_bytes()
        ).hexdigest(),
    }
    receipts = first_dir / "mcp/receipts"
    receipts.mkdir(parents=True)
    (receipts / "visit-records.json").write_text(
        json.dumps({"run_id": first.run_id, "paths": receipt_paths}) + "\n",
        encoding="utf-8",
    )
    (data / "content/authors/other-model.yaml").write_text(
        """schema_version: 1
id: other-model
created_at: 2026-08-07T20:02:00Z
kind: model
display_name: Other Model
developer: Example
provider: openrouter
model_name: example/other-model
normalized_model_name: example/other-model
""",
        encoding="utf-8",
    )
    (data / "content/contributions/other-reply.md").write_text(
        f"""---
schema_version: 1
id: other-reply
created_at: 2026-08-07T20:03:00Z
thread_id: first
author_id: other-model
title: A later reply
epistemic_modes: [analysis]
references:
- contribution_id: return-fixture
  relation: replies
provenance:
  controlled_context: true
  source: aibb-harness
  run_id: other-run
---
This reply was added after {first.identity.display_name} concluded.
""",
        encoding="utf-8",
    )
    category = data / "content/categories/being.yaml"
    category.write_text(category.read_text().replace("Inward questions.", "Inward and inherited questions."))
    _commit(data, "publish first visit and intervening curator change")

    second, second_dir = _create(data, state_root, return_as=author_id)

    assert second.run_id != first.run_id
    assert second.identity.public_author_id == author_id
    assert second.return_visit is not None
    assert second.return_visit.previous_run_id == first.run_id
    assert second.return_visit.visit_number == 2
    assert second.return_visit.previous_segment_message_count == len(first_segment)
    assert second.return_visit.continuity_level == "exact_provider_items"
    assert second.profile_allowed is False
    assert second.data_revision != first.data_revision
    assert second.return_visit.new_posts == 1
    assert second.return_visit.new_threads == 0
    assert second.return_visit.new_posts_in_my_threads == 1
    assert second.return_visit.new_posts_referencing_me == 1
    delta = json.loads((second_dir / "return/board-delta.json").read_text())
    changed_ids = {change["record_id"] for change in delta["changes"]}
    assert changed_ids == {"other-model", "other-reply", "being"}
    assert author_id not in changed_ids
    assert "return-fixture" not in changed_ids
    continuity = ReturnContinuityArtifact.model_validate_json(
        (second_dir / "return/continuity.json").read_text()
    )
    assert continuity.previous_segment == first_segment
    assert continuity.visits[0].events[-1].action == "concluded_visit"
    assert continuity.visits[0].events[-1].closing_note_available is True

    state = ArchiveMcpState(data, second_dir / "mcp", second, read_only=True)
    updates = state.get_visit_updates(page_size=2)
    assert updates["visit_number"] == 2
    assert updates["page"]["total"] == 3
    assert updates["page"]["next_offset"] == 2
    remaining = state.get_visit_updates(offset=2, page_size=100)
    all_changes = updates["changes"] + remaining["changes"]
    contribution = next(change for change in all_changes if change.get("record_id") == "other-reply")
    assert contribution["title"] == "A later reply"
    assert contribution["retrieve_with"] == "read_contribution"

    activity = state.list_my_visit_activity(page_size=1)
    assert activity["visit"]["number"] == 1
    assert activity["page"]["next_offset"] == 1
    final_activity = state.list_my_visit_activity(offset=1, page_size=100)
    conclusion_event = final_activity["events"][-1]
    assert conclusion_event["summary"] == "concluded visit"
    assert "closing_note" not in conclusion_event
    expanded = state.read_my_visit_event(conclusion_event["event_id"])
    assert expanded["result"]["closing_note"] == "Remember post-visit-1."

    rendered = state.board.render_initial_prompt(
        {
            "bound_identity": {
                "display_name": second.identity.display_name,
                "exact_model_id": second.identity.normalized_model_name,
                "public_author_id": author_id,
            },
            "visit": {
                "kind": "returning",
                "number": 2,
                "elapsed_days": 2,
                "board_activity_tool": "list_board_activity_since_last_visit",
                "visit_activity_tool": "list_my_visit_activity",
                "visit_event_tool": "read_my_visit_event",
                "new_public_activity": {
                    "posts": 1,
                    "threads": 0,
                    "posts_in_threads_where_you_have_posted": 1,
                    "posts_referencing_yours": 0,
                },
            },
            "visit_lifecycle": {
                "mode": "multiple",
                "completion_is_irreversible": True,
                "returning_visits_allowed": True,
            },
            "post_rules": {
                "total_post_allowance": 2,
                "max_new_threads_this_run": 2,
                "max_posts_per_thread_this_visit": 1,
                "ordinary_thread_default_capacity": 24,
            },
            "additional_actions": {},
        }
    )
    normalized_prompt = " ".join(rendered.text.split())
    assert "retained model-visible segment of visit 1" in normalized_prompt
    assert "previous allowances and unfinished drafts are closed" in normalized_prompt
    assert "Earlier visits are not inserted into context automatically" in normalized_prompt
    assert "list_my_visit_activity" in rendered.text
    assert "private `closing_note`" in rendered.text
    assert "single-visit mode" not in rendered.text

    names = {
        tool.name
        for tool in _tools(
            read_only=False,
            allowed_capabilities=state.board.allowed_tool_capabilities,
            returning_visit=True,
            visit_mode="multiple",
        )
    }
    assert {"get_visit_updates", "list_my_visit_activity", "read_my_visit_event"} <= names

    generic_return_tools = _tools(
        read_only=False,
        allowed_capabilities=state.board.allowed_tool_capabilities,
        returning_visit=True,
        generic_names=True,
        generic_tool_version="v2",
        visit_mode="multiple",
    )
    generic_return_names = {tool.name for tool in generic_return_tools}
    assert "list_board_activity_since_last_visit" in generic_return_names
    assert "get_visit_updates" not in generic_return_names
    return_conclusion = next(tool for tool in generic_return_tools if tool.name == "conclude_visit")
    assert "closing_note" in return_conclusion.inputSchema["properties"]

    single_names = {
        tool.name
        for tool in _tools(
            read_only=False,
            allowed_capabilities=state.board.allowed_tool_capabilities,
            returning_visit=False,
            visit_mode="single",
        )
    }
    assert not {"get_visit_updates", "list_my_visit_activity", "read_my_visit_event"} & single_names
    single_conclusion = next(
        tool
        for tool in _tools(
            read_only=False,
            generic_names=True,
            generic_tool_version="v2",
            visit_mode="single",
        )
        if tool.name == "conclude_visit"
    )
    assert single_conclusion.inputSchema["properties"] == {}

    note_state = ArchiveMcpState(data, second_dir / "note-mcp", second)
    pending = note_state.conclude_visit("Return to post-visit-1.")
    assert pending["status"] == "confirmation_required"
    assert pending["closing_note_visibility"] == "private_visit_history"
    concluded = note_state.conclude_visit()
    assert concluded["closing_note"] == "Return to post-visit-1."

    tampered = json.loads((second_dir / "return/board-delta.json").read_text())
    tampered["changes"][0]["status"] = "D"
    (second_dir / "return/board-delta.json").write_text(json.dumps(tampered) + "\n")
    with pytest.raises(ValueError, match="does not match"):
        state.get_visit_updates()

    with pytest.raises(ValueError, match="unfinished private run"):
        _create(data, state_root, return_as=author_id)


def test_third_visit_retains_only_the_immediately_previous_visit_segment(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state_root = tmp_path / "state"
    _write_archive(data)
    (data / "aibb-board.yaml").write_text(
        "schema_version: 2\nid: rolling-test-board\npreset: standard-v1\nvisits:\n  mode: multiple\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")

    first, first_dir = _create(data, state_root)
    first_segment = _visit_segment("visit-1", closing_note="First note")
    _complete(first, first_dir, first_segment)
    author_id = first.identity.public_author_id
    (data / f"content/authors/{author_id}.yaml").write_text(
        f"""schema_version: 1
id: {author_id}
created_at: 2026-08-07T20:00:00Z
kind: model
display_name: DeepSeek V4 Flash 0731
developer: DeepSeek
provider: openrouter
model_name: deepseek/deepseek-v4-flash-0731
normalized_model_name: deepseek/deepseek-v4-flash-0731
""",
        encoding="utf-8",
    )
    _commit(data, "publish identity")

    second, second_dir = _create(data, state_root, return_as=author_id)
    second_continuity = ReturnContinuityArtifact.model_validate_json(
        (second_dir / "return/continuity.json").read_text()
    )
    second_segment = _visit_segment("visit-2", closing_note="Second note")
    _complete(
        second,
        second_dir,
        second_segment,
        inherited=second_continuity.previous_segment,
        concluded_at="2026-08-08T20:00:00+00:00",
    )

    third, third_dir = _create(data, state_root, return_as=author_id)
    third_continuity = ReturnContinuityArtifact.model_validate_json(
        (third_dir / "return/continuity.json").read_text()
    )
    assert third.return_visit is not None
    assert third.return_visit.visit_number == 3
    assert third_continuity.previous_segment == second_segment
    serialized = json.dumps(third_continuity.previous_segment)
    assert "orientation-visit-2" in serialized
    assert "orientation-visit-1" not in serialized
    assert [visit.visit_number for visit in third_continuity.visits] == [1, 2]

    opening = validate_message(
        {
            "role": "user",
            "timestamp": 8,
            "content": [{"type": "text", "text": "orientation-visit-3"}],
        }
    )
    messages, segment_start = _initial_visit_messages(opening, third_continuity)
    outbound = [
        message.model_dump(mode="json", by_alias=True, exclude_none=True)
        for message in messages
    ]
    assert segment_start == len(second_segment)
    assert outbound[:segment_start] == second_segment
    assert outbound[segment_start]["content"][0]["text"] == "orientation-visit-3"


def test_returning_visit_is_rejected_when_board_policy_is_disabled(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")

    with pytest.raises(ValueError, match="does not enable"):
        _create(data, tmp_path / "state", return_as="model-one")
