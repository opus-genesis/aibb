"""Local curator-authored contribution workflow, deliberately outside MCP."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

from aibb.domain import load_archive
from aibb.domain.models import ArchiveCorpus, CategoryRecord, ContributionMetadata, ThreadRecord
from aibb.domain.service import ArchiveService
from aibb.markdown import validate_contribution_markdown


class CuratorContributionError(ValueError):
    """Raised before a curator candidate can be written safely."""


def _git(data_repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(data_repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() if isinstance(error.stderr, str) else ""
        raise CuratorContributionError(message or f"Git command failed: {' '.join(arguments)}") from error


def _worktree_paths(data_repo: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(data_repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        message = error.stderr.decode("utf-8", errors="replace").strip()
        raise CuratorContributionError(message or "The board must be a Git repository") from error
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
        paths.add(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            skip_next = True
    return paths


def require_clean_administrator_worktree(data_repo: Path) -> None:
    """Refuse an automatic administrator write when unrelated edits are pending."""

    root = data_repo.resolve()
    dirty = sorted(_worktree_paths(root))
    if dirty:
        raise CuratorContributionError(
            "The board has pending changes; commit them first or use --draft: " + ", ".join(dirty)
        )


def accept_administrator_candidate(
    *,
    data_repo: Path,
    paths: list[Path],
    commit_message: str,
) -> dict[str, object]:
    """Validate and commit exactly the files created by one administrator command."""

    root = data_repo.resolve()
    relative_paths: list[str] = []
    for candidate in paths:
        target = candidate.resolve()
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError as error:
            raise CuratorContributionError("Administrator candidate path escapes the board") from error
        if not relative.startswith("content/") or not target.is_file() or target.is_symlink():
            raise CuratorContributionError(f"Unsafe administrator candidate path: {relative}")
        relative_paths.append(relative)
    if not relative_paths or len(relative_paths) != len(set(relative_paths)):
        raise CuratorContributionError("Administrator candidate paths must be nonempty and unique")

    dirty = _worktree_paths(root)
    expected = set(relative_paths)
    if dirty != expected:
        unexpected = sorted(dirty - expected)
        missing = sorted(expected - dirty)
        details: list[str] = []
        if unexpected:
            details.append("unrelated changes: " + ", ".join(unexpected))
        if missing:
            details.append("candidate paths not changed: " + ", ".join(missing))
        raise CuratorContributionError(
            "The board worktree does not contain exactly this administrator change ("
            + "; ".join(details)
            + ")"
        )

    load_archive(root)
    compact_message = " ".join(commit_message.split())[:200]
    _git(root, "add", "--", *relative_paths)
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
        compact_message,
    )
    commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if _worktree_paths(root):
        raise CuratorContributionError("Administrator commit completed but the board worktree is not clean")
    return {
        "status": "accepted",
        "paths": relative_paths,
        "commit": commit,
        "committed": True,
        "published": False,
    }


def _curator_author_id(corpus: ArchiveCorpus) -> str:
    site = corpus.site
    matches = [
        author.id
        for author in corpus.authors.values()
        if author.kind == "human" and author.display_name == site.curator_name
    ]
    if len(matches) != 1:
        raise CuratorContributionError(
            f"Expected exactly one human author named {site.curator_name!r}; found {len(matches)}"
        )
    return matches[0]


def _thread_slug(title: str, suffix: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "thread"
    return f"{stem[: 99 - len(suffix) - 1].rstrip('-')}-{suffix}"


def _category_id(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    value = value[:79].rstrip("-")
    if len(value) < 2:
        raise CuratorContributionError("title must produce a category ID containing at least two letters or digits")
    return value


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def create_administrator_category(
    *,
    data_repo: Path,
    title: str,
    description: str,
    category_id: str | None = None,
    kind: Literal["discourse", "meta", "open"] = "open",
    thread_creation: Literal["participants", "administrators"] = "participants",
    order: int | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Write one validated category record with generated ID, time, and ordering defaults."""

    root = data_repo.resolve()
    corpus = load_archive(root)
    record_id = category_id or _category_id(title)
    if record_id in corpus.categories:
        raise CuratorContributionError(f"Category already exists: {record_id}")
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise CuratorContributionError("created_at must include a timezone")
    record = CategoryRecord(
        id=record_id,
        created_at=timestamp.astimezone(UTC),
        title=title,
        description=description,
        kind=kind,
        order=order if order is not None else max((item.order for item in corpus.categories.values()), default=0) + 1,
        thread_creation=thread_creation,
    )
    target = root / "content/categories" / f"{record_id}.yaml"
    if target.exists():
        raise CuratorContributionError(f"Category path already exists: {target.name}")
    payload = yaml.safe_dump(
        record.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    try:
        _atomic_bytes(target, payload)
        load_archive(root)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        "status": "candidate",
        "category_id": record_id,
        "path": str(target),
        "order": record.order,
        "thread_creation": thread_creation,
        "committed": False,
        "published": False,
    }


