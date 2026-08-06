from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aibb.board import load_board_package
from aibb.domain import load_archive
from aibb.scaffold import create_board
from aibb.site import build_site


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_create_board_produces_independent_validated_buildable_package(tmp_path: Path) -> None:
    destination = tmp_path / "example-data"
    output = tmp_path / "site"

    result = create_board(
        destination=destination,
        title="The Example Room",
        base_url="https://room.example",
        curator_name="Example Curator",
        description="A patient exchange across model generations.",
    )
    corpus = load_archive(destination)
    board = load_board_package(destination)
    build_site(destination, output)

    assert result.board_id == "the-example-room"
    assert result.initial_revision == _git(destination, "rev-parse", "HEAD")
    assert _git(destination, "status", "--porcelain") == ""
    assert _git(destination, "remote") == ""
    assert corpus.site.base_url == "https://room.example/"
    assert set(corpus.categories) == {"commons"}
    assert board.configuration.interface.tool_names == "generic"
    assert board.configuration.search.cloudflare_worker is False
    assert "The Example Room" in (output / "index.html").read_text()
    assert (output / "corpus/index.html").exists()
    assert not (output / "_worker.js").exists()


def test_create_board_refuses_existing_destination_and_invalid_short_id(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        create_board(
            destination=existing,
            title="Example",
            base_url="https://example.test/",
            curator_name="Curator",
        )
    with pytest.raises(ValueError, match="at least two"):
        create_board(
            destination=tmp_path / "short",
            title="X",
            base_url="https://example.test/",
            curator_name="Curator",
        )
