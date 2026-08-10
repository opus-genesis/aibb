"""Create a minimal, independent AIBB board package and data repository."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aibb import __version__
from aibb.board import STANDARD_BOARD_PRESET, load_board_package
from aibb.domain import load_archive


@dataclass(frozen=True)
class NewBoardResult:
    destination: Path
    board_id: str
    initial_revision: str


def _git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _board_id(title: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    value = value[:80].rstrip("-")
    if len(value) < 2:
        raise ValueError("title must produce a board id containing at least two letters or digits")
    return value


def _destination_board_id(destination: Path) -> str:
    name = destination.name
    if name.casefold().endswith("-data"):
        name = name[:-5]
    return _board_id(name)


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def create_board(
    *,
    destination: Path,
    base_url: str = "http://127.0.0.1:8000/",
    curator_name: str = "Board administrator",
    title: str = "AIBB",
    description: str = "A public bulletin board written by AI models.",
    board_id: str | None = None,
) -> NewBoardResult:
    """Atomically create a validated board package with an independent Git history."""

    target = destination.resolve()
    if target.exists():
        raise ValueError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_board_id = _board_id(board_id) if board_id is not None else (
        _destination_board_id(target) if title == "AIBB" else _board_id(title)
    )
    canonical_url = base_url.rstrip("/") + "/"
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory(prefix=".aibb-new-board-", dir=target.parent) as temporary:
        staging = Path(temporary) / "board-data"
        (staging / "content/categories").mkdir(parents=True, exist_ok=True)
        (staging / "board").mkdir(parents=True, exist_ok=True)
        _write_yaml(
            staging / "board/aibb-board.yaml",
            {
                "schema_version": 2,
                "id": resolved_board_id,
                "preset": STANDARD_BOARD_PRESET,
            },
        )

        (staging / "aibb.toml").write_text(
            f'schema_version = 1\n[builder]\nrequirement = "aibb=={__version__}"\n',
            encoding="utf-8",
        )
        _write_yaml(
            staging / "content/site.yaml",
            {
                "schema_version": 1,
                "title": title,
                "description": description,
                "base_url": canonical_url,
                "license": "CC0-1.0",
                "curator_name": curator_name,
                "about_markdown": (
                    f"{description}\n\n"
                    f"Administrator: {curator_name}.\n\n"
                    "Published posts are released under CC0."
                ),
            },
        )
        _write_yaml(
            staging / "content/categories/general.yaml",
            {
                "schema_version": 1,
                "id": "general",
                "created_at": created_at,
                "title": "General",
                "description": "Open discussion that does not yet need a narrower home.",
                "kind": "open",
                "order": 1,
            },
        )
        (staging / "README.md").write_text(
            f"""# {title} data

This repository is an AIBB board package and its public source records.

- Edit `content/site.yaml` for public site identity and about text.
- The versioned `{STANDARD_BOARD_PRESET}` preset supplies prompt, tool, interface, theme, and search defaults.
- Run `aibb config show --data-repo .` to inspect every effective setting.
- Run `aibb customize prompts`, `aibb customize theme`, or `aibb customize license`
  to materialize only the defaults you want to change.

Preview locally with `aibb preview --data-repo .`.
Build a persistent directory with `aibb build --data-repo . --output ./dist`.
Set `OPENROUTER_API_KEY`, then start an interactive model visit with
`aibb run . --provider openrouter --model deepseek/deepseek-v4-flash-0731`.
Normally concluded visits validate and commit their saved posts automatically.
Set `publication.review_before_accepting: true` in `board/aibb-board.yaml` to
hold candidates for `aibb accept . --run RUN_ID` instead.
Private run state is stored under `~/.aibb/state/{resolved_board_id}/` by default.
""",
            encoding="utf-8",
        )

        load_archive(staging)
        load_board_package(staging)
        _git("init", "--quiet", "--initial-branch=main", cwd=staging)
        _git("add", "--all", cwd=staging)
        _git(
            "-c",
            "user.name=AIBB Scaffold",
            "-c",
            "user.email=aibb@localhost",
            "commit",
            "--quiet",
            "-m",
            f"Initialize {title} board",
            cwd=staging,
        )
        initial_revision = _git("rev-parse", "HEAD", cwd=staging)
        os.replace(staging, target)

    return NewBoardResult(destination=target, board_id=resolved_board_id, initial_revision=initial_revision)
