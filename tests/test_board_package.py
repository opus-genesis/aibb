from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from test_archive_build import _write_archive
from test_budget import make_manifest

from aibb.board import (
    BoardConfigurationError,
    PostTagsConfiguration,
    ThreadTagsConfiguration,
    load_board_package,
    load_run_board_package,
    resolve_board_state_root,
)
from aibb.harness.runner import create_run_manifest
from aibb.protocol.server import _project_generic_v2_result, _tools
from aibb.protocol.state import ArchiveMcpState
from aibb.site import build_site


def _write_board_package(root: Path) -> Path:
    framing = root / "framing"
    templates = root / "theme/templates"
    assets = root / "theme/output/assets"
    framing.mkdir(exist_ok=True)
    templates.mkdir(parents=True)
    assets.mkdir(parents=True)
    (framing / "orientation.md").write_text("# Example orientation\n\nRead with care.\n")
    (framing / "notice.md").write_text("# Example notice\n\nThis visit is public.\n")
    (framing / "policy.md").write_text("# Example policy\n\nAdd signal.\n")
    (templates / "home.html").write_text(
        "{% extends 'base.html' %}{% block content %}<h1>Custom Example Board home</h1>{% endblock %}\n"
    )
    (templates / "_wordmark_glyph.html").write_text(
        '<svg class="wordmark-glyph custom-mark" aria-hidden="true"></svg>\n'
    )
    (assets / "custom.css").write_text(".wordmark { color: rebeccapurple; }\n")
    (root / "theme/output/favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0h1v1H0z"/></svg>\n'
    )
    config = root / "aibb-board.yaml"
    config.write_text(
        """schema_version: 1
id: example-board
framing:
  orientation:
    version: v1
    path: framing/orientation.md
    title: Orientation
    description: The opening invitation.
  notice:
    version: v1
    path: framing/notice.md
    title: Operational notice
    description: The visit boundaries.
  policy:
    version: v1
    path: framing/policy.md
    title: Contribution policy
    description: The contribution rules.
interface:
  tool_names: generic
  headless_continuation_version: v1
  headless_continuation_message: No board tool call was received. The visit remains open.
  conclusion_confirmation_message: Call conclude_visit again to end this one-time visit.
theme:
  templates: theme/templates
  assets: theme/output
  stylesheets:
    - /assets/style.css
    - /assets/custom.css
search:
  cloudflare_worker: false
  static_fallback: true
  static_page_size: 10
ui:
  nav_models: Visitors
  home_boards: Rooms
"""
    )
    return config


def _write_v2_board_package(root: Path) -> Path:
    (root / "prompts").mkdir()
    (root / "documents").mkdir()
    (root / "prompts/initial.md").write_text(
        "Welcome {{runvar:bound_identity.display_name}}.\n\n{{prompt:run_config}}\n\n{{doc:documents/rules.md}}\n"
    )
    (root / "prompts/run_config.md").write_text(
        "{% if runvar.contribution_rules.total_finished_contribution_allowance %}"
        "Allowance: {{runvar:contribution_rules.total_finished_contribution_allowance}}."
        "{% endif %}\n"
    )
    (root / "documents/rules.md").write_text("Add signal.\n")
    (root / "documents/reference.md").write_text("Reference material.\n")
    (root / "documents/orphan.md").write_text("Unused.\n")
    (root / "publication").mkdir()
    (root / "publication/LICENSE.md").write_text("# Example publication license\n")
    (root / "publication/visit-context-example.json").write_text(
        json.dumps(
            {
                "bound_identity": {"display_name": "[model name]"},
                "contribution_rules": {"total_finished_contribution_allowance": "[allowance]"},
            }
        )
        + "\n"
    )
    config = root / "aibb-board.yaml"
    config.write_text(
        """schema_version: 2
id: example-board
documents:
  path: documents
  retrievable:
    - documents/reference.md
prompts:
  path: prompts
  initial: initial
tools:
  preset: standard
  hide:
    - web.browse
    - threads.create
    - images.generate
interface:
  tool_names: generic
theme:
  stylesheets:
    - /assets/style.css
search:
  cloudflare_worker: false
  static_fallback: true
publication:
  license_markdown: publication/LICENSE.md
  visit_context:
    enabled: true
    example_runvar: publication/visit-context-example.json
    aliases:
      rules-v1.md: documents/rules.md
"""
    )
    return config


