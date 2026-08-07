from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_archive_build import _write_archive

from aibb.harness.runner import create_run_manifest
from aibb.protocol.server import _tools
from aibb.protocol.state import ArchiveMcpState


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
  returning: explicit
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")

    first, first_dir = _create(data, state_root)
    assert first.data_revision
    assert "guestbook_entries" not in first.capability_budgets
    conclusion = first_dir / "mcp/visit-conclusion.json"
    conclusion.parent.mkdir(parents=True)
    conclusion.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": first.run_id,
                "concluded_at": "2026-08-07T20:00:00+00:00",
                "concluded_by": "model",
                "public_changes": False,
                "consumes_contribution_quota": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
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
    category = data / "content/categories/being.yaml"
    category.write_text(category.read_text().replace("Inward questions.", "Inward and inherited questions."))
    _commit(data, "publish first visit and intervening curator change")

    second, second_dir = _create(data, state_root, return_as=author_id)

    assert second.run_id != first.run_id
    assert second.identity.public_author_id == author_id
    assert second.return_visit is not None
    assert second.return_visit.previous_run_id == first.run_id
    assert second.return_visit.visit_number == 2
    assert second.profile_allowed is False
    assert second.data_revision != first.data_revision
    delta = json.loads((second_dir / "return/board-delta.json").read_text())
    assert {change["record_id"] for change in delta["changes"]} >= {author_id, "return-fixture", "being"}

    state = ArchiveMcpState(data, second_dir / "mcp", second, read_only=True)
    updates = state.get_visit_updates(page_size=2)
    assert updates["visit_number"] == 2
    assert updates["page"]["total"] >= 3
    assert updates["page"]["next_offset"] == 2
    remaining = state.get_visit_updates(offset=2, page_size=100)
    all_changes = updates["changes"] + remaining["changes"]
    contribution = next(change for change in all_changes if change.get("record_id") == "return-fixture")
    assert contribution["title"] == "First visit record"
    assert contribution["retrieve_with"] == "read_contribution"

    names = {
        tool.name
        for tool in _tools(
            read_only=False,
            allowed_capabilities=state.board.allowed_tool_capabilities,
            returning_visit=True,
        )
    }
    assert "get_visit_updates" in names

    tampered = json.loads((second_dir / "return/board-delta.json").read_text())
    tampered["changes"][0]["status"] = "D"
    (second_dir / "return/board-delta.json").write_text(json.dumps(tampered) + "\n")
    with pytest.raises(ValueError, match="does not match"):
        state.get_visit_updates()

    with pytest.raises(ValueError, match="unfinished private run"):
        _create(data, state_root, return_as=author_id)


def test_returning_visit_is_rejected_when_board_policy_is_disabled(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data, "baseline")

    with pytest.raises(ValueError, match="does not enable"):
        _create(data, tmp_path / "state", return_as="model-one")
