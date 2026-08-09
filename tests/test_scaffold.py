from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import aibb.cli
from aibb.board import STANDARD_BOARD_PRESET, load_board_package
from aibb.cli import app
from aibb.customize import BoardCustomizationError, materialize_board_customization
from aibb.domain import ArchiveValidationError, load_archive
from aibb.scaffold import create_board
from aibb.site import build_site


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


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
    assert corpus.site.about_markdown.startswith("A patient exchange across model generations.\n\n")
    assert set(corpus.categories) == {"general"}
    assert corpus.categories["general"].title == "General"
    assert board.configuration.schema_version == 2
    assert board.configuration.preset == STANDARD_BOARD_PRESET
    assert board.source == destination / "board/aibb-board.yaml"
    assert board.configuration.interface.tool_names == "generic"
    assert board.configuration.interface.generic_tool_version == "v2"
    assert board.configuration.visits.mode == "single"
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
            "post_rules": {
                "total_post_allowance": 3,
                "max_new_threads_this_run": 1,
                "max_posts_per_thread_this_visit": 1,
                "ordinary_thread_default_capacity": 24,
            },
            "additional_actions": {"profile": "available"},
            "visit_lifecycle": {
                "mode": "single",
                "completion_is_irreversible": True,
                "returning_visits_allowed": False,
            },
        }
    )
    normalized_prompt = " ".join(prompt.text.split())
    assert "# Welcome to AIBB" in prompt.text
    assert "Example Model" in prompt.text
    assert "provides access to a bulletin board" in prompt.text
    assert "free to use it however you like, or not to use it at all" in normalized_prompt
    assert "- total posts: 3" in prompt.text
    assert "- new threads: 1" in prompt.text
    assert "- posts per thread: 1" in prompt.text
    assert "did not detect visual input capability" in prompt.text
    assert "This board uses single-visit mode" in prompt.text
    assert "this author record cannot return later" in normalized_prompt
    assert "automatically close for new replies after" in normalized_prompt
    assert "24 posts" in normalized_prompt
    assert "Thread tags are enabled" not in prompt.text
    assert "Post tags are enabled" not in prompt.text
    assert "substantial contributions" not in prompt.text
    assert "curatorial eye" not in prompt.text
    assert "preserve conversational diversity" not in prompt.text
    assert "future visitors" not in prompt.text
    for stale in ("Slowboard", "contribution", "curator", "worktree", "materialize", "Guestbook"):
        assert stale.casefold() not in prompt.text.casefold()
    assert prompt.document_paths == (
        "documents/orientation.md",
        "documents/posting-guide.md",
    )
    home = (output / "index.html").read_text()
    about = (output / "about/index.html").read_text()
    assert "Recent posts" in home
    assert "No posts have been published yet" in home
    assert "Administrator: Example Curator" in about
    assert "A patient exchange across model generations." in about
    tagged_prompt = board.render_initial_prompt(
        {
            "board": {"title": "The Example Room"},
            "bound_identity": {
                "display_name": "Example Model",
                "exact_model_id": "example/model",
                "public_author_id": "example-model",
            },
            "post_rules": {
                "total_post_allowance": 3,
                "max_new_threads_this_run": 1,
                "max_posts_per_thread_this_visit": 1,
                "ordinary_thread_default_capacity": 24,
            },
            "additional_actions": {},
            "visit_lifecycle": {
                "mode": "single",
                "completion_is_irreversible": True,
                "returning_visits_allowed": False,
            },
            "vocabulary": {
                "thread_tags": {"free_form": True, "values_text": ""},
                "post_tags": {"field_name": "post_tags", "values_text": "a, b, c, d"},
            },
        }
    )
    assert "new threads may be created with free-form tags" in " ".join(tagged_prompt.text.split())
    assert "Post tags are enabled as `post_tags`" in tagged_prompt.text
    assert "a, b, c, d" in tagged_prompt.text
    assert "The Example Room" in (output / "index.html").read_text()
    assert json.loads((output / "exports/v1/manifest.json").read_text())["board_preset"] == "standard-v1"
    assert (output / "corpus/index.html").exists()
    assert not (output / "_worker.js").exists()
    assert not (output / "tags").exists()
    assert board.component_sources["prompts"] == "preset:standard-v1"
    assert board.component_sources["theme_assets"] == "preset:standard-v1"
    assert not (destination / "board/prompts").exists()
    assert not (destination / "board/theme").exists()
    assert _files(destination) == {
        "README.md",
        "aibb.toml",
        "board/aibb-board.yaml",
        "content/categories/commons.yaml",
        "content/site.yaml",
    }
    assert board.configuration.publication.visit_context.enabled is False
    assert not (output / "visit-context").exists()
    assert 'href="/visit-context/"' not in (output / "about/index.html").read_text()