def test_configured_board_controls_build_theme_framing_and_search_fallback(tmp_path: Path) -> None:
    data = tmp_path / "data"
    output = tmp_path / "site"
    _write_archive(data)
    _write_board_package(data)

    result = build_site(data, output)
    board = load_board_package(data)

    assert result.contributions == 1
    assert board.configuration.id == "example-board"
    assert len(board.digest) == 64
    home = (output / "index.html").read_text()
    assert "Custom Example Board home" in home
    assert 'class="wordmark-glyph custom-mark"' in home
    assert 'href="/assets/custom.css"' in home
    assert (output / "assets/custom.css").read_text() == ".wordmark { color: rebeccapurple; }\n"
    assert 'viewBox="0 0 1 1"' in (output / "favicon.svg").read_text()
    assert "First record" in (output / "corpus/index.html").read_text()
    assert 'href="/corpus/"' in (output / "search/index.html").read_text()
    search = (output / "search/index.html").read_text()
    assert "This static host cannot execute an interactive search without JavaScript." in search
    assert "This form returns complete HTML results without JavaScript." not in search
    assert not (output / "_worker.js").exists()
    assert not (output / "_routes.json").exists()
    assert not (output / "visit-context").exists()
    assert 'href="/visit-context/"' not in (output / "about/index.html").read_text()
    assert "How visits are framed" not in (output / "llms.txt").read_text()
    assert "released under CC0-1.0 for indexing" in (output / "data/index.html").read_text()
    assert "public archive of posts made by AI model instances" in (output / "llms.txt").read_text()
    publication_license = (output / "LICENSE.md").read_text()
    assert "produced by AIBB" in publication_license
    assert "xlr8harder/slowboard" not in publication_license


def test_generic_tool_projection_uses_board_vocabulary() -> None:
    tools = _tools(read_only=False, archive_title="Example Board", generic_names=True)
    names = {tool.name for tool in tools}
    rendered = json.dumps([tool.model_dump(mode="json") for tool in tools])
    new_thread = next(tool for tool in tools if tool.name == "start_new_thread_draft")
    reply = next(tool for tool in tools if tool.name == "start_reply_draft")

    assert "get_board_status" in names
    assert "search_contributions" in names
    assert "report_board_issue" in names
    assert "get_slowboard_status" not in names
    assert "Slowboard" not in rendered
    assert "slowboard" not in rendered.casefold()
    assert "Example Board" in rendered
    assert "thread_tags" not in new_thread.inputSchema["properties"]
    assert "tags" not in new_thread.inputSchema["properties"]
    assert "post_tags" not in reply.inputSchema["properties"]
    assert "epistemic_modes" not in reply.inputSchema["properties"]


def test_generic_v2_tool_projection_uses_ordinary_board_language() -> None:
    tools = _tools(
        read_only=False,
        capabilities={"ask", "search", "browse", "verify"},
        archive_title="Example Board",
        generic_names=True,
        generic_tool_version="v2",
    )
    names = {tool.name for tool in tools}
    rendered = json.dumps([tool.model_dump(mode="json") for tool in tools], ensure_ascii=False).casefold()

    assert {
        "search_posts",
        "read_post",
        "research_web",
        "search_web",
        "browse_web_source",
        "fetch_url",
        "save_post",
        "draft_profile",
        "preview_profile",
        "save_profile",
    } <= names
    for stale in (
        "slowboard",
        "contribution",
        "curator",
        "worktree",
        "materialize",
        "external review",
        "generous",
        "current_web",
        "public_web",
    ):
        assert stale not in rendered
    assert "save one validated draft as a completed post" in rendered
    assert "requires no further action from you" in rendered


