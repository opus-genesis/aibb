"""Validate and accept one completed model visit into the board's Git history."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aibb.board import load_board_package
from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.domain import load_archive
from aibb.harness.runner import reported_board_issues_summary
from aibb.runtime import RunManifest
from aibb.sessions import SessionStore


class RunAcceptanceError(ValueError):
    """Raised when a run cannot be accepted without crossing a review boundary."""


class RunAcceptanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    status: Literal["accepted", "no_candidate", "review_required"]
    mode: Literal["automatic", "manual"]
    paths: list[str] = Field(default_factory=list)
    commit: str | None = None
    reason: str | None = None
    accepted_at: datetime | None = None


def _git(data_repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(data_repo), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}-",
        suffix=".tmp",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _worktree_paths(data_repo: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(data_repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True,
        capture_output=True,
    )
    paths: set[str] = set()
    entries = result.stdout.decode("utf-8", errors="strict").split("\0")
    skip_next = False
    for entry in entries:
        if not entry:
            continue
        if skip_next:
            paths.add(entry)
            skip_next = False
            continue
        status = entry[:2]
        paths.add(entry[3:])
        if "R" in status or "C" in status:
            skip_next = True
    return paths


def _completed_event(run_dir: Path, run_id: str):
    events = SessionStore(run_dir / "session", run_id).read_events()
    completed = [event for event in events if event.type == "run_completed"]
    if not completed:
        raise RunAcceptanceError("Only a completed visit can be accepted")
    return completed[-1]


def _candidate_paths(run_dir: Path, run_id: str) -> tuple[list[str], dict[str, str]]:
    paths: dict[str, str] = {}
    receipts_dir = run_dir / "mcp/receipts"
    for receipt_path in sorted(receipts_dir.glob("*.json")):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RunAcceptanceError(f"Unreadable run receipt: {receipt_path.name}") from error
        if receipt.get("run_id") != run_id:
            raise RunAcceptanceError(f"Receipt {receipt_path.name} belongs to another run")
        receipt_paths = receipt.get("paths")
        if not isinstance(receipt_paths, dict):
            raise RunAcceptanceError(f"Receipt {receipt_path.name} has no candidate paths")
        for raw_path, raw_digest in receipt_paths.items():
            path = PurePosixPath(raw_path) if isinstance(raw_path, str) else PurePosixPath("")
            if (
                not isinstance(raw_path, str)
                or not isinstance(raw_digest, str)
                or len(raw_digest) != 64
                or any(character not in "0123456789abcdef" for character in raw_digest)
                or path.is_absolute()
                or len(path.parts) < 2
                or path.parts[0] != "content"
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise RunAcceptanceError(f"Receipt {receipt_path.name} contains an unsafe candidate path")
            existing = paths.get(raw_path)
            if existing is not None and existing != raw_digest:
                raise RunAcceptanceError(f"Conflicting receipts exist for candidate path {raw_path}")
            paths[raw_path] = raw_digest
    return sorted(paths), paths


def run_candidate_paths(run_dir: Path) -> list[str]:
    """List receipt-bound candidate paths for one completed run without changing board state."""

    private_run = run_dir.resolve()
    manifest = RunManifest.load(private_run / "manifest.json")
    _completed_event(private_run, manifest.run_id)
    paths, _digests = _candidate_paths(private_run, manifest.run_id)
    return paths


def _validate_candidate(data_repo: Path) -> None:
    verify_archive_compatibility(load_archive_config(data_repo))
    load_board_package(data_repo)
    load_archive(data_repo)


def _commit_message(display_name: str) -> str:
    compact = " ".join(display_name.split())
    return f"Accept posts from {compact[:160]}"


def accept_run_candidate(
    *,
    data_repo: Path,
    run_dir: Path,
    mode: Literal["automatic", "manual"],
    require_receipt_hashes: bool,
) -> RunAcceptanceResult:
    """Commit exactly one completed run's validated candidate paths, or explain why review is required."""

    root = data_repo.resolve()
    private_run = run_dir.resolve()
    manifest = RunManifest.load(private_run / "manifest.json")
    artifact_path = private_run / "acceptance.json"
    if artifact_path.exists():
        return RunAcceptanceResult.model_validate_json(artifact_path.read_text(encoding="utf-8"))

    completed = _completed_event(private_run, manifest.run_id)
    if completed.payload.get("reason") not in {"model_concluded_visit", "curator"}:
        raise RunAcceptanceError("The visit did not reach an accept-ready completion boundary")
    current_revision = _git(root, "rev-parse", "HEAD").stdout.strip()
    if manifest.data_revision != current_revision:
        raise RunAcceptanceError(
            "The board HEAD no longer matches the revision from which this visit started"
        )

    paths, expected_digests = _candidate_paths(private_run, manifest.run_id)
    if not paths:
        result = RunAcceptanceResult(
            run_id=manifest.run_id,
            status="no_candidate",
            mode=mode,
            reason="The visit completed without saving a post or profile.",
        )
        _atomic_json(artifact_path, result.model_dump(mode="json", exclude_none=True))
        SessionStore(private_run / "session", manifest.run_id).append(
            "run_acceptance_completed",
            result.model_dump(mode="json", exclude_none=True),
            "operator",
        )
        return result

    issue_summary = completed.payload.get("reported_board_issues")
    if not isinstance(issue_summary, dict):
        issue_summary = reported_board_issues_summary(private_run, manifest.run_id)
    if mode == "automatic" and issue_summary.get("requires_administrator_review") is not False:
        return RunAcceptanceResult(
            run_id=manifest.run_id,
            status="review_required",
            mode=mode,
            paths=paths,
            reason="The visit reported a board issue that requires administrator review.",
        )

    dirty_paths = _worktree_paths(root)
    expected_paths = set(paths)
    if dirty_paths != expected_paths:
        unexpected = sorted(dirty_paths - expected_paths)
        missing = sorted(expected_paths - dirty_paths)
        detail = []
        if unexpected:
            detail.append("unrelated changes: " + ", ".join(unexpected))
        if missing:
            detail.append("candidate paths no longer dirty: " + ", ".join(missing))
        raise RunAcceptanceError(
            "The board worktree does not contain exactly this run's candidate ("
            + "; ".join(detail)
            + ")"
        )

    for path in paths:
        target = root / path
        if target.is_symlink() or not target.is_file():
            raise RunAcceptanceError(f"Candidate path is missing or is not a file: {path}")
        if require_receipt_hashes:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected_digests[path]:
                raise RunAcceptanceError(f"Candidate path changed after the model saved it: {path}")

    _validate_candidate(root)
    _git(root, "add", "--", *paths)
    _git(
        root,
        "-c",
        "user.name=AIBB",
        "-c",
        "user.email=aibb@localhost",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-m",
        _commit_message(manifest.identity.display_name),
        "-m",
        f"AIBB-Run: {manifest.run_id}",
    )
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if _worktree_paths(root):
        raise RunAcceptanceError("Candidate commit completed but the board worktree is not clean")
    result = RunAcceptanceResult(
        run_id=manifest.run_id,
        status="accepted",
        mode=mode,
        paths=paths,
        commit=commit,
        accepted_at=datetime.now(UTC),
    )
    _atomic_json(artifact_path, result.model_dump(mode="json", exclude_none=True))
    SessionStore(private_run / "session", manifest.run_id).append(
        "run_acceptance_completed",
        result.model_dump(mode="json", exclude_none=True),
        "operator",
    )
    return result
