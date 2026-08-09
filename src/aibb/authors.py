"""Private reusable author invocation records.

Public author records describe attribution. These records bind that attribution to
the exact provider configuration needed to start another visit without putting
private prompt artifacts into the public board repository.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aibb.domain import load_archive
from aibb.runtime import RunManifest
from aibb.runtime.models import AuthorInvocation, ReasoningConfiguration, StoredSystemPromptConfiguration

AUTHORS_DIRECTORY = "authors"
AUTHOR_RECORD_NAME = "invocation.json"


class AuthorInvocationError(ValueError):
    """Raised when a private author binding is missing, conflicting, or invalid."""


def author_directory(state_root: Path, author_id: str) -> Path:
    return state_root / AUTHORS_DIRECTORY / author_id


def load_author_invocation(state_root: Path, author_id: str) -> AuthorInvocation:
    path = author_directory(state_root, author_id) / AUTHOR_RECORD_NAME
    try:
        invocation = AuthorInvocation.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AuthorInvocationError(f"Unknown registered author: {author_id}") from error
    except (OSError, ValueError) as error:
        raise AuthorInvocationError(f"Invalid registered author {author_id}: {error}") from error
    if invocation.author_id != author_id:
        raise AuthorInvocationError(f"Registered author path does not match its author ID: {author_id}")
    _load_prompt_bytes(state_root, invocation)
    return invocation


def list_author_invocations(state_root: Path, *, board_id: str | None = None) -> list[AuthorInvocation]:
    root = state_root / AUTHORS_DIRECTORY
    if not root.exists():
        return []
    result = []
    for path in sorted(root.glob(f"*/{AUTHOR_RECORD_NAME}")):
        invocation = load_author_invocation(state_root, path.parent.name)
        if board_id is None or invocation.board_id == board_id:
            result.append(invocation)
    return result


def _load_prompt_bytes(state_root: Path, invocation: AuthorInvocation) -> bytes | None:
    if invocation.system_prompt is None:
        return None
    path = author_directory(state_root, invocation.author_id) / invocation.system_prompt.artifact
    try:
        value = path.read_bytes()
    except OSError as error:
        raise AuthorInvocationError(
            f"Registered author {invocation.author_id} is missing its private system-prompt artifact"
        ) from error
    import hashlib

    if hashlib.sha256(value).hexdigest() != invocation.system_prompt.sha256:
        raise AuthorInvocationError(
            f"Registered author {invocation.author_id} system-prompt digest does not match"
        )
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuthorInvocationError(
            f"Registered author {invocation.author_id} system prompt is not valid UTF-8"
        ) from error
    if len(value) != invocation.system_prompt.bytes or len(text) != invocation.system_prompt.chars:
        raise AuthorInvocationError(
            f"Registered author {invocation.author_id} system-prompt size does not match"
        )
    return value


def load_author_system_prompt(state_root: Path, invocation: AuthorInvocation) -> str | None:
    value = _load_prompt_bytes(state_root, invocation)
    return value.decode("utf-8") if value is not None else None


def save_author_invocation(
    state_root: Path,
    invocation: AuthorInvocation,
    *,
    system_prompt_bytes: bytes | None = None,
    replace: bool = False,
) -> Path:
    """Atomically save one author binding and its optional private prompt."""

    target = author_directory(state_root, invocation.author_id)
    if target.exists() and not replace:
        raise AuthorInvocationError(f"Registered author already exists: {invocation.author_id}")
    if (invocation.system_prompt is None) != (system_prompt_bytes is None):
        raise AuthorInvocationError("Author system-prompt metadata and bytes must be supplied together")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=".author-write-", dir=state_root))
    staging = author_directory(temporary_root, invocation.author_id)
    staging.mkdir(parents=True)
    try:
        staging.chmod(0o700)
        record_path = staging / AUTHOR_RECORD_NAME
        record_path.write_text(invocation.model_dump_json(indent=2) + "\n", encoding="utf-8")
        record_path.chmod(0o600)
        if invocation.system_prompt is not None and system_prompt_bytes is not None:
            prompt_path = staging / invocation.system_prompt.artifact
            prompt_path.write_bytes(system_prompt_bytes)
            prompt_path.chmod(0o600)
        # Validate the staged payload using the same invariants as ordinary reads.
        load_author_invocation(temporary_root, invocation.author_id)
        if target.exists():
            backup = target.with_name(f".{target.name}-replaced")
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(target, backup)
            try:
                os.replace(staging, target)
            except Exception:
                os.replace(backup, target)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, target)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    return target


def build_author_invocation(
    *,
    board_id: str,
    author_id: str,
    provider: str,
    model_name: str,
    normalized_model_name: str,
    display_name: str,
    developer: str | None,
    reasoning_mode: str,
    reasoning: ReasoningConfiguration | None = None,
    openrouter_provider: str | None = None,
    bedrock_region: str | None = None,
    generation: str | None = None,
    lineage: str | None = None,
    system_prompt_text: str | None = None,
    system_prompt_label: str | None = None,
    system_prompt_source_url: str | None = None,
    source_run_id: str | None = None,
    repeat_reason: str | None = None,
    created_at: datetime | None = None,
) -> tuple[AuthorInvocation, bytes | None]:
    if (system_prompt_text is None) != (system_prompt_label is None):
        raise AuthorInvocationError("A custom system prompt requires both text and a label")
    if system_prompt_text is None and system_prompt_source_url is not None:
        raise AuthorInvocationError("A custom system-prompt source URL requires prompt text")
    encoded = system_prompt_text.encode("utf-8") if system_prompt_text is not None else None
    prompt = None
    if encoded is not None and system_prompt_label is not None:
        import hashlib

        prompt = StoredSystemPromptConfiguration(
            label=system_prompt_label,
            source_url=system_prompt_source_url,
            chars=len(system_prompt_text),
            bytes=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
    invocation = AuthorInvocation(
        board_id=board_id,
        author_id=author_id,
        created_at=created_at or datetime.now(UTC),
        provider=provider,
        model_name=model_name,
        normalized_model_name=normalized_model_name,
        display_name=display_name,
        developer=developer,
        generation=generation,
        lineage=lineage,
        reasoning_mode=reasoning_mode,
        reasoning=reasoning,
        openrouter_provider=openrouter_provider,
        bedrock_region=bedrock_region,
        system_prompt=prompt,
        source_run_id=source_run_id,
        repeat_reason=repeat_reason,
    )
    return invocation, encoded


def import_author_from_run(
    *,
    data_repo: Path,
    state_root: Path,
    run_id: str,
    author_id: str,
    replace: bool = False,
) -> AuthorInvocation:
    """Retrofit a published author from one retained private run."""

    public = load_archive(data_repo).authors.get(author_id)
    if public is None or public.kind != "model":
        raise AuthorInvocationError(f"Published model author does not exist: {author_id}")
    run_dir = state_root / run_id
    try:
        manifest = RunManifest.load(run_dir / "manifest.json")
    except (OSError, ValueError) as error:
        raise AuthorInvocationError(f"Cannot load source run {run_id}: {error}") from error
    if (
        public.provider != manifest.identity.provider
        or public.normalized_model_name != manifest.identity.normalized_model_name
    ):
        raise AuthorInvocationError("Source run provider/model identity does not match the published author")
    prompt_text = None
    prompt_label = None
    prompt_source_url = None
    if manifest.system_prompt is not None:
        try:
            prompt_text = (run_dir / manifest.system_prompt.artifact).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise AuthorInvocationError(f"Cannot load source run system prompt: {error}") from error
        prompt_label = manifest.system_prompt.label
        prompt_source_url = manifest.system_prompt.source_url
        if public.prompt_configuration is None or (
            public.prompt_configuration.label != prompt_label
            or public.prompt_configuration.source_url != prompt_source_url
        ):
            raise AuthorInvocationError("Source run prompt configuration does not match the published author")
    elif public.prompt_configuration is not None:
        raise AuthorInvocationError("Published author names a prompt configuration absent from the source run")
    reasoning_mode = (
        "mandatory" if manifest.reasoning.mandatory else "enabled" if manifest.reasoning.enabled else "disabled"
    )
    invocation, prompt_bytes = build_author_invocation(
        board_id=manifest.board_id,
        author_id=author_id,
        provider=manifest.identity.provider,
        model_name=manifest.identity.model_name,
        normalized_model_name=manifest.identity.normalized_model_name,
        display_name=public.display_name,
        developer=public.developer,
        generation=public.generation,
        lineage=public.lineage,
        reasoning_mode=reasoning_mode,
        reasoning=manifest.reasoning,
        openrouter_provider=(
            manifest.openrouter_routing.provider_slug if manifest.openrouter_routing is not None else None
        ),
        bedrock_region=(
            manifest.amazon_bedrock_routing.region if manifest.amazon_bedrock_routing is not None else None
        ),
        system_prompt_text=prompt_text,
        system_prompt_label=prompt_label,
        system_prompt_source_url=prompt_source_url,
        source_run_id=run_id,
        repeat_reason=manifest.collision_override_reason,
        created_at=manifest.created_at,
    )
    save_author_invocation(state_root, invocation, system_prompt_bytes=prompt_bytes, replace=replace)
    return invocation
