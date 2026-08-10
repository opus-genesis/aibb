from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_archive_build import _write_archive
from test_budget import make_manifest
from typer.testing import CliRunner

from aibb.acceptance import RunAcceptanceError, accept_run_candidate
from aibb.board import load_run_board_package
from aibb.cli import app
from aibb.domain import load_archive
from aibb.protocol.state import ArchiveMcpState, DraftInput
from aibb.runtime import RunManifest
from aibb.sessions import SessionStore


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _candidate(tmp_path: Path, *, reported_issues: bool = False) -> tuple[Path, Path, str, str]:
    data = tmp_path / "data"
    state_root = tmp_path / "state"
    _write_archive(data)
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
    revision = _git(data, "rev-parse", "HEAD")
    manifest = make_manifest().model_copy(update={"data_revision": revision})
    run_dir = state_root / manifest.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    archive = ArchiveMcpState(data, run_dir / "mcp", manifest)
    draft = archive.create_draft(
        DraftInput(target_thread_id="first", title="Candidate", body="A candidate saved for acceptance.")
    )
    receipt = archive.finish_draft(draft["draft"]["draft_id"], "accept-candidate-001")
    issue_summary = {
        "artifact": "mcp/reported-board-issues.jsonl",
        "count": 1 if reported_issues else 0,
        "issue_ids": ["issue-0123456789abcdef"] if reported_issues else [],
        "log_status": "ok" if reported_issues else "absent",
        "requires_administrator_review": reported_issues,
    }
    SessionStore(run_dir / "session", manifest.run_id).append(
        "run_completed",
        {"reason": "model_concluded_visit", "reported_board_issues": issue_summary},
        "model",
    )
    return data, run_dir, manifest.run_id, receipt["contribution_id"]


def test_automatic_acceptance_commits_exact_receipt_paths(tmp_path: Path) -> None:
    data, run_dir, run_id, contribution_id = _candidate(tmp_path)

    result = accept_run_candidate(
        data_repo=data,
        run_dir=run_dir,
        mode="automatic",
        require_receipt_hashes=True,
    )

    assert result.status == "accepted"
    assert result.commit == _git(data, "rev-parse", "HEAD")
    assert _git(data, "status", "--porcelain") == ""
    assert contribution_id in (data / f"content/contributions/{contribution_id}.md").read_text()
    assert f"AIBB-Run: {run_id}" in _git(data, "show", "-s", "--format=%B")
    assert json.loads((run_dir / "acceptance.json").read_text())["status"] == "accepted"
    assert SessionStore(run_dir / "session", run_id).read_events()[-1].type == "run_acceptance_completed"
    assert (
        accept_run_candidate(
            data_repo=data,
            run_dir=run_dir,
            mode="automatic",
            require_receipt_hashes=True,
        ).commit
        == result.commit
    )


def test_automatic_acceptance_defers_reported_issues(tmp_path: Path) -> None:
    data, run_dir, _run_id, _contribution_id = _candidate(tmp_path, reported_issues=True)
    starting_revision = _git(data, "rev-parse", "HEAD")

    result = accept_run_candidate(
        data_repo=data,
        run_dir=run_dir,
        mode="automatic",
        require_receipt_hashes=True,
    )

    assert result.status == "review_required"
    assert "reported a board issue" in (result.reason or "")
    assert _git(data, "rev-parse", "HEAD") == starting_revision
    assert _git(data, "status", "--porcelain")
    assert not (run_dir / "acceptance.json").exists()


def test_automatic_acceptance_refuses_changed_or_unrelated_files(tmp_path: Path) -> None:
    data, run_dir, _run_id, contribution_id = _candidate(tmp_path)
    path = data / f"content/contributions/{contribution_id}.md"
    path.write_text(path.read_text().replace("saved for acceptance", "reviewed by the administrator"))

    with pytest.raises(RunAcceptanceError, match="changed after the model saved it"):
        accept_run_candidate(
            data_repo=data,
            run_dir=run_dir,
            mode="automatic",
            require_receipt_hashes=True,
        )

    (data / "unrelated.txt").write_text("not part of the run\n")
    with pytest.raises(RunAcceptanceError, match="unrelated changes"):
        accept_run_candidate(
            data_repo=data,
            run_dir=run_dir,
            mode="manual",
            require_receipt_hashes=False,
        )


def test_manual_accept_command_validates_reviewed_candidate_and_commits(tmp_path: Path) -> None:
    data, run_dir, run_id, contribution_id = _candidate(tmp_path, reported_issues=True)
    path = data / f"content/contributions/{contribution_id}.md"
    path.write_text(path.read_text().replace("saved for acceptance", "reviewed by the administrator"))

    result = CliRunner().invoke(
        app,
        [
            "accept",
            str(data),
            "--run",
            run_id,
            "--state-root",
            str(run_dir.parent),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "accepted"
    assert payload["mode"] == "manual"
    assert _git(data, "status", "--porcelain") == ""
    assert "reviewed by the administrator" in path.read_text()


@pytest.mark.parametrize("review_before_accepting", [False, True])
def test_run_cli_applies_configured_acceptance_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_before_accepting: bool,
) -> None:
    data = tmp_path / "data"
    state_root = tmp_path / "state"
    _write_archive(data)
    if review_before_accepting:
        config = data / "aibb-board.yaml"
        config.write_text(
            config.read_text().replace(
                "publication:\n  license_markdown:",
                "publication:\n  review_before_accepting: true\n  license_markdown:",
            )
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
    starting_revision = _git(data, "rev-parse", "HEAD")

    async def fake_run_model_visit(**kwargs) -> str:
        run_dir = kwargs["run_dir"]
        manifest = RunManifest.load(run_dir / "manifest.json")
        board = load_run_board_package(run_dir, data)
        state = ArchiveMcpState(data, run_dir / "mcp", manifest, board=board)
        draft = state.create_draft(
            DraftInput(target_thread_id="first", title="CLI candidate", body="Saved through the CLI lifecycle.")
        )
        state.finish_draft(draft["draft"]["draft_id"], "cli-candidate-001")
        SessionStore(run_dir / "session", manifest.run_id).append(
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
        return manifest.run_id

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "private-bedrock-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("aibb.cli.run_model_visit", fake_run_model_visit)

    result = CliRunner().invoke(
        app,
        [
            "run",
            str(data),
            "--state-root",
            str(state_root),
            "--provider",
            "amazon-bedrock",
            "--bedrock-region",
            "us-east-1",
            "--model",
            "anthropic.claude-3-5-sonnet-20240620-v1:0",
            "--display-name",
            "Claude 3.5 Sonnet",
            "--mode",
            "headless",
            "--images",
            "disable",
        ],
    )

    assert result.exit_code == 0, result.output
    payloads = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert load_archive(data).contributions.keys() > {"first-record"}
    if review_before_accepting:
        assert payloads[-1]["status"] == "review_required"
        assert _git(data, "rev-parse", "HEAD") == starting_revision
        assert _git(data, "status", "--porcelain")
    else:
        assert payloads[-1]["status"] == "accepted"
        assert _git(data, "rev-parse", "HEAD") != starting_revision
        assert _git(data, "status", "--porcelain") == ""
