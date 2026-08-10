"""Materialize a versioned data baseline as a new independent Git repository."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aibb.domain import load_archive


@dataclass(frozen=True)
class StarterResult:
    destination: Path
    ref: str
    source_revision: str
    initial_revision: str


def _git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def initialize_data_repo(*, source: str, destination: Path, ref: str) -> StarterResult:
    target = destination.resolve()
    if target.exists():
        raise ValueError(f"destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".aibb-starter-", dir=target.parent) as temporary:
        temporary_root = Path(temporary)
        template = temporary_root / "template"
        staging = temporary_root / "new-data-repo"
        _git("clone", "--quiet", "--no-checkout", source, str(template))
        _git("checkout", "--quiet", "--detach", ref, cwd=template)
        source_revision = _git("rev-parse", "HEAD", cwd=template)

        shutil.copytree(template, staging, ignore=shutil.ignore_patterns(".git"), symlinks=True)
        load_archive(staging)
        _git("init", "--quiet", "--initial-branch=main", cwd=staging)
        _git("add", "--all", cwd=staging)
        commit_message = f"Initialize AIBB board from {ref}\n\nStarter-Revision: {source_revision}"
        _git(
            "-c",
            "user.name=AIBB Starter",
            "-c",
            "user.email=aibb@localhost",
            "commit",
            "--quiet",
            "-m",
            commit_message,
            cwd=staging,
        )
        initial_revision = _git("rev-parse", "HEAD", cwd=staging)
        os.replace(staging, target)

    return StarterResult(
        destination=target,
        ref=ref,
        source_revision=source_revision,
        initial_revision=initial_revision,
    )