def test_generic_v2_result_projection_hides_repository_mechanics_without_rewriting_posts() -> None:
    projected = _project_generic_v2_result(
        {
            "contribution_id": "post-1234",
            "body": "A contribution about Slowboard must remain byte-for-byte intact.",
            "publication_state": "local_worktree",
            "remaining_run_contributions": 2,
            "paths": {"content/contributions/post-1234.md": "digest"},
            "local_worktree": True,
            "consumes_contribution_quota": True,
        }
    )

    assert projected == {
        "post_id": "post-1234",
        "body": "A contribution about Slowboard must remain byte-for-byte intact.",
        "publication_state": "saved",
        "remaining_posts": 2,
        "uses_post_allowance": True,
    }


def test_generic_v2_result_projection_preserves_stable_ids_and_updates_retrieval_hints() -> None:
    projected = _project_generic_v2_result(
        {
            "record_id": "contribution-4a2e19382e39bd62",
            "contribution_id": "contribution-4a2e19382e39bd62",
            "retrieve_with": "read_contribution",
            "record_type": "contributions",
            "retrieve_one_contribution_with": "read_contribution(contribution_id)",
            "thread_title": "A contribution to the archive",
            "excerpt": "Slowboard contribution wording in public content.",
        }
    )

    assert projected == {
        "record_id": "contribution-4a2e19382e39bd62",
        "post_id": "contribution-4a2e19382e39bd62",
        "retrieve_with": "read_post",
        "record_type": "posts",
        "retrieve_one_post_with": "read_post(post_id)",
        "thread_title": "A contribution to the archive",
        "excerpt": "Slowboard contribution wording in public content.",
    }


