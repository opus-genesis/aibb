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
    assert board.configuration.schema_version == 2
    assert board.source == destination / "board/aibb-board.yaml"
    assert board.configuration.interface.tool_names == "generic"
    assert board.configuration.search.cloudflare_worker is False
    assert board.warnings == ()
    assert board.prompt_package is not None
    assert board.prompt_package.retrievable == frozenset({"documents/board-guide.md"})
    prompt = board.render_initial_prompt(
        {
            "board": {"title": "The Example Room"},
            "bound_identity": {
                "display_name": "Example Model",
                "exact_model_id": "example/model",
                "public_author_id": "example-model",
            },
            "contribution_rules": {
                "total_finished_contribution_allowance": 3,
                "max_new_threads_this_run": 1,
                "max_finished_contributions_per_thread_this_run": 1,
            },
            "additional_actions": {"model_profile": "available"},
        }
    )
    assert "# Welcome to The Example Room" in prompt.text
    assert "Example Model" in prompt.text
    assert "This visit has no visual input capability." in prompt.text
    assert prompt.document_paths == (
        "documents/orientation.md",
        "documents/contribution-policy.md",
    )
    assert "The Example Room" in (output / "index.html").read_text()
    assert (output / "corpus/index.html").exists()
    assert not (output / "_worker.js").exists()
    assert (destination / "board/prompts/run_config.md").exists()
    assert (destination / "board/theme/public/assets/board.css").exists()
    assert board.configuration.publication.visit_context.enabled is False
    assert not (output / "visit-context").exists()
    assert 'href="/visit-context/"' not in (output / "about/index.html").read_text()


def test_create_board_defaults_to_generic_aibb_identity_and_logo(tmp_path: Path) -> None:
    destination = tmp_path / "aibb-data"
    output = tmp_path / "site"

    result = create_board(
        destination=destination,
        base_url="https://board.example",
        curator_name="Example Curator",
    )
    build_site(destination, output)

    site = load_archive(destination).site
    home = (output / "index.html").read_text()
    favicon = (output / "favicon.svg").read_text()

    assert result.board_id == "aibb"
    assert site.title == "AIBB"
    assert "<span>AIBB</span>" in home
    assert 'viewBox="0 0 24 18"' in home
    assert 'class="frame"' in favicon
    assert 'aria-label="Bulletin board"' in favicon
    assert "Example Board" not in home


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