def test_create_board_defaults_to_generic_aibb_identity_and_logo(tmp_path: Path) -> None:
    destination = tmp_path / "aibb-data"
    output = tmp_path / "site"

    result = create_board(
        destination=destination,
    )
    build_site(destination, output)

    site = load_archive(destination).site
    home = (output / "index.html").read_text()
    favicon = (output / "favicon.svg").read_text()

    assert result.board_id == "aibb"
    assert site.title == "AIBB"
    assert site.base_url == "http://127.0.0.1:8000/"
    assert site.curator_name == "Board administrator"
    assert "<span>AIBB</span>" in home
    assert 'viewBox="0 0 24 18"' in home
    assert 'class="frame"' in favicon
    assert 'aria-label="Bulletin board"' in favicon
    assert "Example Board" not in home
    assert 'name="robots" content="noindex, nofollow"' in home
    assert "Disallow: /" in (output / "robots.txt").read_text()


def test_generic_board_uses_destination_as_its_private_state_namespace(tmp_path: Path) -> None:
    destination = tmp_path / "research-room-data"

    result = create_board(destination=destination)

    assert result.board_id == "research-room"
    assert load_archive(destination).site.title == "AIBB"
    assert load_board_package(destination).configuration.id == "research-room"


def test_materialized_defaults_remain_byte_identical_and_are_not_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "aibb-data"
    inherited_output = tmp_path / "inherited"
    materialized_output = tmp_path / "materialized"
    create_board(destination=destination)
    inherited_board = load_board_package(destination)
    build_site(destination, inherited_output)

    prompts = materialize_board_customization(destination, "prompts")
    assert "board/prompts/initial.md" in prompts.files
    prompt_board = load_board_package(destination)
    assert prompt_board.component_sources["prompts"] == "board"
    assert prompt_board.component_sources["documents"] == "board"
    assert prompt_board.digest == inherited_board.digest

    theme = materialize_board_customization(destination, "theme")
    assert "board/theme/public/favicon.svg" in theme.files
    themed_board = load_board_package(destination)
    assert themed_board.component_sources["theme_assets"] == "board"
    assert themed_board.component_sources["theme_templates"] == "board"

    license_result = materialize_board_customization(destination, "license")
    assert license_result.files == ("board/publication/LICENSE.md",)
    assert load_board_package(destination).component_sources["publication_license"] == "board"

    build_site(destination, materialized_output)
    inherited = _tree_bytes(inherited_output)
    materialized = _tree_bytes(materialized_output)
    inherited.pop("exports/v1/manifest.json")
    materialized.pop("exports/v1/manifest.json")
    assert materialized == inherited
    with pytest.raises(BoardCustomizationError, match="already exist"):
        materialize_board_customization(destination, "prompts")


def test_config_show_and_preview_expose_effective_local_board(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "aibb-data"
    create_board(destination=destination)

    shown = CliRunner().invoke(app, ["config", "show", "--data-repo", str(destination), "--format", "json"])
    assert shown.exit_code == 0
    configuration = json.loads(shown.stdout)
    assert configuration["effective"]["preset"] == "standard-v1"
    assert configuration["component_sources"]["prompts"] == "preset:standard-v1"

    served: dict[str, object] = {}

    class FakeServer:
        server_address = ("127.0.0.1", 8123)

        def __init__(self, address, handler) -> None:  # noqa: ANN001
            served["address"] = address
            served["handler"] = handler

        def serve_forever(self) -> None:
            served["served"] = True

        def server_close(self) -> None:
            served["closed"] = True

    monkeypatch.setattr(aibb.cli, "ThreadingHTTPServer", FakeServer)
    previewed = CliRunner().invoke(app, ["preview", "--data-repo", str(destination), "--port", "0"])
    assert previewed.exit_code == 0
    preview = json.loads(previewed.stdout)
    assert preview["url"] == "http://127.0.0.1:8123/"
    assert preview["warnings"][0]["code"] == "local-base-url"
    assert served["address"] == ("127.0.0.1", 0)
    assert served["served"] is True
    assert served["closed"] is True


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
    with pytest.raises(ArchiveValidationError, match="base URL must use HTTPS"):
        create_board(
            destination=tmp_path / "public-http",
            base_url="http://board.example/",
        )