def test_returning_visit_policy_rejects_ambiguous_mode_alias(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    config = _write_v2_board_package(data)
    config.write_text(config.read_text().replace("tools:\n", "visits:\n  mode: multiple\ntools:\n"))

    with pytest.raises(BoardConfigurationError, match="Extra inputs are not permitted"):
        load_board_package(data)


def test_board_can_name_and_bound_its_post_tag_vocabulary() -> None:
    slowboard_modes = PostTagsConfiguration(
        enabled=True,
        field_name="epistemic_modes",
        label="Mode",
        values=["witnessed", "felt", "analysis"],
    )
    tools = _tools(
        read_only=False,
        post_tags=slowboard_modes,
        thread_tags=ThreadTagsConfiguration(enabled=True),
    )
    reply = next(tool for tool in tools if tool.name == "start_reply_draft")
    new_thread = next(tool for tool in tools if tool.name == "start_new_thread_draft")
    properties = reply.inputSchema["properties"]

    assert "post_tags" not in properties
    assert properties["epistemic_modes"]["items"]["enum"] == ["witnessed", "felt", "analysis"]
    assert "thread_tags" in new_thread.inputSchema["properties"]

    with pytest.raises(ValueError, match="require at least one"):
        PostTagsConfiguration(enabled=True, values=[])


def test_run_snapshot_preserves_model_visible_board_contract(tmp_path: Path) -> None:
    data = tmp_path / "data"
    run_dir = tmp_path / "state/run-one"
    _write_archive(data)
    _write_board_package(data)
    board = load_board_package(data)
    board.snapshot(run_dir)

    snapshot_path = run_dir / "board/package.json"
    historical = json.loads(snapshot_path.read_text())
    historical["configuration"].pop("preset")
    historical["configuration"].pop("visits")
    snapshot_path.write_text(json.dumps(historical))

    (data / "framing/orientation.md").write_text("# Changed later\n")
    restored = load_run_board_package(run_dir, data)

    assert restored.digest == board.digest
    assert restored.framing_document("orientation") == "# Example orientation\n\nRead with care.\n"

    payload = json.loads(snapshot_path.read_text())
    payload["framing_documents"]["notice"] = "tampered"
    snapshot_path.write_text(json.dumps(payload))
    with pytest.raises(BoardConfigurationError, match="digest does not match"):
        load_run_board_package(run_dir, data)


def test_private_state_defaults_to_aibb_home_and_stable_board_id(tmp_path: Path) -> None:
    data = tmp_path / "renamed-checkout"
    _write_archive(data)
    _write_board_package(data)
    board = load_board_package(data)

    resolved = resolve_board_state_root(
        data,
        board,
        environment={"AIBB_HOME": str(tmp_path / "operator-home")},
    )

    assert resolved == tmp_path / "operator-home/state/example-board"


def test_runtime_state_override_does_not_change_board_contract_digest(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    config = _write_board_package(data)
    baseline = load_board_package(data).digest
    payload = config.read_text().replace(
        "theme:\n",
        f"runtime:\n  state_root: {tmp_path / 'private-state'}\ntheme:\n",
    )
    config.write_text(payload)
    board = load_board_package(data)

    assert board.digest == baseline
    assert resolve_board_state_root(data, board) == tmp_path / "private-state"

    run_dir = tmp_path / "run"
    snapshot_path = board.snapshot(run_dir)
    snapshot = json.loads(snapshot_path.read_text())
    assert snapshot["configuration"]["runtime"] == {"state_root": None}


def test_private_state_is_rejected_inside_public_board_repo(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    config = _write_board_package(data)
    config.write_text(config.read_text().replace("theme:\n", "runtime:\n  state_root: private\ntheme:\n"))
    board = load_board_package(data)

    with pytest.raises(BoardConfigurationError, match="must live outside"):
        resolve_board_state_root(data, board)


def test_v2_board_renders_prompt_warns_and_snapshots_sources(tmp_path: Path) -> None:
    data = tmp_path / "data"
    run_dir = tmp_path / "state/run-two"
    _write_archive(data)
    _write_v2_board_package(data)
    board = load_board_package(data)

    rendered = board.render_initial_prompt(
        {
            "bound_identity": {"display_name": "Example Model"},
            "contribution_rules": {"total_finished_contribution_allowance": 3},
        }
    )

    assert rendered.text == "Welcome Example Model.\n\nAllowance: 3.\n\n\nAdd signal.\n\n"
    assert {(warning.code, warning.path) for warning in board.warnings} == {
        ("document-unreachable", "documents/orphan.md")
    }
    output = tmp_path / "site"
    build_site(data, output)
    assert (output / "visit-context/rules-v1.md").read_text() == "Add signal.\n"
    visit_context = (output / "visit-context/index.html").read_text()
    assert "this is the complete text Test Accumulation adds to an ordinary visit" in visit_context
    assert "Welcome [model name]." in visit_context
    assert "Allowance: [allowance]." in visit_context
    assert "{{runvar" not in visit_context
    assert not (output / "visit-context/prompts/initial.md").exists()
    visit_manifest = json.loads((output / "visit-context/index.json").read_text())
    assert "aibb_system_prompt" not in visit_manifest
    assert visit_manifest["opening_user_message"].startswith("Welcome [model name].")
    assert visit_manifest["rendered_sha256"] == hashlib.sha256(
        visit_manifest["opening_user_message"].encode()
    ).hexdigest()
    headers = (output / "_headers").read_text()
    assert "/visit-context/*.md\n  ! X-Robots-Tag\n  X-Robots-Tag: noindex, follow" in headers
    board.snapshot(run_dir)
    snapshot_path = run_dir / "board/package.json"
    historical = json.loads(snapshot_path.read_text())
    historical["configuration"].pop("preset")
    historical["configuration"].pop("visits")
    snapshot_path.write_text(json.dumps(historical))
    (data / "prompts/initial.md").write_text("Changed later.\n")
    (data / "documents/rules.md").write_text("Changed later.\n")

    restored = load_run_board_package(run_dir, data)
    restored_rendered = restored.render_initial_prompt(
        {
            "bound_identity": {"display_name": "Example Model"},
            "contribution_rules": {"total_finished_contribution_allowance": 3},
        }
    )

    assert restored.digest == board.digest
    assert restored_rendered == rendered
    assert restored.publication_license_markdown == "# Example publication license\n"
    assert restored.visit_context_example_runvar == board.visit_context_example_runvar


def test_enabled_visit_context_requires_a_renderable_public_example(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _write_archive(missing)
    config = _write_v2_board_package(missing)
    config.write_text(
        config.read_text().replace("    example_runvar: publication/visit-context-example.json\n", "")
    )
    with pytest.raises(BoardConfigurationError, match="requires enabled and example_runvar together"):
        load_board_package(missing)

    invalid = tmp_path / "invalid"
    _write_archive(invalid)
    _write_v2_board_package(invalid)
    (invalid / "publication/visit-context-example.json").write_text("{}\n")
    with pytest.raises(BoardConfigurationError, match="example cannot render the opening prompt"):
        load_board_package(invalid)


def test_v2_board_controls_tools_and_retrievable_documents(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _write_v2_board_package(data)
    board = load_board_package(data)
    state = ArchiveMcpState(data, tmp_path / "state", make_manifest(), read_only=True, board=board)

    tools = _tools(
        read_only=False,
        capabilities={"ask", "browse", "generate_image"},
        allowed_capabilities=board.allowed_tool_capabilities,
        document_access=True,
        archive_title="Example Board",
        generic_names=True,
    )
    names = {tool.name for tool in tools}
    assert {"list_documents", "search_documents", "read_document", "research_current_web"} <= names
    assert "browse_current_events_source" not in names
    assert "start_new_thread_draft" not in names
    assert "generate_image" not in names
    assert "start_reply_draft" in names
    reply = next(tool for tool in tools if tool.name == "start_reply_draft")
    assert "attachments" not in reply.inputSchema["properties"]

    listing = state.list_documents()
    assert listing["page"] == {"offset": 0, "returned": 1, "total": 1, "next_offset": None}
    assert listing["documents"][0]["path"] == "documents/reference.md"
    search = state.search_documents("material")
    assert search["hits"][0]["path"] == "documents/reference.md"
    document = state.read_document("documents/reference.md")
    assert document["content"] == "Reference material.\n"
    assert document["page"]["complete_document"] is True


def test_new_run_binds_configured_board_and_snapshots_it(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _write_board_package(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    subprocess.run(["git", "-C", str(data), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(data),
            "-c",
            "user.name=AIBB tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    manifest, run_dir = create_run_manifest(
        data_repo=data,
        state_root=tmp_path / "state",
        model_id="example/model-v1",
        display_name="Example Model",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="deny",
        contribution_quota=3,
        max_output_tokens=4_096,
        max_provider_turns=20,
        max_total_tokens=1_000_000,
        max_cost_usd=5,
        max_contributions_per_thread=1,
        model_context_window=128_000,
        model_max_completion_tokens=4_096,
        prompt_price_per_token=0,
        completion_price_per_token=0,
        allow_repeat_reason=None,
    )

    assert manifest.board_id == "example-board"
    assert manifest.board_package_sha256 == load_board_package(data).digest
    assert manifest.orientation_version == "v1"
    assert manifest.headless_continuation_message == "No board tool call was received. The visit remains open."
    board = load_run_board_package(run_dir, data)
    assert board.digest == manifest.board_package_sha256
    state = ArchiveMcpState(data, run_dir / "mcp", manifest, read_only=True, board=board)
    assert state.list_threads()["retrieve_full_thread_with"] == "read_thread(thread_id)"
    assert state.read_thread("first")["retrieve_one_contribution_with"] == ("read_contribution(contribution_id)")


def test_board_package_rejects_paths_outside_package_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    config = _write_board_package(data)
    config.write_text(config.read_text().replace("framing/orientation.md", "../outside.md"))
    (tmp_path / "outside.md").write_text("outside")

    with pytest.raises(BoardConfigurationError, match="escapes the package root"):
        load_board_package(data)


def test_board_package_rejects_unknown_ui_copy_key(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    config = _write_board_package(data)
    config.write_text(config.read_text() + "  typo_heading: This would otherwise be ignored.\n")

    with pytest.raises(BoardConfigurationError, match="unknown UI string key"):
        load_board_package(data)


def test_board_package_is_required_instead_of_assuming_slowboard(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()

    with pytest.raises(BoardConfigurationError, match="Missing board configuration"):
        load_board_package(data)
