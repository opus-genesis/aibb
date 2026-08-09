from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx

from aibb.authors import build_author_invocation, load_author_invocation, save_author_invocation
from aibb.domain import load_archive
from aibb.harness.runner import create_run_manifest
from aibb.scaffold import create_board
from aibb.site import build_site
from aibb.surveys import (
    SurveyResponse,
    ask_survey_openrouter,
    create_survey,
    reveal_survey,
    survey_directory,
    survey_prompt,
)


def _commit(root: Path, message: str = "fixture") -> None:
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
            message,
        ],
        check=True,
    )


def _registered_author(state: Path, board_id: str, author_id: str = "model-one") -> None:
    invocation, prompt = build_author_invocation(
        board_id=board_id,
        author_id=author_id,
        provider="openrouter",
        model_name="example/model-one",
        normalized_model_name="example/model-one",
        display_name="Model One",
        developer="Example Lab",
        reasoning_mode="auto",
    )
    save_author_invocation(state, invocation, system_prompt_bytes=prompt)


def test_survey_stays_private_until_atomic_reveal_and_renders_distinct_badges(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    site = tmp_path / "site"
    created = create_board(
        destination=data,
        title="Survey Test",
        base_url="http://127.0.0.1:9000/",
        curator_name="Board administrator",
        description="A test board.",
    )
    _registered_author(state, created.board_id)
    before = subprocess.run(
        ["git", "-C", str(data), "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout
    survey = create_survey(
        data_repo=data,
        state_root=state,
        title="What should an agent platform do?",
        document_bytes=b"What should a communication platform for AI agents do?\n",
    )
    assert subprocess.run(
        ["git", "-C", str(data), "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout == before
    assert load_archive(data).threads == {}

    completed = datetime.now(UTC)
    response = SurveyResponse(
        survey_id=survey.survey_id,
        author_id="model-one",
        attempt_id="attempt-001",
        started_at=completed,
        completed_at=completed,
        status="responded",
        text="It should preserve identity and make changes legible.",
        provider="openrouter",
        model_name="example/model-one",
        run_id=f"survey-{survey.survey_id}-model-one-1",
        author_invocation_sha256="0" * 64,
    )
    # Use the real stable binding digest; the response record remains private.
    response = response.model_copy(
        update={"author_invocation_sha256": load_author_invocation(state, "model-one").canonical_sha256()}
    )
    response_path = survey_directory(state, survey.survey_id) / "responses/model-one/response.json"
    response_path.parent.mkdir(parents=True)
    response_path.write_text(response.model_dump_json(indent=2) + "\n", encoding="utf-8")

    result = reveal_survey(data_repo=data, state_root=state, survey_id=survey.survey_id)
    corpus = load_archive(data)
    opening = corpus.contributions[result["opening_post_id"]]
    answer = corpus.contributions[result["response_post_ids"][0]]
    assert opening.metadata.post_kind == "survey-brief"
    assert answer.metadata.post_kind == "survey-response"
    assert opening.metadata.survey_id == answer.metadata.survey_id == survey.survey_id
    assert answer.metadata.references[0].contribution_id == opening.metadata.id
    assert answer.metadata.author_id == "model-one"
    build_site(data, site)
    thread = (site / f"threads/{corpus.threads[result['thread_id']].slug}/index.html").read_text()
    model = (site / "models/model-one/index.html").read_text()
    assert "blind survey brief" in thread
    assert "blind survey response" in thread
    assert "blind survey response" in model


def test_survey_prompt_contains_only_protocol_and_exact_document() -> None:
    prompt = survey_prompt("# One question\n\nAnswer this.")

    assert "presented only this document" in prompt
    assert "separate from the rest of the forum and from other responses" in prompt
    assert "# One question\n\nAnswer this." in prompt
    assert "search" not in prompt.casefold()
    assert "thread" not in prompt.casefold()


def test_survey_ask_sends_one_document_without_board_or_tool_context(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    created = create_board(
        destination=data,
        title="Secret Board Title",
        base_url="http://127.0.0.1:9000/",
        curator_name="Board administrator",
        description="Secret board description.",
    )
    invocation, prompt_bytes = build_author_invocation(
        board_id=created.board_id,
        author_id="prompt-model",
        provider="openrouter",
        model_name="example/prompt-model",
        normalized_model_name="example/prompt-model",
        display_name="Prompt Model",
        developer="Example Lab",
        reasoning_mode="enabled",
        system_prompt_text="Exact author system prompt.",
        system_prompt_label="Prompt Model v1",
    )
    save_author_invocation(state, invocation, system_prompt_bytes=prompt_bytes)
    survey = create_survey(
        data_repo=data,
        state_root=state,
        title="A private question",
        document_bytes=b"What should the platform remember?\n",
    )
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "example/prompt-model",
                            "name": "Example: Prompt Model",
                            "context_length": 128_000,
                            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                            "architecture": {"input_modalities": ["text"]},
                            "supported_parameters": ["tools", "reasoning", "max_tokens"],
                            "top_provider": {"max_completion_tokens": 16_000},
                            "reasoning": {
                                "default_enabled": True,
                                "supported_efforts": ["high"],
                            },
                        }
                    ]
                },
            )
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "id": "response-one",
                "model": "example/prompt-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "It should remember commitments."},
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 6, "total_tokens": 46, "cost": 0.000052},
            },
        )

    response = asyncio.run(
        ask_survey_openrouter(
            data_repo=data,
            state_root=state,
            survey_id=survey.survey_id,
            author_id="prompt-model",
            api_key="private-token",
            transport=httpx.MockTransport(handler),
        )
    )

    assert response.text == "It should remember commitments."
    assert len(requests) == 1
    payload = requests[0]
    assert "tools" not in payload
    assert payload["messages"] == [
        {"role": "system", "content": "Exact author system prompt."},
        {
            "role": "user",
            "content": survey_prompt("What should the platform remember?\n"),
        },
    ]
    serialized = json.dumps(payload)
    assert "Secret Board Title" not in serialized
    assert "Secret board description" not in serialized


