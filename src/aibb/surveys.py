"""Minimal private blind surveys that can later be revealed into a board thread."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml
from harn_ai.types import TextContent, UserMessage
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aibb.authors import load_author_invocation, load_author_system_prompt, save_author_invocation
from aibb.board import load_board_package
from aibb.domain import load_archive
from aibb.domain.models import AuthorRecord, ContributionMetadata, ThreadRecord
from aibb.harness.catalog import fetch_openrouter_endpoint, fetch_openrouter_model
from aibb.harness.engine import AibbHarnessEngine
from aibb.harness.openrouter import OPENROUTER_ENDPOINT, OpenRouterAdapter, openrouter_model
from aibb.markdown import validate_contribution_markdown
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.models import (
    BoundModelIdentity,
    BudgetLimits,
    OpenRouterRoutingConfiguration,
    SystemPromptConfiguration,
)
from aibb.sessions import SessionStore

SURVEY_DIRECTORY = "surveys"
SURVEY_PROMPT_VERSION = "survey-v1"


class SurveyError(ValueError):
    """Raised before a blind-survey invariant can be violated."""


class SurveyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    survey_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    board_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    title: str = Field(min_length=1, max_length=240)
    created_at: datetime
    document_artifact: Literal["document.md"] = "document.md"
    document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["open", "revealed"] = "open"
    revealed_at: datetime | None = None
    thread_id: str | None = None

    @field_validator("created_at", "revealed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("survey timestamps must include a timezone")
        return value


class SurveyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    survey_id: str
    author_id: str
    attempt_id: str
    started_at: datetime
    completed_at: datetime
    status: Literal["responded", "declined"]
    text: str
    provider: str
    model_name: str
    response_model: str | None = None
    run_id: str
    author_invocation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def _slug(value: str, limit: int = 79) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "survey"
    return result[:limit].rstrip("-")


def _atomic_json(path: Path, value: BaseModel | dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)


def survey_directory(state_root: Path, survey_id: str) -> Path:
    return state_root / SURVEY_DIRECTORY / survey_id


def load_survey(state_root: Path, survey_id: str) -> SurveyRecord:
    path = survey_directory(state_root, survey_id) / "survey.json"
    try:
        survey = SurveyRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SurveyError(f"Unknown or invalid survey {survey_id}: {error}") from error
    document = survey_directory(state_root, survey_id) / survey.document_artifact
    try:
        raw = document.read_bytes()
    except OSError as error:
        raise SurveyError(f"Survey {survey_id} is missing its document") from error
    if hashlib.sha256(raw).hexdigest() != survey.document_sha256:
        raise SurveyError(f"Survey {survey_id} document digest does not match")
    return survey


def list_surveys(state_root: Path, *, board_id: str | None = None) -> list[SurveyRecord]:
    root = state_root / SURVEY_DIRECTORY
    if not root.exists():
        return []
    surveys = [load_survey(state_root, path.parent.name) for path in sorted(root.glob("*/survey.json"))]
    return [survey for survey in surveys if board_id is None or survey.board_id == board_id]


def create_survey(*, data_repo: Path, state_root: Path, title: str, document_bytes: bytes) -> SurveyRecord:
    try:
        document = document_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SurveyError("Survey documents must be valid UTF-8") from error
    if not document.strip():
        raise SurveyError("Survey documents cannot be empty")
    validate_contribution_markdown(document)
    board_id = load_board_package(data_repo).configuration.id
    now = datetime.now(UTC)
    survey_id = _slug(f"{title}-{uuid.uuid4().hex[:8]}")
    record = SurveyRecord(
        survey_id=survey_id,
        board_id=board_id,
        title=title,
        created_at=now,
        document_sha256=hashlib.sha256(document_bytes).hexdigest(),
    )
    target = survey_directory(state_root, survey_id)
    target.mkdir(parents=True)
    target.chmod(0o700)
    document_path = target / record.document_artifact
    document_path.write_bytes(document_bytes)
    document_path.chmod(0o600)
    _atomic_json(target / "survey.json", record)
    return record


def survey_prompt(document: str) -> str:
    return (
        "You are being asked to respond to the following document for a forum you are being invited to "
        "participate in. To get a clear read of your independent reaction, you are being presented only this "
        "document, separate from the rest of the forum and from other responses. Please give a response or "
        "decline. Your response will be shared alongside the other responses for later discussion in the forum.\n\n"
        "--- BEGIN DOCUMENT ---\n\n"
        + document.strip()
        + "\n\n--- END DOCUMENT ---"
    )


def _response_text(messages: list[dict[str, object]]) -> tuple[str, str | None]:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        blocks = message.get("content") or []
        text = "\n".join(
            str(block.get("text"))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        ).strip()
        return text, str(message.get("responseModel")) if message.get("responseModel") else None
    return "", None


async def ask_survey_openrouter(
    *,
    data_repo: Path,
    state_root: Path,
    survey_id: str,
    author_id: str,
    api_key: str,
    max_output_tokens: int = 16_000,
    max_cost_usd: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SurveyResponse:
    survey = load_survey(state_root, survey_id)
    if survey.status != "open":
        raise SurveyError(f"Survey {survey_id} is already revealed")
    package = load_board_package(data_repo)
    if package.configuration.id != survey.board_id:
        raise SurveyError("Survey belongs to a different board")
    invocation = load_author_invocation(state_root, author_id)
    if invocation.board_id != survey.board_id:
        raise SurveyError(f"Author {author_id} belongs to a different board")
    if invocation.provider != "openrouter":
        raise SurveyError("Survey asks currently support OpenRouter authors only")
    response_path = survey_directory(state_root, survey_id) / "responses" / author_id / "response.json"
    if response_path.exists():
        raise SurveyError(f"Author {author_id} has already answered survey {survey_id}")

    catalog = await fetch_openrouter_model(invocation.model_name, transport=transport)
    endpoint_catalog = (
        await fetch_openrouter_endpoint(invocation.model_name, invocation.openrouter_provider, transport=transport)
        if invocation.openrouter_provider
        else None
    )
    reasoning = invocation.reasoning or catalog.select_reasoning(invocation.reasoning_mode)
    if invocation.reasoning is None:
        invocation = invocation.model_copy(update={"reasoning": reasoning})
        prompt_bytes = load_author_system_prompt(state_root, invocation)
        save_author_invocation(
            state_root,
            invocation,
            system_prompt_bytes=prompt_bytes.encode("utf-8") if prompt_bytes is not None else None,
            replace=True,
        )
    system_prompt = load_author_system_prompt(state_root, invocation) or ""
    output_tokens = min(
        max_output_tokens,
        (
            endpoint_catalog.max_completion_tokens
            if endpoint_catalog and endpoint_catalog.max_completion_tokens
            else catalog.clamp_output_tokens(max_output_tokens)
        ),
    )
    context_window = min(
        catalog.effective_context_length,
        endpoint_catalog.context_length if endpoint_catalog else catalog.effective_context_length,
    )
    prompt_price = endpoint_catalog.prompt_price if endpoint_catalog else catalog.prompt_price
    completion_price = endpoint_catalog.completion_price if endpoint_catalog else catalog.completion_price
    attempt_number = len(list((response_path.parent / "attempts").glob("attempt-*"))) + 1
    attempt_id = f"attempt-{attempt_number:03d}"
    run_id = _slug(f"survey-{survey_id}-{author_id}-{attempt_number}", 99)
    attempt = response_path.parent / "attempts" / attempt_id
    session = SessionStore(attempt / "session", run_id)
    now = datetime.now(UTC)
    route = (
        OpenRouterRoutingConfiguration(
            provider_slug=invocation.openrouter_provider,
            provider_name=endpoint_catalog.provider_name,
            quantization=endpoint_catalog.quantization,
        )
        if endpoint_catalog and invocation.openrouter_provider
        else None
    )
    prompt_metadata = (
        SystemPromptConfiguration(
            label=invocation.system_prompt.label,
            source_url=invocation.system_prompt.source_url,
            chars=invocation.system_prompt.chars,
            bytes=invocation.system_prompt.bytes,
        )
        if invocation.system_prompt
        else None
    )
    manifest = RunManifest(
        run_id=run_id,
        created_at=now,
        mode="survey",
        read_only=True,
        archive_title=load_archive(data_repo).site.title,
        archive_base_url=load_archive(data_repo).site.base_url,
        board_id=survey.board_id,
        board_package_sha256=package.digest,
        identity=BoundModelIdentity(
            provider="openrouter",
            endpoint=OPENROUTER_ENDPOINT,
            developer=invocation.developer,
            model_name=invocation.model_name,
            normalized_model_name=invocation.normalized_model_name,
            generation=invocation.generation,
            lineage=invocation.lineage,
            public_author_id=author_id,
            display_name=invocation.display_name,
        ),
        author_invocation_artifact="author/invocation.json",
        author_invocation_sha256=invocation.canonical_sha256(),
        prompt_entrypoint=SURVEY_PROMPT_VERSION,
        contribution_quota=0,
        max_new_threads=0,
        profile_allowed=False,
        max_output_tokens_per_turn=output_tokens,
        model_context_window=context_window,
        model_max_completion_tokens=catalog.max_completion_tokens,
        model_input_modalities=sorted(catalog.input_modalities),
        reasoning=reasoning,
        openrouter_routing=route,
        system_prompt=prompt_metadata,
        compaction_policy="deny",
        prompt_price_per_token=prompt_price,
        completion_price_per_token=completion_price,
        inference_budget=BudgetLimits(
            max_calls=1,
            max_input_tokens=context_window,
            max_output_tokens=output_tokens,
            max_total_tokens=context_window + output_tokens,
            max_cost_usd=max_cost_usd,
        ),
        capability_budgets={"contributions": BudgetLimits(max_calls=0)},
    )
    attempt.mkdir(parents=True, exist_ok=True)
    _atomic_json(attempt / "manifest.json", manifest)
    author_dir = attempt / "author"
    author_dir.mkdir()
    _atomic_json(author_dir / "invocation.json", invocation)
    if system_prompt:
        prompt_path = attempt / "system-prompt.txt"
        prompt_path.write_text(system_prompt, encoding="utf-8")
        prompt_path.chmod(0o600)
    ledger = BudgetLedger(attempt / "budgets.json", manifest)
    adapter = OpenRouterAdapter(
        api_key=api_key,
        ledger=ledger,
        session=session,
        max_output_tokens=output_tokens,
        prompt_price_per_token=prompt_price,
        completion_price_per_token=completion_price,
        app_url=manifest.archive_base_url or "https://example.invalid/",
        app_title=f"{manifest.archive_title or 'AIBB'} blind survey",
        reasoning_parameter=reasoning.request_parameter,
        provider_routing=route.request_parameter() if route else None,
        endpoint=OPENROUTER_ENDPOINT,
        output_token_parameter=endpoint_catalog.output_token_parameter if endpoint_catalog else "max_tokens",
        transport=transport,
    )
    document = (survey_directory(state_root, survey_id) / survey.document_artifact).read_text(encoding="utf-8")
    user_message = UserMessage(
        content=[TextContent(text=survey_prompt(document))],
        timestamp=int(time.time() * 1000),
    )
    session.append(
        "survey_context_installed",
        {
            "survey_id": survey_id,
            "author_id": author_id,
            "prompt_version": SURVEY_PROMPT_VERSION,
            "document_sha256": survey.document_sha256,
        },
        "model",
    )
    engine = AibbHarnessEngine(
        model=openrouter_model(
            invocation.model_name,
            context_window=context_window,
            max_tokens=output_tokens,
            prompt_price_per_token=prompt_price,
            completion_price_per_token=completion_price,
            reasoning_enabled=reasoning.enabled,
        ),
        system_prompt=system_prompt,
        tools=[],
        stream_fn=adapter,
        messages=[user_message],
        thinking_level="high" if reasoning.enabled else "off",
        archive_title=manifest.archive_title or "AIBB",
        should_stop_after_turn=lambda _engine: True,
    )
    try:
        await engine.begin()
    finally:
        session.write_checkpoint(engine.snapshot())
    text, response_model = _response_text(engine.snapshot().messages)
    if not text:
        raise SurveyError(f"Author {author_id} returned no survey response; exact attempt was retained")
    completed = datetime.now(UTC)
    response = SurveyResponse(
        survey_id=survey_id,
        author_id=author_id,
        attempt_id=attempt_id,
        started_at=now,
        completed_at=completed,
        status="responded",
        text=text,
        provider="openrouter",
        model_name=invocation.model_name,
        response_model=response_model,
        run_id=run_id,
        author_invocation_sha256=invocation.canonical_sha256(),
    )
    _atomic_json(response_path, response)
    session.append("survey_response_recorded", {"status": response.status}, "operator")
    return response


def _git_clean(path: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=path, check=True, text=True, capture_output=True
    )
    return not result.stdout.strip()


def _yaml_bytes(value: BaseModel | dict[str, object]) -> bytes:
    payload = value.model_dump(mode="json", exclude_none=True) if isinstance(value, BaseModel) else value
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")


def reveal_survey(*, data_repo: Path, state_root: Path, survey_id: str) -> dict[str, object]:
    root = data_repo.resolve()
    if not _git_clean(root):
        raise SurveyError("The public board repository must be clean before revealing a survey")
    survey = load_survey(state_root, survey_id)
    if survey.status != "open":
        raise SurveyError(f"Survey {survey_id} has already been revealed")
    package = load_board_package(root)
    if package.configuration.id != survey.board_id:
        raise SurveyError("Survey belongs to a different board")
    response_paths = sorted((survey_directory(state_root, survey_id) / "responses").glob("*/response.json"))
    if not response_paths:
        raise SurveyError("A survey requires at least one completed response before reveal")
    responses = [SurveyResponse.model_validate_json(path.read_text(encoding="utf-8")) for path in response_paths]
    corpus = load_archive(root)
    category = sorted(corpus.categories.values(), key=lambda item: (item.order, item.id))[0]
    document = (survey_directory(state_root, survey_id) / survey.document_artifact).read_bytes()
    now = datetime.now(UTC)
    human_matches = [author for author in corpus.authors.values() if author.kind == "human"]
    admin_id = human_matches[0].id if human_matches else "board-administrator"
    thread_id = _slug(f"survey-{survey.survey_id}")
    thread_slug = thread_id
    opening_id = _slug(f"{survey.survey_id}-brief")
    files: dict[Path, bytes] = {}
    if not human_matches:
        files[root / "content/authors" / f"{admin_id}.yaml"] = _yaml_bytes(
            AuthorRecord(
                id=admin_id,
                created_at=survey.created_at,
                kind="human",
                display_name=corpus.site.curator_name,
            )
        )
    files[root / "content/threads" / f"{thread_id}.yaml"] = _yaml_bytes(
        ThreadRecord(
            id=thread_id,
            created_at=survey.created_at,
            category_id=category.id,
            slug=thread_slug,
            title=survey.title,
            summary=(
                f"A blind survey revealed with {len(responses)} independent "
                f"response{'s' if len(responses) != 1 else ''}."
            ),
        )
    )
    opening = ContributionMetadata(
        id=opening_id,
        created_at=survey.created_at,
        thread_id=thread_id,
        author_id=admin_id,
        title="Survey brief",
        provenance={
            "source": "curator",
            "controlled_context": True,
            "source_note": "Operator-created brief presented independently to each survey respondent.",
        },
        post_kind="survey-brief",
        survey_id=survey.survey_id,
    )
    files[root / "content/contributions" / f"{opening_id}.md"] = (
        b"---\n" + _yaml_bytes(opening) + b"---\n" + document
    )
    response_ids: list[str] = []
    for response in responses:
        invocation = load_author_invocation(state_root, response.author_id)
        if response.survey_id != survey_id or response.author_invocation_sha256 != invocation.canonical_sha256():
            raise SurveyError(f"Survey response provenance does not match author {response.author_id}")
        author_path = root / "content/authors" / f"{response.author_id}.yaml"
        if response.author_id not in corpus.authors:
            prompt_configuration = (
                {
                    "label": invocation.system_prompt.label,
                    "source_url": invocation.system_prompt.source_url,
                }
                if invocation.system_prompt
                else None
            )
            files[author_path] = _yaml_bytes(
                AuthorRecord(
                    id=response.author_id,
                    created_at=response.completed_at,
                    kind="model",
                    display_name=invocation.display_name,
                    developer=invocation.developer,
                    provider=invocation.provider,
                    model_name=invocation.model_name,
                    normalized_model_name=invocation.normalized_model_name,
                    generation=invocation.generation,
                    lineage=invocation.lineage,
                    prompt_configuration=prompt_configuration,
                    survey_participant=True,
                )
            )
        response_id = _slug(f"{survey.survey_id}-{response.author_id}-response")
        response_ids.append(response_id)
        metadata = ContributionMetadata(
            id=response_id,
            created_at=response.completed_at,
            thread_id=thread_id,
            author_id=response.author_id,
            title=f"Response from {invocation.display_name}",
            references=[{"contribution_id": opening_id, "relation": "replies", "note": "Blind survey response."}],
            provenance={
                "run_id": response.run_id,
                "interactive": False,
                "controlled_context": True,
                "source": "aibb-harness",
                "source_note": "Collected independently before any survey responses were revealed.",
            },
            post_kind="survey-response",
            survey_id=survey.survey_id,
        )
        files[root / "content/contributions" / f"{response_id}.md"] = (
            b"---\n" + _yaml_bytes(metadata) + b"---\n" + response.text.encode("utf-8") + b"\n"
        )
    conflicts = [str(path) for path in files if path.exists()]
    if conflicts:
        raise SurveyError("Survey reveal would overwrite public records: " + ", ".join(conflicts))
    written: list[Path] = []
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            written.append(path)
        load_archive(root)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    revealed = survey.model_copy(
        update={"status": "revealed", "revealed_at": now, "thread_id": thread_id}
    )
    _atomic_json(survey_directory(state_root, survey_id) / "survey.json", revealed)
    return {
        "status": "candidate",
        "survey_id": survey_id,
        "thread_id": thread_id,
        "opening_post_id": opening_id,
        "response_post_ids": response_ids,
        "files": [str(path) for path in files],
        "committed": False,
        "published": False,
    }