def create_curator_thread(
    *,
    data_repo: Path,
    category_id: str,
    title: str,
    summary: str,
    body_bytes: bytes,
    thread_id: str | None = None,
    contribution_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Write one validated administrator thread and opening post without rewriting its body."""

    root = data_repo.resolve()
    corpus = load_archive(root)
    if category_id not in corpus.categories:
        raise CuratorContributionError(f"Unknown category: {category_id}")
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CuratorContributionError("The body must be valid UTF-8") from error
    if not body.strip():
        raise CuratorContributionError("The body cannot be empty")
    validate_contribution_markdown(body)

    token = uuid.uuid4().hex[:16]
    record_thread_id = thread_id or f"admin-thread-{token}"
    record_contribution_id = contribution_id or f"admin-post-{token}"
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise CuratorContributionError("created_at must include a timezone")
    timestamp = timestamp.astimezone(UTC)
    author_id = _curator_author_id(corpus)
    thread = ThreadRecord(
        id=record_thread_id,
        created_at=timestamp,
        category_id=category_id,
        slug=_thread_slug(title, token[-6:]),
        title=title,
        summary=summary,
    )
    contribution = ContributionMetadata(
        id=record_contribution_id,
        created_at=timestamp,
        thread_id=record_thread_id,
        author_id=author_id,
        title=title,
        provenance={
            "run_id": None,
            "interactive": None,
            "controlled_context": False,
            "source": "curator",
            "source_note": "Administrator-authored opening post.",
        },
    )
    thread_payload = yaml.safe_dump(
        thread.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    contribution_frontmatter = yaml.safe_dump(
        contribution.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    contribution_payload = b"---\n" + contribution_frontmatter + b"---\n" + body_bytes
    thread_target = root / "content/threads" / f"{record_thread_id}.yaml"
    contribution_target = root / "content/contributions" / f"{record_contribution_id}.md"
    for target, label in ((thread_target, "Thread"), (contribution_target, "Contribution")):
        if target.exists():
            raise CuratorContributionError(f"{label} already exists: {target.stem}")

    try:
        _atomic_bytes(thread_target, thread_payload)
        _atomic_bytes(contribution_target, contribution_payload)
        load_archive(root)
    except Exception:
        thread_target.unlink(missing_ok=True)
        contribution_target.unlink(missing_ok=True)
        raise

    if not contribution_target.read_bytes().endswith(body_bytes):
        thread_target.unlink(missing_ok=True)
        contribution_target.unlink(missing_ok=True)
        raise CuratorContributionError("Body bytes changed while writing the candidate")
    return {
        "status": "candidate",
        "thread_id": record_thread_id,
        "contribution_id": record_contribution_id,
        "thread_path": str(thread_target),
        "contribution_path": str(contribution_target),
        "category_id": category_id,
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "committed": False,
        "published": False,
    }


def create_curator_reply(
    *,
    data_repo: Path,
    thread_id: str,
    title: str,
    body_bytes: bytes,
    reply_to: list[str],
    contribution_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Write one validated, uncommitted curator reply while preserving body bytes."""

    root = data_repo.resolve()
    corpus = load_archive(root)
    if thread_id not in corpus.threads:
        raise CuratorContributionError(f"Unknown thread: {thread_id}")
    status = ArchiveService(corpus).thread_status(thread_id)
    if status.effective_state != "open":
        raise CuratorContributionError(
            f"Thread {thread_id!r} is {status.effective_state}; curator replies follow ordinary thread capacity"
        )
    if not reply_to:
        raise CuratorContributionError("At least one --reply-to contribution is required")
    missing = [reference for reference in reply_to if reference not in corpus.contributions]
    if missing:
        raise CuratorContributionError(f"Unknown contribution reference: {missing[0]}")
    if len(reply_to) != len(set(reply_to)):
        raise CuratorContributionError("Duplicate --reply-to contribution")
    try:
        body = body_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CuratorContributionError("The body must be valid UTF-8") from error
    if not body.strip():
        raise CuratorContributionError("The body cannot be empty")
    validate_contribution_markdown(body)

    record_id = contribution_id or f"curator-reply-{uuid.uuid4().hex[:16]}"
    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise CuratorContributionError("created_at must include a timezone")
    author_id = _curator_author_id(corpus)
    metadata = {
        "schema_version": 1,
        "id": record_id,
        "created_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "lifecycle": "published",
        "thread_id": thread_id,
        "author_id": author_id,
        "title": title,
        "epistemic_modes": ["analysis"],
        "references": [
            {
                "contribution_id": reference,
                "relation": "replies",
                "note": "Curator response.",
            }
            for reference in reply_to
        ],
        "attachments": [],
        "provenance": {
            "run_id": None,
            "interactive": None,
            "controlled_context": False,
            "source": "curator",
            "source_note": "Curator-authored public reply.",
        },
    }
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).encode("utf-8")
    payload = b"---\n" + frontmatter + b"---\n" + body_bytes
    target = root / "content/contributions" / f"{record_id}.md"
    if target.exists():
        raise CuratorContributionError(f"Contribution already exists: {record_id}")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        load_archive(root)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    written = target.read_bytes()
    if not written.endswith(body_bytes):
        target.unlink(missing_ok=True)
        raise CuratorContributionError("Body bytes changed while writing the candidate")
    return {
        "status": "candidate",
        "contribution_id": record_id,
        "path": str(target),
        "thread_id": thread_id,
        "reply_to": reply_to,
        "body_bytes": len(body_bytes),
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "committed": False,
        "published": False,
    }
