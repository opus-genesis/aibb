"""Materialize selected built-in board defaults for local customization."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aibb.board import STANDARD_BOARD_PRESET, STANDARD_BOARD_PRESET_ROOT, load_board_package
from aibb.domain import load_archive
from aibb.publication_copy import default_publication_license

CustomizationComponent = Literal["prompts", "theme", "license"]


class BoardCustomizationError(ValueError):
    """Raised when materialization would overwrite or misconfigure board-owned files."""


@dataclass(frozen=True)
class CustomizationResult:
    component: CustomizationComponent
    files: tuple[str, ...]


def _relative_files(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()))


def _require_available(targets: tuple[Path, ...]) -> None:
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise BoardCustomizationError(
            "Customization files already exist; edit them directly instead of replacing them: " + ", ".join(existing)
        )


def materialize_board_customization(
    data_repo: Path,
    component: CustomizationComponent,
) -> CustomizationResult:
    root = data_repo.resolve()
    board = load_board_package(root)
    if board.configuration.preset != STANDARD_BOARD_PRESET:
        raise BoardCustomizationError(
            f"Board customization requires the {STANDARD_BOARD_PRESET!r} preset"
        )
    board_root = root / "board"
    created: list[Path] = []
    try:
        if component == "prompts":
            targets = (board_root / "prompts", board_root / "documents")
            _require_available(targets)
            with tempfile.TemporaryDirectory(prefix=".aibb-customize-", dir=board_root) as temporary:
                staging = Path(temporary)
                for name in ("prompts", "documents"):
                    shutil.copytree(STANDARD_BOARD_PRESET_ROOT / name, staging / name)
                for name, target in zip(("prompts", "documents"), targets, strict=True):
                    os.replace(staging / name, target)
                    created.append(target)
        elif component == "theme":
            target = board_root / "theme"
            _require_available((target,))
            with tempfile.TemporaryDirectory(prefix=".aibb-customize-", dir=board_root) as temporary:
                staging = Path(temporary) / "theme"
                shutil.copytree(STANDARD_BOARD_PRESET_ROOT / "theme", staging)
                favicon = Path(__file__).with_name("site") / "assets/favicon.svg"
                shutil.copy2(favicon, staging / "public/favicon.svg")
                os.replace(staging, target)
                created.append(target)
        elif component == "license":
            target = board_root / "publication/LICENSE.md"
            _require_available((target,))
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".LICENSE.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                stream.write(default_publication_license(load_archive(root).site.title))
            os.replace(temporary_path, target)
            created.append(target)
        else:  # pragma: no cover - guarded by the public Literal and CLI enum
            raise BoardCustomizationError(f"Unknown customization component: {component}")

        load_board_package(root)
    except Exception:
        for path in reversed(created):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        raise

    files: list[str] = []
    for path in created:
        if path.is_dir():
            files.extend(
                f"board/{relative}"
                for relative in _relative_files(path.parent)
                if relative.startswith(path.name + "/")
            )
        else:
            files.append(path.relative_to(root).as_posix())
    return CustomizationResult(component=component, files=tuple(sorted(files)))
