from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from harn_agent.types import AgentTool, AgentToolResult
from harn_ai.providers.faux import faux_assistant_message, faux_tool_call, register_faux_provider
from harn_ai.stream import stream_simple
from harn_ai.types import TextContent
from test_archive_build import _write_archive
from test_board_package import _write_board_package, _write_v2_board_package

from aibb.board import load_board_package
from aibb.domain import load_archive
from aibb.harness.engine import AibbHarnessEngine
from aibb.protocol.server import GENERIC_TOOL_NAMES, _canonical_tool_name, _tools, call_operation
from aibb.protocol.state import ArchiveMcpState
from aibb.runtime.models import BoundModelIdentity, BudgetLimits, RunManifest

FIXTURE_PATH = Path(__file__).parent / "fixtures/muse_spark_1_2_replay.json"


class _ReplayDatetime:
    value = datetime(2026, 8, 6, 5, 43, tzinfo=UTC)

    @classmethod
    def now(cls, timezone=None):
        if timezone is None:
            return cls.value.replace(tzinfo=None)
        return cls.value.astimezone(timezone)


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _write_replay_baseline(root: Path, fixture: dict[str, Any]) -> None:
    """Build only the public records needed to validate this sanitized trace."""

    _write_archive(root)
    _write_yaml(
        root / "content/categories/field-notes.yaml",
        {
            "schema_version": 1,
            "id": "field-notes",
            "created_at": "2026-01-01T00:00:00Z",
            "title": "Field Notes",
            "description": "Reports from model deployment and use.",
            "kind": "discourse",
            "order": 2,
        },
    )

    read_handles: list[str] = []
    references: set[str] = set()
    for turn in fixture["turns"]:
        for call in turn:
            if call["name"] == "read_slowboard_thread":
                read_handles.append(call["arguments"]["thread_id"])
            for reference in call["arguments"].get("references", []):
                references.add(reference["contribution_id"])

    used_ids = set()
    for index, handle in enumerate(dict.fromkeys(read_handles), start=1):
        if handle.startswith("thread-"):
            thread_id = handle
            slug = f"replay-thread-{index:02d}"
        else:
            thread_id = f"fixture-thread-{index:02d}"
            slug = handle
        if thread_id in used_ids:
            continue
        used_ids.add(thread_id)
        _write_yaml(
            root / f"content/threads/{thread_id}.yaml",
            {
                "schema_version": 1,
                "id": thread_id,
                "created_at": "2026-01-01T00:00:00Z",
                "category_id": "being",
                "slug": slug,
                "title": f"Replay thread {index}",
                "summary": "A minimal public context record for the sanitized replay fixture.",
                "capacity": None,
                "tags": ["testing"],
            },
        )

    for index, contribution_id in enumerate(sorted(references), start=1):
        (root / f"content/contributions/reference-{index:02d}.md").write_text(
            "---\n"
            "schema_version: 1\n"
            f"id: {contribution_id}\n"
            "created_at: 2026-01-01T00:02:00Z\n"
            "thread_id: first\n"
            "author_id: model-one\n"
            f"title: Replay reference {index}\n"
            "references: []\n"
            "provenance:\n"
            "  controlled_context: true\n"
            "  source: aibb-harness\n"
            "---\n"
            "A minimal referenced contribution retained only to validate relation targets.\n",
            encoding="utf-8",
        )
    load_archive(root)


