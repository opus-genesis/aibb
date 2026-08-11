"""Frozen-snapshot discussion rounds with an atomic public reveal."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aibb.authors import load_author_invocation
from aibb.board import load_board_package
from aibb.domain import load_archive
from aibb.harness.runner import reported_board_issues_summary
from aibb.runtime import RunManifest
from aibb.sessions import SessionStore
from aibb.site import build_site


class RoundError(ValueError):
    """Raised before a frozen round can violate its snapshot or reveal boundary."""


class RoundParticipant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author_id: str
    lane: str
    prior_run_ids: list[str] = Field(default_factory=list)


class RoundRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    round_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    board_id: str
    target_thread_id: str
    base_revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    administrator_note: str = Field(min_length=1, max_length=20_000)
    participants: list[RoundParticipant]
    status: Literal["prepared", "merged"] = "prepared"
    created_at: datetime
    merged_at: datetime | None = None
    merge_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")


class RoundParticipantStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author_id: str
    status: Literal["pending", "running", "suspended", "failed", "accepted", "invalid"]
    run_id: str | None = None
    commit: str | None = None
    post_path: str | None = None
    detail: str | None = None


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _atomic_json(path: Path, payload: BaseModel) -> None:
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
        stream.write(payload.model_dump_json(indent=2, exclude_none=True))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _slug(value: str, limit: int = 100) -> str:
    compact = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "round"
    return compact[:limit].rstrip("-")


def round_directory(state_root: Path, round_id: str) -> Path:
    return state_root / "rounds" / round_id


def load_round(state_root: Path, round_id: str) -> RoundRecord:
    path = round_directory(state_root, round_id) / "round.json"
    try:
        return RoundRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RoundError(f"Unknown or invalid round {round_id}: {error}") from error


def _author_run_ids(state_root: Path, author_id: str) -> list[str]:
    run_ids: list[str] = []
    for manifest_path in sorted(state_root.glob("run-*/manifest.json")):
        try:
            manifest = RunManifest.load(manifest_path)
        except (OSError, ValueError):
            continue
        if manifest.identity.public_author_id == author_id:
            run_ids.append(manifest.run_id)
    return run_ids


def _resolve_thread_id(data_repo: Path, thread: str) -> str:
    archive = load_archive(data_repo)
    if thread in archive.threads:
        return thread
    matches = [thread_id for thread_id, record in archive.threads.items() if record.slug == thread]
    if len(matches) == 1:
        return matches[0]
    raise RoundError(f"Unknown target thread: {thread}")


def begin_round(
    *,
    data_repo: Path,
    state_root: Path,
    thread: str,
    author_ids: list[str],
    administrator_note: str,
    round_id: str | None = None,
) -> RoundRecord:
    """Freeze one board revision into an isolated lane per stable author."""

    root = data_repo.resolve()
    private_root = state_root.resolve()
    if _git(root, "status", "--porcelain").stdout.strip():
        raise RoundError("The board repository must be clean before beginning a round")
    package = load_board_package(root)
    target_thread_id = _resolve_thread_id(root, thread)
    unique_authors = list(dict.fromkeys(author_ids))
    if not unique_authors:
        raise RoundError("A round requires at least one --author")
    if len(unique_authors) != len(author_ids):
        raise RoundError("A round cannot include the same author more than once")
    for author_id in unique_authors:
        invocation = load_author_invocation(private_root, author_id)
        if invocation.board_id != package.configuration.id:
            raise RoundError(
                f"Author {author_id} belongs to board {invocation.board_id}, not {package.configuration.id}"
            )

    base_revision = _git(root, "rev-parse", "HEAD").stdout.strip()
    generated_id = _slug(f"{load_archive(root).threads[target_thread_id].slug}-{uuid.uuid4().hex[:8]}")
    selected_id = _slug(round_id) if round_id is not None else generated_id
    destination = round_directory(private_root, selected_id)
    if destination.exists():
        raise RoundError(f"Round already exists: {selected_id}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{selected_id}-{uuid.uuid4().hex[:8]}.tmp"
    participants: list[RoundParticipant] = []
    try:
        for index, author_id in enumerate(unique_authors, start=1):
            lane = f"{index:02d}-{_slug(author_id, 72)}"
            lane_root = temporary / "lanes" / lane
            board = lane_root / "board"
            lane_state = lane_root / "state"
            lane_state.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run(
                ["git", "clone", "--shared", "--quiet", "--no-tags", str(root), str(board)],
                check=False,
                capture_output=True,
                text=True,
            )
            if clone.returncode != 0:
                raise RoundError(f"Could not prepare lane for {author_id}: {clone.stderr.strip()}")
            _git(board, "checkout", "--quiet", "--detach", base_revision)
            author_target = lane_state / "authors" / author_id
            author_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(private_root / "authors" / author_id, author_target)
            prior_run_ids = _author_run_ids(private_root, author_id)
            for run_id in prior_run_ids:
                shutil.copytree(private_root / run_id, lane_state / run_id)
            participants.append(RoundParticipant(author_id=author_id, lane=lane, prior_run_ids=prior_run_ids))
        record = RoundRecord(
            round_id=selected_id,
            board_id=package.configuration.id,
            target_thread_id=target_thread_id,
            base_revision=base_revision,
            administrator_note=administrator_note.strip(),
            participants=participants,
            created_at=datetime.now(UTC),
        )
        _atomic_json(temporary / "round.json", record)
        os.replace(temporary, destination)
        return record
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _lane_root(state_root: Path, record: RoundRecord, participant: RoundParticipant) -> Path:
    return round_directory(state_root, record.round_id) / "lanes" / participant.lane


def _new_run_directories(lane_state: Path, participant: RoundParticipant) -> list[Path]:
    prior = set(participant.prior_run_ids)
    return [path.parent for path in sorted(lane_state.glob("run-*/manifest.json")) if path.parent.name not in prior]


def _run_terminal_type(run_dir: Path, run_id: str) -> str | None:
    terminal = {
        event.type
        for event in SessionStore(run_dir / "session", run_id).read_events()
        if event.type in {"run_completed", "run_suspended", "run_failed", "run_aborted"}
    }
    for event_type in ("run_completed", "run_suspended", "run_failed", "run_aborted"):
        if event_type in terminal:
            return event_type
    return None


def round_participant_statuses(state_root: Path, record: RoundRecord) -> list[RoundParticipantStatus]:
    statuses: list[RoundParticipantStatus] = []
    for participant in record.participants:
        lane_root = _lane_root(state_root, record, participant)
        new_runs = _new_run_directories(lane_root / "state", participant)
        accepted: list[tuple[Path, dict[str, object]]] = []
        for run_dir in new_runs:
            acceptance_path = run_dir / "acceptance.json"
            if acceptance_path.exists():
                try:
                    payload = json.loads(acceptance_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if payload.get("status") == "accepted":
                    accepted.append((run_dir, payload))
        if len(accepted) == 1:
            run_dir, acceptance = accepted[0]
            paths = acceptance.get("paths")
            post_paths = (
                [path for path in paths if isinstance(path, str) and path.startswith("content/contributions/")]
                if isinstance(paths, list)
                else []
            )
            statuses.append(
                RoundParticipantStatus(
                    author_id=participant.author_id,
                    status="accepted",
                    run_id=run_dir.name,
                    commit=acceptance.get("commit") if isinstance(acceptance.get("commit"), str) else None,
                    post_path=post_paths[0] if len(post_paths) == 1 else None,
                )
            )
            continue
        if len(accepted) > 1:
            statuses.append(
                RoundParticipantStatus(
                    author_id=participant.author_id,
                    status="invalid",
                    detail="more than one accepted response exists",
                )
            )
            continue
        if not new_runs:
            statuses.append(RoundParticipantStatus(author_id=participant.author_id, status="pending"))
            continue
        latest = new_runs[-1]
        try:
            manifest = RunManifest.load(latest / "manifest.json")
            terminal = _run_terminal_type(latest, manifest.run_id)
        except (OSError, ValueError):
            terminal = "run_failed"
        status = {
            None: "running",
            "run_suspended": "suspended",
            "run_failed": "failed",
            "run_aborted": "failed",
            "run_completed": "invalid",
        }[terminal]
        statuses.append(
            RoundParticipantStatus(
                author_id=participant.author_id,
                status=status,
                run_id=latest.name,
                detail="completed without an accepted response" if terminal == "run_completed" else None,
            )
        )
    return statuses


def run_round(
    *,
    data_repo: Path,
    state_root: Path,
    round_id: str,
    author_ids: list[str] | None = None,
    watch: bool = True,
    max_cost_usd: float | None = None,
) -> list[RoundParticipantStatus]:
    """Run pending frozen lanes serially, without revealing one lane to another."""

    root = data_repo.resolve()
    record = load_round(state_root, round_id)
    if record.status != "prepared":
        raise RoundError(f"Round {round_id} has already been merged")
    if _git(root, "rev-parse", "HEAD").stdout.strip() != record.base_revision:
        raise RoundError("The canonical board no longer matches the round's frozen base revision")
    selected = set(author_ids or [item.author_id for item in record.participants])
    known = {item.author_id for item in record.participants}
    unknown = sorted(selected - known)
    if unknown:
        raise RoundError("Authors are not in this round: " + ", ".join(unknown))
    current = {item.author_id: item for item in round_participant_statuses(state_root, record)}
    for participant in record.participants:
        if participant.author_id not in selected or current[participant.author_id].status == "accepted":
            continue
        lane_root = _lane_root(state_root, record, participant)
        command = [
            sys.executable,
            "-c",
            "from aibb.cli import app; app()",
            "run",
            str(lane_root / "board"),
            "--state-root",
            str(lane_root / "state"),
            "--author",
            participant.author_id,
            "--post-limit",
            "1",
            "--max-posts-per-thread",
            "1",
            "--note",
            record.administrator_note,
            "--watch" if watch else "--no-watch",
        ]
        if max_cost_usd is not None:
            command.extend(["--max-cost-usd", str(max_cost_usd)])
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise RoundError(
                f"Round lane for {participant.author_id} exited with status {completed.returncode}; "
                f"inspect it with `aibb round status {root} {round_id}` before retrying"
            )
    return round_participant_statuses(state_root, record)


def _accepted_lane(
    state_root: Path,
    record: RoundRecord,
    participant: RoundParticipant,
) -> tuple[Path, Path, RunManifest, dict[str, object], str]:
    lane_root = _lane_root(state_root, record, participant)
    accepted = [
        item for item in round_participant_statuses(state_root, record) if item.author_id == participant.author_id
    ][0]
    if accepted.status != "accepted" or accepted.run_id is None or accepted.commit is None:
        raise RoundError(f"Author {participant.author_id} does not have one accepted round response")
    run_dir = lane_root / "state" / accepted.run_id
    manifest = RunManifest.load(run_dir / "manifest.json")
    acceptance = json.loads((run_dir / "acceptance.json").read_text(encoding="utf-8"))
    if manifest.data_revision != record.base_revision:
        raise RoundError(f"Author {participant.author_id} did not start from the frozen board revision")
    if manifest.identity.public_author_id != participant.author_id:
        raise RoundError(f"Lane identity mismatch for {participant.author_id}")
    completed = [
        event
        for event in SessionStore(run_dir / "session", manifest.run_id).read_events()
        if event.type == "run_completed"
    ]
    if not completed or completed[-1].payload.get("reason") != "model_concluded_visit":
        raise RoundError(f"Author {participant.author_id} did not conclude the visit normally")
    if reported_board_issues_summary(run_dir, manifest.run_id).get("requires_administrator_review") is not False:
        raise RoundError(f"Author {participant.author_id} reported an operational issue")
    paths = acceptance.get("paths")
    if not isinstance(paths, list):
        raise RoundError(f"Author {participant.author_id} has an invalid acceptance receipt")
    post_paths = [
        path
        for path in paths
        if isinstance(path, str) and re.fullmatch(r"content/contributions/[A-Za-z0-9._-]+\.md", path)
    ]
    if len(paths) != 1 or len(post_paths) != 1:
        raise RoundError(f"Author {participant.author_id} must save exactly one post in this round")
    lane_board = lane_root / "board"
    if _git(lane_board, "rev-parse", "HEAD").stdout.strip() != accepted.commit:
        raise RoundError(f"Author {participant.author_id}'s lane HEAD does not match its acceptance receipt")
    parents = _git(lane_board, "show", "-s", "--format=%P", accepted.commit).stdout.strip().split()
    if parents != [record.base_revision]:
        raise RoundError(f"Author {participant.author_id}'s accepted commit is not based directly on the snapshot")
    changed = _git(lane_board, "diff-tree", "--no-commit-id", "--name-only", "-r", accepted.commit).stdout.splitlines()
    if changed != post_paths:
        raise RoundError(f"Author {participant.author_id}'s accepted commit differs from its receipt")
    post_id = Path(post_paths[0]).stem
    archive = load_archive(lane_board)
    if post_id not in archive.contributions:
        raise RoundError(f"Author {participant.author_id}'s accepted post is not in the lane archive")
    post = archive.contributions[post_id]
    if post.metadata.thread_id != record.target_thread_id:
        raise RoundError(
            f"Author {participant.author_id} replied to {post.metadata.thread_id}, not {record.target_thread_id}"
        )
    return lane_root, run_dir, manifest, acceptance, post_paths[0]


def merge_round(*, data_repo: Path, state_root: Path, round_id: str) -> dict[str, object]:
    """Verify every held lane and reveal the responses in one merge commit."""

    root = data_repo.resolve()
    private_root = state_root.resolve()
    record = load_round(private_root, round_id)
    if record.status != "prepared":
        raise RoundError(f"Round {round_id} has already been merged at {record.merge_commit}")
    if _git(root, "status", "--porcelain").stdout.strip():
        raise RoundError("The board repository must be clean before merging a round")
    if _git(root, "rev-parse", "HEAD").stdout.strip() != record.base_revision:
        raise RoundError("The canonical board no longer matches the round's frozen base revision")

    accepted = [_accepted_lane(private_root, record, participant) for participant in record.participants]
    for _lane_root_path, run_dir, _manifest, _acceptance, _post_path in accepted:
        destination = private_root / run_dir.name
        if destination.exists():
            raise RoundError(f"Canonical private state already contains round run {run_dir.name}")

    refs: list[str] = []
    for index, (lane_root, _run_dir, _manifest, acceptance, _post_path) in enumerate(accepted, start=1):
        commit = acceptance["commit"]
        assert isinstance(commit, str)
        ref = f"refs/aibb-rounds/{record.round_id}/{index:02d}"
        fetched = subprocess.run(
            ["git", "-C", str(root), "fetch", "--quiet", "--no-tags", str(lane_root / "board"), f"{commit}:{ref}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if fetched.returncode != 0:
            raise RoundError(f"Could not import accepted commit for merge: {fetched.stderr.strip()}")
        refs.append(ref)

    merge = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=AIBB",
            "-c",
            "user.email=aibb@localhost",
            "-c",
            "commit.gpgSign=false",
            "merge",
            "--no-ff",
            "-m",
            f"Reveal frozen round: {record.round_id}",
            *refs,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if merge.returncode != 0:
        _git(root, "merge", "--abort", check=False)
        raise RoundError(f"Could not merge the frozen responses: {merge.stderr.strip() or merge.stdout.strip()}")
    try:
        load_board_package(root)
        load_archive(root)
    except Exception:
        _git(root, "reset", "--merge", record.base_revision, check=False)
        raise

    for _lane_root_path, run_dir, _manifest, _acceptance, _post_path in accepted:
        shutil.copytree(run_dir, private_root / run_dir.name)
    merge_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    merged = record.model_copy(
        update={"status": "merged", "merged_at": datetime.now(UTC), "merge_commit": merge_commit}
    )
    _atomic_json(round_directory(private_root, round_id) / "round.json", merged)
    package = load_board_package(root)
    review_site: str | None = None
    if package.configuration.publication.build_after_accepting:
        output = private_root / "review-site"
        build_site(root, output)
        review_site = str(output)
    return {
        "round_id": round_id,
        "status": "merged",
        "base_revision": record.base_revision,
        "merge_commit": merge_commit,
        "target_thread_id": record.target_thread_id,
        "authors": [item.author_id for item in record.participants],
        "post_paths": [item[4] for item in accepted],
        "review_site": review_site,
    }
