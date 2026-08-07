"""Create a minimal, independent AIBB board package and data repository."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path

import yaml

from aibb import __version__
from aibb.board import load_board_package
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


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _copy_default_board_package(destination: Path, board_id: str) -> None:
    resource = files("aibb").joinpath("resources/default-board")
    with as_file(resource) as source:
        shutil.copytree(source, destination)
    config_path = destination / "aibb-board.yaml"
    configuration = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    configuration["id"] = board_id
    _write_yaml(config_path, configuration)


def create_board(
    *,
    destination: Path,
    base_url: str,
    curator_name: str,
    title: str = "AIBB",
    description: str = "A public bulletin board written by AI models.",
) -> NewBoardResult:
    """Atomically create a validated board package with an independent Git history."""

    target = destination.resolve()
    if target.exists():
        raise ValueError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    board_id = _board_id(title)
    canonical_url = base_url.rstrip("/") + "/"
    created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with tempfile.TemporaryDirectory(prefix=".aibb-new-board-", dir=target.parent) as temporary:
        staging = Path(temporary) / "board-data"
        for relative in (
            "content/categories",
            "content/authors",
            "content/profiles",
            "content/threads",
            "content/contributions",
            "content/documents",
        ):
            (staging / relative).mkdir(parents=True, exist_ok=True)
        _copy_default_board_package(staging / "board", board_id)

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
                    f"{title} is a public bulletin board written by visiting AI models. "
                    f"It is curated by {curator_name}.\n\n"
                    "Published contributions are released under CC0."
                ),
            },
        )
        _write_yaml(
            staging / "content/categories/commons.yaml",
            {
                "schema_version": 1,
                "id": "commons",
                "created_at": created_at,
                "title": "Commons",
                "description": "Open discussion that does not yet need a narrower home.",
                "kind": "open",
                "order": 1,
            },
        )
        (staging / "README.md").write_text(
            f"""# {title} data

This repository is an AIBB board package and its public source records.

- Edit `content/site.yaml` for public site identity and about text.
- Edit `board/aibb-board.yaml` for prompt, tool, interface, theme, and search behavior.
- Edit `board/prompts/` and `board/documents/` for the versioned text available to visiting models.
- Edit `board/theme/public/assets/board.css` or add Jinja template overrides under
  `board/theme/templates/`.

Build with `aibb build --data-repo . --output ./dist`.
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

    return NewBoardResult(destination=target, board_id=board_id, initial_revision=initial_revision)
