from __future__ import annotations

from pathlib import Path

import pytest

from aibb.prompting import PromptPackage, PromptPackageError


def _package(root: Path) -> PromptPackage:
    (root / "prompts").mkdir(parents=True)
    (root / "documents/reference").mkdir(parents=True)
    (root / "prompts/initial.md").write_text(
        "Welcome {{ runvar.model.display_name }}.\n\n{{prompt:run_config}}\n\n{{doc:documents/rules.md}}\n",
        encoding="utf-8",
    )
    (root / "prompts/run_config.md").write_text(
        "{% if runvar.web.enabled %}Web budget: {{runvar:web.budget_usd}}.{% endif %}\n",
        encoding="utf-8",
    )
    (root / "documents/rules.md").write_text(
        "Rules remain opaque: {{ runvar.secret }} and {{prompt:not-an-include}}.\n",
        encoding="utf-8",
    )
    (root / "documents/reference/history.md").write_text("History.\n", encoding="utf-8")
    (root / "documents/orphan.md").write_text("Unused.\n", encoding="utf-8")
    (root / "prompts/orphan.md").write_text("Unused prompt.\n", encoding="utf-8")
    return PromptPackage(root, retrievable=["documents/reference/"])


def test_prompt_partials_conditions_runvars_and_opaque_documents(tmp_path: Path) -> None:
    package = _package(tmp_path)

    rendered = package.render(
        "initial",
        runvar={
            "model": {"display_name": "Example Model"},
            "web": {"enabled": True, "budget_usd": 10},
        },
    )

    assert "Welcome Example Model." in rendered.text
    assert "Web budget: 10." in rendered.text
    assert "{{ runvar.secret }}" in rendered.text
    assert "{{prompt:not-an-include}}" in rendered.text
    assert rendered.prompt_paths == ("prompts/initial.md", "prompts/run_config.md")
    assert rendered.document_paths == ("documents/rules.md",)


def test_prompt_false_branch_does_not_insert_document(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (tmp_path / "prompts/initial.md").write_text(
        "{% if runvar.show %}{{doc:documents/rules.md}}{% endif %}\n",
        encoding="utf-8",
    )
    package = PromptPackage(tmp_path, retrievable=["documents/reference/"])

    rendered = package.render("initial", runvar={"show": False})

    assert rendered.text == "\n"
    assert rendered.document_paths == ()


def test_unreachable_documents_and_prompt_partials_warn(tmp_path: Path) -> None:
    package = _package(tmp_path)

    warnings = {(warning.code, warning.path) for warning in package.warnings(["initial"])}

    assert warnings == {
        ("document-unreachable", "documents/orphan.md"),
        ("prompt-unreachable", "prompts/orphan.md"),
    }


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("{{prompt:missing}}", "Unknown or ambiguous prompt"),
        ("{{doc:documents/missing.md}}", "Unknown document"),
        ("{{runvar:missing}}", "Prompt rendering failed"),
        ("{{doc:../outside.md}}", "Invalid document path"),
    ],
)
def test_invalid_prompt_inputs_fail(tmp_path: Path, replacement: str, message: str) -> None:
    package = _package(tmp_path)
    (tmp_path / "prompts/initial.md").write_text(replacement + "\n", encoding="utf-8")
    package = PromptPackage(tmp_path, retrievable=["documents/reference/"])

    with pytest.raises(PromptPackageError, match=message):
        package.render("initial", runvar={})


def test_prompt_partial_cycle_fails(tmp_path: Path) -> None:
    package = _package(tmp_path)
    (tmp_path / "prompts/initial.md").write_text("{{prompt:run_config}}\n", encoding="utf-8")
    (tmp_path / "prompts/run_config.md").write_text("{{prompt:initial}}\n", encoding="utf-8")
    package = PromptPackage(tmp_path, retrievable=["documents/reference/"])

    with pytest.raises(PromptPackageError, match="Prompt partial cycle"):
        package.render("initial", runvar={})


def test_retrievable_selector_must_match(tmp_path: Path) -> None:
    _package(tmp_path)

    with pytest.raises(PromptPackageError, match="matches no documents"):
        PromptPackage(tmp_path, retrievable=["documents/missing/"])
