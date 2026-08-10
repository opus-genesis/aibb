from __future__ import annotations

import subprocess
from pathlib import Path

from test_archive_build import _write_archive

from aibb.domain import load_archive
from aibb.starter import initialize_data_repo


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_initialize_data_repo_creates_independent_validated_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "fresh-data"
    _write_archive(source)
    _git(source, "init", "--initial-branch=main")
    _git(source, "add", "--all")
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "starter",
        ],
        check=True,
        capture_output=True,
    )
    _git(source, "tag", "starter-v0.8")

    result = initialize_data_repo(source=str(source), destination=destination, ref="starter-v0.8")

    assert result.destination == destination
    assert result.source_revision == _git(source, "rev-parse", "starter-v0.8")
    assert load_archive(destination).contributions["first-record"].body == "A durable contribution."
    assert _git(destination, "remote") == ""
    assert "Starter-Revision:" in _git(destination, "log", "-1", "--format=%B")
    assert _git(destination, "status", "--porcelain") == ""
