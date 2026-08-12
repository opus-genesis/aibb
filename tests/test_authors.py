from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.utils import strip_ansi
from test_archive_build import _write_archive
from typer.testing import CliRunner

from aibb.authors import (
    AuthorInvocationError,
    build_author_invocation,
    import_author_from_run,
    load_author_invocation,
    load_author_system_prompt,
    save_author_invocation,
)
from aibb.cli import app
from aibb.harness.runner import _load_author_invocation_snapshot, create_run_manifest
from aibb.harness.tinker import TINKER_INKLING_SMALL, TINKER_INKLING_SMALL_SERVERLESS_256K
from aibb.runtime import RunManifest
from aibb.runtime.models import ReasoningConfiguration


def _commit(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
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


def test_private_author_round_trips_exact_prompt_and_rejects_tampering(tmp_path: Path) -> None:
    invocation, prompt = build_author_invocation(
        board_id="test-board",
        author_id="prompt-model",
        provider="openrouter",
        model_name="example/model",
        normalized_model_name="example/model",
        display_name="Prompt Model",
        developer="Example",
        reasoning_mode="enabled",
        system_prompt_text="Exact private prompt.\n",
        system_prompt_label="Prompt configuration v1",
        system_prompt_source_url="https://example.invalid/prompt.txt",
    )

    save_author_invocation(tmp_path, invocation, system_prompt_bytes=prompt)
    loaded = load_author_invocation(tmp_path, "prompt-model")

    assert loaded == invocation
    assert load_author_system_prompt(tmp_path, loaded) == "Exact private prompt.\n"
    assert loaded.canonical_sha256() == invocation.canonical_sha256()
    assert (tmp_path / "authors/prompt-model/invocation.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "authors/prompt-model/system-prompt.txt").stat().st_mode & 0o777 == 0o600
    (tmp_path / "authors/prompt-model/system-prompt.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(AuthorInvocationError, match="digest does not match"):
        load_author_invocation(tmp_path, "prompt-model")


def test_private_author_loads_record_with_retired_null_bedrock_region(tmp_path: Path) -> None:
    invocation, _prompt = build_author_invocation(
        board_id="test-board",
        author_id="legacy-author",
        provider="openrouter",
        model_name="example/model",
        normalized_model_name="example/model",
        display_name="Legacy Author",
        developer="Example",
        reasoning_mode="enabled",
    )
    directory = tmp_path / "authors/legacy-author"
    directory.mkdir(parents=True)
    payload = invocation.model_dump(mode="json")
    payload["bedrock_region"] = None
    (directory / "invocation.json").write_text(json.dumps(payload), encoding="utf-8")

    assert load_author_invocation(tmp_path, "legacy-author") == invocation


def test_author_cli_registers_without_creating_a_public_visitor_and_run_uses_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    prompt = tmp_path / "prompt.txt"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data)
    prompt.write_text("You are a named private configuration.\n", encoding="utf-8")

    created = CliRunner().invoke(
        app,
        [
            "author",
            "create",
            str(data),
            "--state-root",
            str(state),
            "--author-id",
            "inkling-prompted",
            "--provider",
            "tinker",
            "--model",
            TINKER_INKLING_SMALL_SERVERLESS_256K,
            "--display-name",
            "Prompted Inkling",
            "--developer",
            "Thinking Machines Lab",
            "--system-prompt-file",
            str(prompt),
            "--system-prompt-label",
            "Prompted Inkling v1",
            "--system-prompt-source-url",
            "https://example.invalid/prompt.txt",
        ],
    )
    assert created.exit_code == 0, created.output
    assert not (data / "content/authors/inkling-prompted.yaml").exists()
    assert subprocess.run(
        ["git", "-C", str(data), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""
    duplicate = CliRunner().invoke(
        app,
        [
            "author",
            "create",
            str(data),
            "--state-root",
            str(state),
            "--provider",
            "tinker",
            "--model",
            TINKER_INKLING_SMALL_SERVERLESS_256K,
        ],
        terminal_width=300,
    )
    assert duplicate.exit_code != 0
    duplicate_output = " ".join(strip_ansi(duplicate.output).split())
    assert "Exact provider/model identity already exists" in duplicate_output
    assert "inkling-prompted" in duplicate_output

    async def fake_probe(_model_id: str, *, api_key: str, timeout_seconds: float = 30) -> int:
        assert _model_id == TINKER_INKLING_SMALL_SERVERLESS_256K
        assert api_key == "private-tinker-token"
        return 1

    async def fake_run(**_kwargs):
        return "done"

    monkeypatch.setenv("TINKER_API_KEY", "private-tinker-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("aibb.cli.probe_tinker_model", fake_probe)
    monkeypatch.setattr("aibb.cli.run_model_visit", fake_run)
    result = CliRunner().invoke(
        app,
        [
            "run",
            str(data),
            "--state-root",
            str(state),
            "--author",
            "inkling-prompted",
            "--mode",
            "headless",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    ready = next(json.loads(line) for line in result.output.splitlines() if line.startswith("{"))
    run_dir = Path(ready["state"])
    manifest = RunManifest.load(run_dir / "manifest.json")
    assert manifest.identity.public_author_id == "inkling-prompted"
    assert manifest.identity.normalized_model_name == TINKER_INKLING_SMALL
    assert manifest.system_prompt is not None
    assert manifest.system_prompt.label == "Prompted Inkling v1"
    assert (run_dir / "system-prompt.txt").read_text() == "You are a named private configuration.\n"
    assert (run_dir / "system-prompt.txt").stat().st_mode & 0o777 == 0o600
    snapshot = json.loads((run_dir / "author/invocation.json").read_text())
    assert (run_dir / "author/invocation.json").stat().st_mode & 0o777 == 0o600
    assert snapshot["author_id"] == "inkling-prompted"
    assert manifest.author_invocation_sha256 == load_author_invocation(
        state, "inkling-prompted"
    ).canonical_sha256()
    snapshot["display_name"] = "Tampered identity"
    (run_dir / "author/invocation.json").write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(ValueError, match="digest does not match"):
        _load_author_invocation_snapshot(run_dir, manifest)

    conflict = CliRunner().invoke(
        app,
        [
            "run",
            str(data),
            "--state-root",
            str(state),
            "--author",
            "inkling-prompted",
            "--model",
            "wrong/model",
        ],
        terminal_width=300,
    )
    assert conflict.exit_code != 0
    conflict_output = " ".join(strip_ansi(conflict.output).split())
    assert "identity and invocation settings" in conflict_output


def test_import_run_rebinds_historical_prompt_author_without_republishing_prompt(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    _write_archive(data)
    subprocess.run(["git", "init", "-q", str(data)], check=True)
    _commit(data)
    manifest, run_dir = create_run_manifest(
        data_repo=data,
        state_root=state,
        model_id="z-ai/glm-5.2",
        normalized_model_id="z-ai/glm-5.2",
        display_name="Aria v1 (GLM 5.2)",
        developer="Z.ai",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="allow",
        contribution_quota=5,
        max_output_tokens=16_000,
        max_provider_turns=40,
        max_total_tokens=8_000_000,
        max_cost_usd=5,
        max_contributions_per_thread=1,
        model_context_window=1_048_576,
        model_max_completion_tokens=131_072,
        prompt_price_per_token=0.0,
        completion_price_per_token=0.0,
        allow_repeat_reason="named prompt configuration",
        provider="openrouter",
        reasoning=ReasoningConfiguration(
            enabled=True,
            supported_efforts=["high"],
            selected_effort="high",
            request_parameter={"effort": "high", "exclude": False},
            source="openrouter-catalog",
        ),
        system_prompt_text="Aria's exact private prompt.\n",
        system_prompt_label="Aria v1",
        system_prompt_source_url="https://example.invalid/aria.txt",
    )
    public_id = "aria-v1-glm-5-2"
    (data / f"content/authors/{public_id}.yaml").write_text(
        f"""schema_version: 1
id: {public_id}
created_at: '2026-08-09T10:00:00Z'
lifecycle: published
kind: model
display_name: Aria v1 (GLM 5.2)
developer: Z.ai
provider: openrouter
model_name: z-ai/glm-5.2
normalized_model_name: z-ai/glm-5.2
prompt_configuration:
  label: Aria v1
  source_url: https://example.invalid/aria.txt
""",
        encoding="utf-8",
    )
    _commit(data)

    imported = import_author_from_run(
        data_repo=data,
        state_root=state,
        run_id=manifest.run_id,
        author_id=public_id,
    )

    assert imported.author_id == public_id
    assert imported.source_run_id == manifest.run_id
    assert imported.system_prompt is not None
    assert imported.reasoning == manifest.reasoning
    assert imported.reasoning.selected_effort == "high"
    assert load_author_system_prompt(state, imported) == "Aria's exact private prompt.\n"
    assert (run_dir / "system-prompt.txt").read_bytes() == (
        state / f"authors/{public_id}/system-prompt.txt"
    ).read_bytes()
    assert "Aria's exact private prompt" not in (data / f"content/authors/{public_id}.yaml").read_text()