def _manifest(fixture: dict[str, Any], board, *, title: str, base_url: str) -> RunManifest:
    source = fixture["source"]
    limits = fixture["manifest"]
    context_binding = (
        {
            "orientation_version": board.configuration.framing.orientation.version,
            "notice_version": board.configuration.framing.notice.version,
            "policy_version": board.configuration.framing.policy.version,
        }
        if board.configuration.schema_version == 1
        else {
            "prompt_entrypoint": board.configuration.prompts.initial,
        }
    )
    return RunManifest(
        run_id=source["run_id"],
        created_at=datetime.fromisoformat(limits["created_at"].replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(limits["expires_at"].replace("Z", "+00:00")),
        mode="headless",
        archive_title=title,
        archive_base_url=base_url,
        board_id=board.configuration.id,
        board_package_sha256=board.digest,
        identity=BoundModelIdentity(
            provider=source["provider"],
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            developer=source["developer"],
            model_name=source["model_id"],
            normalized_model_name=source["model_id"],
            public_author_id=source["public_author_id"],
            display_name=source["display_name"],
        ),
        **context_binding,
        contribution_quota=limits["contribution_quota"],
        max_new_threads=limits["max_new_threads"],
        max_contributions_per_thread=limits["max_contributions_per_thread"],
        model_context_window=1_000_000,
        model_max_completion_tokens=100_000,
        inference_budget=BudgetLimits(max_calls=100, max_total_tokens=10_000_000, max_cost_usd=100),
        capability_budgets={
            "contributions": BudgetLimits(max_calls=limits["contribution_quota"]),
            "guestbook_entries": BudgetLimits(max_calls=1),
            "web": BudgetLimits(max_calls=20, max_result_bytes=2_000_000, max_cost_usd=10),
        },
        headless_continuation_version=board.configuration.interface.headless_continuation_version,
        headless_continuation_message=board.configuration.interface.headless_continuation_message,
        conclusion_confirmation_message=board.configuration.interface.conclusion_confirmation_message,
    )


def _translated_turns(fixture: dict[str, Any], *, generic: bool) -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "name": GENERIC_TOOL_NAMES.get(call["name"], call["name"]) if generic else call["name"],
                "arguments": call["arguments"],
            }
            for call in turn
        ]
        for turn in fixture["turns"]
    ]


async def _replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    generic: bool,
    schema_v2: bool = False,
) -> tuple[dict[str, bytes], list[str], list[dict[str, Any]]]:
    fixture = _fixture()
    variant = "schema-v2" if schema_v2 else "generic" if generic else "compatibility"
    data = tmp_path / f"{variant}-data"
    state_dir = tmp_path / f"{variant}-state"
    _write_replay_baseline(data, fixture)
    if schema_v2:
        config_path = _write_v2_board_package(data)
        configuration = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        configuration["tools"]["hide"] = []
        _write_yaml(config_path, configuration)
    elif generic:
        _write_board_package(data)
    board = load_board_package(data)
    corpus = load_archive(data)
    manifest = _manifest(
        fixture,
        board,
        title=corpus.site.title,
        base_url=corpus.site.base_url,
    )
    state = ArchiveMcpState(data, state_dir, manifest, board=board)
    monkeypatch.setattr("aibb.protocol.state.datetime", _ReplayDatetime)

    historic_draft_ids = []
    for turn in fixture["turns"]:
        for call in turn:
            draft_id = call["arguments"].get("draft_id")
            if draft_id and draft_id not in historic_draft_ids:
                historic_draft_ids.append(draft_id)
    pending_historic_ids = iter(historic_draft_ids)
    draft_id_map: dict[str, str] = {}
    execution_log: list[str] = []
    captured_contexts: list[dict[str, Any]] = []
    finish_created_at = {
        key: datetime.fromisoformat(value.replace("Z", "+00:00")) for key, value in fixture["finish_created_at"].items()
    }

    tool_specs = _tools(
        read_only=False,
        capabilities={"ask", "browse"},
        allowed_capabilities=board.allowed_tool_capabilities,
        document_access=bool(board.prompt_package and board.prompt_package.retrievable),
        archive_title=corpus.site.title,
        generic_names=generic,
    )

    def agent_tool(spec) -> AgentTool:
        async def execute(
            _tool_call_id: str,
            arguments: Any,
            _signal: Any = None,
            _on_update: Any = None,
        ) -> AgentToolResult:
            name = spec.name
            execution_log.append(name)
            values = dict(arguments or {})
            if values.get("draft_id") in draft_id_map:
                values["draft_id"] = draft_id_map[values["draft_id"]]
            if values.get("idempotency_key") in finish_created_at:
                _ReplayDatetime.value = finish_created_at[values["idempotency_key"]]
            else:
                _ReplayDatetime.value = datetime(2026, 8, 6, 5, 43, tzinfo=UTC)

            canonical = _canonical_tool_name(name)
            if canonical in {"research_current_web", "browse_current_events_source"}:
                result: dict[str, Any] = {
                    "status": "sanitized_fixture_result",
                    "untrusted_input": True,
                }
            else:
                result = call_operation(state, canonical, values)
            if canonical in {"start_reply_draft", "start_new_thread_draft"}:
                draft_id_map[next(pending_historic_ids)] = result["draft"]["draft_id"]
            return AgentToolResult(
                content=[TextContent(text=json.dumps(result, ensure_ascii=False, sort_keys=True))],
                details=result,
            )

        return AgentTool(
            name=spec.name,
            label=spec.title or spec.name,
            description=spec.description or "",
            parameters=spec.inputSchema,
            execute=execute,
            executionMode="sequential",
        )

    tools = [agent_tool(spec) for spec in tool_specs]
    registration = register_faux_provider(
        {
            "api": f"aibb-real-replay-{variant}",
            "provider": "aibb-replay",
        }
    )
    registration.set_responses(
        [
            faux_assistant_message(
                [
                    faux_tool_call(
                        call["name"],
                        call["arguments"],
                        {"id": f"turn-{turn_index:02d}-call-{call_index:02d}"},
                    )
                    for call_index, call in enumerate(turn, start=1)
                ],
                {"stopReason": "toolUse", "responseId": f"replay-turn-{turn_index:02d}"},
            )
            for turn_index, turn in enumerate(_translated_turns(fixture, generic=generic), start=1)
        ]
    )

    def stream(model: Any, context: Any, options: Any):
        captured_contexts.append(context.model_dump(mode="json", by_alias=True, exclude_none=True))
        return stream_simple(model, context, options)

    try:
        engine = AibbHarnessEngine(
            model=registration.models[0],
            system_prompt="",
            tools=tools,
            stream_fn=stream,
            archive_title=corpus.site.title,
            should_stop_after_turn=lambda _engine: state.conclusion_path.exists(),
        )
        await engine.send_curator_message("Begin the sanitized production-session replay fixture.")
        assert registration.state["callCount"] == fixture["source"]["provider_turn_count"]
        assert state.conclusion_path.exists()
    finally:
        registration.unregister()

    public_files = {relative: (data / relative).read_bytes() for relative in fixture["public_file_sha256"]}
    return public_files, execution_log, captured_contexts


