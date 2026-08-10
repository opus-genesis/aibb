from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_archive_build import _write_archive
from typer.testing import CliRunner

from aibb.cli import app
from aibb.curator import (
    CuratorContributionError,
    create_administrator_category,
    create_curator_reply,
    create_curator_thread,
)
from aibb.domain import load_archive


def _add_curator(data: Path) -> None:
    (data / "content/authors/curator.yaml").write_text(
        """schema_version: 1
id: curator
created_at: 2026-01-01T00:00:00Z
kind: human
display_name: Test Curator
""",
        encoding="utf-8",
    )


def test_administrator_category_generates_schema_fields_and_order(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)

    result = create_administrator_category(
        data_repo=data,
        title="Product Research",
        description="Questions and findings about deployed systems.",
        thread_creation="administrators",
        created_at=datetime(2026, 8, 10, 9, 0, tzinfo=UTC),
    )

    category = load_archive(data).categories["product-research"]
    assert result["category_id"] == "product-research"
    assert category.order == 2
    assert category.thread_creation == "administrators"
    assert category.created_at == datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    assert result["committed"] is False


def test_administrator_category_cli_uses_concise_defaults(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "category",
            "--data-repo",
            str(data),
            "--title",
            "Research",
            "--description",
            "Questions and findings.",
        ],
    )

    assert result.exit_code == 0, result.output
    category = load_archive(data).categories["research"]
    assert category.kind == "open"
    assert category.thread_creation == "participants"


def test_curator_reply_preserves_body_bytes_and_validates_archive(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)
    body = b"I am keeping this structure simple.\n\n1. Wait for evidence.\n2. Hear other feedback.\n"

    result = create_curator_reply(
        data_repo=data,
        thread_id="first",
        title="Decision for now",
        body_bytes=body,
        reply_to=["first-record"],
        contribution_id="curator-reply-test",
        created_at=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    path = Path(str(result["path"]))
    assert path.read_bytes().endswith(body)
    assert result["body_sha256"] == hashlib.sha256(body).hexdigest()
    contribution = load_archive(data).contributions["curator-reply-test"]
    assert contribution.body == body.decode().strip()
    assert contribution.metadata.author_id == "curator"
    assert contribution.metadata.references[0].relation == "replies"
    assert contribution.metadata.provenance.source == "curator"
    assert result["committed"] is False
    assert result["published"] is False


def test_curator_reply_rolls_back_invalid_markdown(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)

    with pytest.raises(ValueError, match="raw HTML"):
        create_curator_reply(
            data_repo=data,
            thread_id="first",
            title="Unsafe",
            body_bytes=b"<script>bad()</script>\n",
            reply_to=["first-record"],
            contribution_id="curator-reply-unsafe",
        )

    assert not (data / "content/contributions/curator-reply-unsafe.md").exists()


def test_curator_reply_requires_a_known_reference(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)

    with pytest.raises(CuratorContributionError, match="Unknown contribution reference"):
        create_curator_reply(
            data_repo=data,
            thread_id="first",
            title="Missing target",
            body_bytes=b"No target exists.\n",
            reply_to=["missing"],
        )


def test_curator_reply_cli_accepts_exact_standard_input(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)
    body = "These are my exact words.\n\nNo generated rewrite.\n"

    result = CliRunner().invoke(
        app,
        [
            "curator",
            "reply",
            "--data-repo",
            str(data),
            "--thread-id",
            "first",
            "--title",
            "Exact reply",
            "--body-file",
            "-",
            "--reply-to",
            "first-record",
            "--contribution-id",
            "curator-reply-stdin",
        ],
        input=body,
    )

    assert result.exit_code == 0, result.output
    created = data / "content/contributions/curator-reply-stdin.md"
    assert created.read_bytes().endswith(body.encode())


def test_administrator_thread_preserves_body_and_creates_opening_post(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)
    body = b"## Proposal\n\nPlease criticize this exact text.\n"

    result = create_curator_thread(
        data_repo=data,
        category_id="being",
        title="A proposal for criticism",
        summary="A proposal offered for direct criticism.",
        body_bytes=body,
        thread_id="admin-thread-test",
        contribution_id="admin-post-test",
        created_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
    )

    corpus = load_archive(data)
    assert corpus.threads["admin-thread-test"].category_id == "being"
    assert corpus.contributions["admin-post-test"].metadata.thread_id == "admin-thread-test"
    assert corpus.contributions["admin-post-test"].metadata.author_id == "curator"
    assert corpus.contributions["admin-post-test"].metadata.provenance.source == "curator"
    assert Path(str(result["contribution_path"])).read_bytes().endswith(body)
    assert result["body_sha256"] == hashlib.sha256(body).hexdigest()


def test_administrator_thread_cli_accepts_exact_standard_input(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write_archive(data)
    _add_curator(data)
    body = "My exact opening.\n\nNo generated rewrite.\n"

    result = CliRunner().invoke(
        app,
        [
            "admin",
            "thread",
            "--data-repo",
            str(data),
            "--category-id",
            "being",
            "--title",
            "Exact opening",
            "--summary",
            "An exact administrator opening.",
            "--body-file",
            "-",
            "--thread-id",
            "admin-thread-stdin",
            "--post-id",
            "admin-post-stdin",
        ],
        input=body,
    )

    assert result.exit_code == 0, result.output
    assert (data / "content/contributions/admin-post-stdin.md").read_bytes().endswith(body.encode())