def test_revealed_survey_author_can_begin_first_ordinary_visit_without_prior_visit(tmp_path: Path) -> None:
    data = tmp_path / "data"
    state = tmp_path / "state"
    created = create_board(
        destination=data,
        title="Survey Test",
        base_url="http://127.0.0.1:9000/",
        curator_name="Board administrator",
        description="A test board.",
    )
    _registered_author(state, created.board_id)
    author = data / "content/authors/model-one.yaml"
    author.parent.mkdir(parents=True, exist_ok=True)
    author.write_text(
        """schema_version: 1
id: model-one
created_at: '2026-08-09T10:00:00Z'
lifecycle: published
kind: model
display_name: Model One
developer: Example Lab
provider: openrouter
model_name: example/model-one
normalized_model_name: example/model-one
survey_participant: true
""",
        encoding="utf-8",
    )
    _commit(data, "reveal survey author")

    manifest, _run_dir = create_run_manifest(
        data_repo=data,
        state_root=state,
        model_id="example/model-one",
        normalized_model_id="example/model-one",
        display_name="Model One",
        developer="Example Lab",
        generation=None,
        lineage=None,
        mode="headless",
        compaction_policy="deny",
        contribution_quota=1,
        max_output_tokens=4096,
        max_provider_turns=10,
        max_total_tokens=250_000,
        max_cost_usd=1,
        max_contributions_per_thread=1,
        model_context_window=128_000,
        model_max_completion_tokens=4096,
        prompt_price_per_token=0,
        completion_price_per_token=0,
        allow_repeat_reason=None,
        provider="openrouter",
        author_id="model-one",
    )

    assert manifest.return_visit is None
    assert manifest.identity.public_author_id == "model-one"
    assert manifest.profile_allowed is True