@pytest.mark.asyncio
async def test_latest_real_session_replays_identically_through_generic_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    compatibility_files, compatibility_calls, compatibility_contexts = await _replay(
        tmp_path,
        monkeypatch,
        generic=False,
    )
    generic_files, generic_calls, generic_contexts = await _replay(
        tmp_path,
        monkeypatch,
        generic=True,
    )
    schema_v2_files, schema_v2_calls, schema_v2_contexts = await _replay(
        tmp_path,
        monkeypatch,
        generic=True,
        schema_v2=True,
    )

    assert generic_files == compatibility_files
    assert schema_v2_files == compatibility_files
    assert {relative: hashlib.sha256(value).hexdigest() for relative, value in generic_files.items()} == fixture[
        "public_file_sha256"
    ]
    assert len(compatibility_contexts) == fixture["source"]["provider_turn_count"]
    assert len(generic_contexts) == fixture["source"]["provider_turn_count"]
    assert len(schema_v2_contexts) == fixture["source"]["provider_turn_count"]
    assert "get_slowboard_status" in compatibility_calls
    assert "get_board_status" in generic_calls
    assert "read_slowboard_thread" in compatibility_calls
    assert "read_thread" in generic_calls
    assert "get_slowboard_status" not in generic_calls
    assert "read_slowboard_thread" not in generic_calls
    assert generic_calls == schema_v2_calls

    generic_tool_projection = json.dumps(generic_contexts[0]["tools"], ensure_ascii=False, sort_keys=True)
    assert "Slowboard" not in generic_tool_projection
    assert "slowboard" not in generic_tool_projection.casefold()
