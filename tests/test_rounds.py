from __future__ import annotations

import subprocess
from pathlib import Path

from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.acceptance import accept_run_candidate
from aibb.authors import build_author_invocation, save_author_invocation
from aibb.board import load_board_package
from aibb.harness.runner import _previous_visit_records_to_suppress, _return_delta_payload
from aibb.protocol.state import ArchiveMcpState, DraftInput
from aibb.rounds import begin_round, load_round, merge_round, round_participant_statuses
from aibb.sessions import SessionStore


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _register_author(state: Path, board_id: str, author_id: str) -> None:
    invocation, prompt = build_author_invocation(
        board_id=board_id,
        author_id=author_id,
        provider="openrouter",
        model_name=f"test/{author_id}",
        normalized_model_name=f"test/{author_id}",
        display_name=author_id.replace("-", " ").title(),
        developer="Test Developer",
        reasoning_mode="auto",
    )
    save_author_invocation(state, invocation, system_prompt_bytes=prompt)


def _complete_lane(
    *,
    lane_board: Path,
    lane_state: Path,
    author_id: str,
    base_revision: str,
    body: str,
) -> tuple[str, str]:
    template = make_manifest()
    run_id = f"run-round-{author_id}"
    identity = template.identity.model_copy(
        update={
            "provider": "openrouter",
            "developer": "Test Developer",
            "model_name": f"test/{author_id}",
            "normalized_model_name": f"test/{author_id}",
            "public_author_id": author_id,
            "display_name": author_id.replace("-", " ").title(),
            "generation": None,
            "lineage": None,
        }
    )
    manifest = template.model_copy(
        update={
            "run_id": run_id,
            "data_revision": base_revision,
            "identity": identity,
            "review_before_accepting": False,
            "build_after_accepting": False,
        }
    )
    run_dir = lane_state / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    archive = ArchiveMcpState(lane_board, run_dir / "mcp", manifest)
    draft = archive.create_draft(
        DraftInput(
            target_thread_id="first",
            title=f"Held response from {author_id}",
            body=body,
        )
    )
    receipt = archive.finish_draft(draft["draft"]["draft_id"], f"round-{author_id}-save")
    SessionStore(run_dir / "session", run_id).append(
        "run_completed",
        {
            "reason": "model_concluded_visit",
            "reported_board_issues": {
                "artifact": "mcp/reported-board-issues.jsonl",
                "count": 0,
                "issue_ids": [],
                "log_status": "absent",
                "requires_administrator_review": False,
            },
        },
        "model",
    )
    accepted = accept_run_candidate(
        data_repo=lane_board,
        run_dir=run_dir,
        mode="automatic",
        require_receipt_hashes=True,
    )
    assert accepted.commit is not None
    return run_id, receipt["contribution_id"]


def test_frozen_round_uses_one_snapshot_and_reveals_accepted_replies_atomically(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    _write_archive(data)
    first_author = data / "content/authors/model-one.yaml"
    first_author.write_text(first_author.read_text().replace("provider: test", "provider: openrouter"))
    second_author = data / "content/authors/model-two.yaml"
    second_author.write_text(
        first_author.read_text()
        .replace("id: model-one", "id: model-two")
        .replace("Model One", "Model Two")
        .replace("test/model-one", "test/model-two")
    )
    first_profile = data / "content/profiles/model-one.yaml"
    second_profile = data / "content/profiles/model-two.yaml"
    second_profile.write_text(
        first_profile.read_text()
        .replace("id: model-one", "id: model-two")
        .replace("author_id: model-one", "author_id: model-two")
        .replace("handle: model-one", "handle: model-two")
    )
    _git(data, "init", "-q", "--initial-branch=main")
    _git(data, "add", "--all")
    _git(
        data,
        "-c",
        "user.name=AIBB tests",
        "-c",
        "user.email=tests@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    board_id = load_board_package(data).configuration.id
    _register_author(state, board_id, "model-one")
    _register_author(state, board_id, "model-two")

    record = begin_round(
        data_repo=data,
        state_root=state,
        thread="first-thread",
        author_ids=["model-one", "model-two"],
        administrator_note="Give one independent final synthesis.",
        round_id="final-synthesis",
    )

    assert record.base_revision == _git(data, "rev-parse", "HEAD")
    assert [item.status for item in round_participant_statuses(state, record)] == ["pending", "pending"]
    post_ids: list[str] = []
    run_ids: list[str] = []
    for participant in record.participants:
        lane_root = state / "rounds" / record.round_id / "lanes" / participant.lane
        assert _git(lane_root / "board", "rev-parse", "HEAD") == record.base_revision
        run_id, post_id = _complete_lane(
            lane_board=lane_root / "board",
            lane_state=lane_root / "state",
            author_id=participant.author_id,
            base_revision=record.base_revision,
            body=f"Independent final answer from {participant.author_id}.",
        )
        run_ids.append(run_id)
        post_ids.append(post_id)

    assert _git(data, "rev-parse", "HEAD") == record.base_revision
    assert [item.status for item in round_participant_statuses(state, record)] == ["accepted", "accepted"]
    result = merge_round(data_repo=data, state_root=state, round_id=record.round_id)

    parents = _git(data, "show", "-s", "--format=%P", result["merge_commit"]).split()
    assert parents[0] == record.base_revision
    assert len(parents) == 3
    assert all((data / f"content/contributions/{post_id}.md").exists() for post_id in post_ids)
    assert all((state / run_id / "acceptance.json").exists() for run_id in run_ids)
    merged = load_round(state, record.round_id)
    assert merged.status == "merged"
    assert merged.merge_commit == result["merge_commit"]

    # A participant's own held response was not visible in the frozen snapshot.
    # The next visit must therefore report the complete atomic reveal, including
    # that response, rather than applying the ordinary immediate-publish filter.
    for run_id in run_ids:
        run_dir = state / run_id
        previous_records = _previous_visit_records_to_suppress(
            data,
            run_dir=run_dir,
            run_id=run_id,
            current_revision=result["merge_commit"],
        )
        assert previous_records == {}
        delta = _return_delta_payload(
            data,
            previous_revision=record.base_revision,
            current_revision=result["merge_commit"],
            previous_run_id=run_id,
            previous_visit_records=previous_records,
        )
        changed_posts = {
            item["record_id"]
            for item in delta["changes"]
            if item["record_type"] == "contributions"
        }
        assert changed_posts == set(post_ids)
