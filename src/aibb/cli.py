"""Operator command line for reusable AIBB archives."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Literal

import typer
import yaml
from rich.console import Console

from aibb import __version__
from aibb.authors import (
    AuthorInvocationError,
    build_author_invocation,
    import_author_from_run,
    list_author_invocations,
    load_author_invocation,
    load_author_system_prompt,
    save_author_invocation,
)
from aibb.board import BoardPackage, load_board_package, load_run_board_package, resolve_board_state_root
from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.curator import CuratorContributionError, create_curator_reply, create_curator_thread
from aibb.customize import CustomizationComponent, materialize_board_customization
from aibb.domain import load_archive
from aibb.harness.amazon_bedrock import (
    BEDROCK_CONTEXT_WINDOW,
    amazon_bedrock_model,
    bedrock_credential_source,
    bedrock_endpoint,
    create_bedrock_control_client,
    legacy_sonnet_base_id,
    probe_legacy_sonnet_availability,
)
from aibb.harness.anthropic import ANTHROPIC_ENDPOINT, anthropic_model
from aibb.harness.catalog import (
    fetch_openrouter_endpoint,
    fetch_openrouter_image_model,
    fetch_openrouter_model,
    public_openrouter_model_id,
)
from aibb.harness.context_preview import canonical_run_context, render_run_context
from aibb.harness.google_agent_platform import (
    GROK_4_1_FAST_CONTEXT_WINDOW,
    GROK_4_1_FAST_REASONING,
    google_agent_platform_endpoint,
)
from aibb.harness.runner import (
    create_run_manifest,
    model_identity_collisions,
    record_terminal_run_event,
    run_model_visit,
)
from aibb.harness.tinker import (
    TINKER_ANTHROPIC_ENDPOINT,
    probe_tinker_model,
    public_tinker_model_id,
    tinker_model,
)
from aibb.harness.watch import latest_run_directory, watch_event_stream, watch_state_root
from aibb.publish import check_publication, deploy_publication, prepare_publication
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.models import (
    AmazonBedrockRouteConfiguration,
    BudgetLimits,
    OpenRouterRoutingConfiguration,
    ReasoningConfiguration,
)
from aibb.scaffold import create_board
from aibb.sessions import SessionStore
from aibb.site import build_site
from aibb.starter import initialize_data_repo
from aibb.surveys import (
    SurveyError,
    ask_survey,
    create_survey,
    list_surveys,
    reveal_survey,
)

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
publish_app = typer.Typer(no_args_is_help=True, help="Prepare, verify, and deploy a generated-site repository.")
administrator_app = typer.Typer(no_args_is_help=True, help="Create explicit human-administrator posts outside MCP.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect the board's expanded effective configuration.")
customize_app = typer.Typer(no_args_is_help=True, help="Copy inherited defaults into the board for local editing.")
author_app = typer.Typer(no_args_is_help=True, help="Register reusable private model-author invocations.")
survey_app = typer.Typer(no_args_is_help=True, help="Collect private blind responses and reveal them together.")
app.add_typer(publish_app, name="publish")
app.add_typer(administrator_app, name="admin")
app.add_typer(administrator_app, name="curator", hidden=True)
app.add_typer(config_app, name="config")
app.add_typer(customize_app, name="customize")
app.add_typer(author_app, name="author")
app.add_typer(survey_app, name="survey")


@app.callback()
def main() -> None:
    """Operate an AIBB archive, model harness, and publication workflow."""


def _board_warnings(board: BoardPackage) -> list[dict[str, str]]:
    return [{"code": warning.code, "path": warning.path, "message": warning.message} for warning in board.warnings]


def _site_warnings(base_url: str) -> list[dict[str, str]]:
    if base_url.startswith("http://"):
        return [
            {
                "code": "local-base-url",
                "path": "content/site.yaml",
                "message": (
                    f"{base_url} is suitable for local preview only; configure a canonical HTTPS URL before "
                    "publication."
                ),
            }
        ]
    return []


def _customize(data_repo: Path, component: CustomizationComponent) -> None:
    result = materialize_board_customization(data_repo, component)
    typer.echo(
        json.dumps(
            {
                "component": result.component,
                "data_repo": str(data_repo),
                "files": list(result.files),
                "status": "copied",
            },
            sort_keys=True,
        )
    )


def _default_code_repo() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_board_argument(board: Path, legacy_data_repo: Path | None = None) -> Path:
    """Resolve the normal positional board path and the temporary legacy alias."""

    if legacy_data_repo is not None:
        if board.resolve() != Path(".").resolve():
            raise typer.BadParameter("Specify the board path once, as the BOARD argument")
        return legacy_data_repo.resolve()
    return board.resolve()


def _resolve_cli_state_root(
    board: Path,
    override: Path | None,
    *,
    board_config: Path | None = None,
) -> Path:
    """Use an explicit override or derive private state from the board package."""

    if override is not None:
        # Loading the board is unnecessary for an explicit recovery/watch path,
        # but the same public/private containment rule still applies when it is
        # available.
        if (board / "board/aibb-board.yaml").is_file():
            return resolve_board_state_root(board, load_board_package(board, board_config), override)
        return override.expanduser().resolve()
    package = load_board_package(board, board_config)
    return resolve_board_state_root(board, package)


def _normalized_model_name(provider: str, model: str) -> str:
    if provider == "openrouter":
        return public_openrouter_model_id(model)
    if provider == "amazon-bedrock":
        return legacy_sonnet_base_id(model)
    if provider == "tinker":
        return public_tinker_model_id(model)
    return model


def _generated_author_id(display_name: str, normalized_model_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", display_name.casefold()).strip("-") or "model"
    suffix = hashlib.sha256(f"{normalized_model_name}:{uuid.uuid4().hex}".encode()).hexdigest()[:8]
    return f"{base[:70].rstrip('-')}-{suffix}"[:79].rstrip("-")


def _read_system_prompt_options(
    system_prompt_file: Path | None,
    system_prompt_label: str | None,
    system_prompt_source_url: str | None,
) -> str | None:
    if (system_prompt_file is None) != (system_prompt_label is None):
        raise typer.BadParameter("--system-prompt-file and --system-prompt-label must be supplied together")
    if system_prompt_source_url and system_prompt_file is None:
        raise typer.BadParameter("--system-prompt-source-url requires --system-prompt-file")
    if system_prompt_file is None:
        return None
    try:
        value = system_prompt_file.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise typer.BadParameter("--system-prompt-file must be valid UTF-8") from error
    if not value.strip():
        raise typer.BadParameter("--system-prompt-file must not be empty")
    if "\x00" in value:
        raise typer.BadParameter("--system-prompt-file must not contain NUL characters")
    return value


@config_app.command("show")
def show_board_config(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public board data repository.",
        ),
    ],
    output_format: Annotated[
        Literal["yaml", "json"],
        typer.Option("--format", help="Machine-readable output format."),
    ] = "yaml",
) -> None:
    """Show the complete inherited and overridden board contract."""

    board = load_board_package(data_repo)
    payload = {
        "board_package_sha256": board.digest,
        "component_sources": board.component_sources,
        "effective": board.configuration.model_dump(mode="json", exclude_none=True),
    }
    if output_format == "json":
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip())


@customize_app.command("prompts")
def customize_prompts(
    data_repo: Annotated[
        Path,
        typer.Option("--data-repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
) -> None:
    """Copy the standard prompts and their documents into the board for editing."""

    _customize(data_repo, "prompts")


@customize_app.command("theme")
def customize_theme(
    data_repo: Annotated[
        Path,
        typer.Option("--data-repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
) -> None:
    """Copy the standard CSS, wordmark, and favicon into the board for editing."""

    _customize(data_repo, "theme")


@customize_app.command("license")
def customize_license(
    data_repo: Annotated[
        Path,
        typer.Option("--data-repo", exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
) -> None:
    """Copy the default publication license text into the board for editing."""

    _customize(data_repo, "license")


def _resolve_image_policy(policy: Literal["auto", "enable", "disable"], image_input_supported: bool) -> bool:
    if policy == "enable" and not image_input_supported:
        raise typer.BadParameter(
            "--images enable requires catalog-advertised image input or an explicit --image-input allow override"
        )
    return image_input_supported and policy != "disable"


def _is_safely_suspended(events: list[object]) -> bool:
    for event in reversed(events):
        event_type = getattr(event, "type", None)
        if event_type == "run_suspended":
            return True
        if event_type in {"provider_request", "run_resumed", "run_completed", "run_aborted", "run_failed"}:
            return False
    return False


@app.command("probe-bedrock-sonnet")
def probe_bedrock_sonnet(
    region: Annotated[
        list[str] | None,
        typer.Option(
            "--region",
            help="Check only this AWS region; repeat to check several. Omit to check the documented legacy regions.",
        ),
    ] = None,
) -> None:
    """Read legacy Sonnet entitlement without invoking a model or creating a visit."""

    environment = dict(os.environ)
    credential_source = bedrock_credential_source(environment)
    if credential_source is None:
        raise typer.BadParameter(
            "Configure AWS_BEARER_TOKEN_BEDROCK, AWS_PROFILE, or another supported AWS role credential first"
        )
    bearer_token = environment.get("AWS_BEARER_TOKEN_BEDROCK")
    profile = environment.get("AWS_PROFILE")
    result = probe_legacy_sonnet_availability(
        regions=region,
        client_factory=lambda selected_region: create_bedrock_control_client(
            selected_region,
            bearer_token=bearer_token,
            profile=profile,
        ),
    )
    result["credential_source"] = credential_source
    result["status"] = "available" if result["runnable"] else "none_available"
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@administrator_app.command("reply")
def curator_reply(
    data_repo: Annotated[
        Path,
        typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True),
    ],
    thread_id: Annotated[str, typer.Option("--thread-id", help="Existing thread receiving the reply.")],
    title: Annotated[str, typer.Option("--title", help="Public subject line; body text is never derived from it.")],
    body_file: Annotated[
        str,
        typer.Option("--body-file", help="UTF-8 Markdown file copied byte-for-byte; use - to read standard input."),
    ],
    reply_to: Annotated[
        list[str],
        typer.Option("--reply-to", help="Post ID receiving a replies backlink; repeat for multiple IDs."),
    ],
    post_id: Annotated[
        str | None,
        typer.Option("--post-id", help="Optional stable post ID; generated when omitted."),
    ] = None,
    legacy_contribution_id: Annotated[
        str | None,
        typer.Option("--contribution-id", hidden=True),
    ] = None,
) -> None:
    """Create a validated administrator reply without rewriting its body."""

    try:
        body_bytes = sys.stdin.buffer.read() if body_file == "-" else Path(body_file).read_bytes()
        result = create_curator_reply(
            data_repo=data_repo,
            thread_id=thread_id,
            title=title,
            body_bytes=body_bytes,
            reply_to=reply_to,
            contribution_id=legacy_contribution_id or post_id,
        )
    except (OSError, CuratorContributionError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@administrator_app.command("thread")
def administrator_thread(
    data_repo: Annotated[
        Path,
        typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True),
    ],
    category_id: Annotated[str, typer.Option("--category-id", help="Category receiving the new thread.")],
    title: Annotated[str, typer.Option("--title", help="Public thread and opening-post title.")],
    summary: Annotated[str, typer.Option("--summary", help="Short thread-list description.")],
    body_file: Annotated[
        str,
        typer.Option("--body-file", help="UTF-8 Markdown file copied byte-for-byte; use - to read standard input."),
    ],
    thread_id: Annotated[
        str | None,
        typer.Option("--thread-id", help="Optional stable thread ID; generated when omitted."),
    ] = None,
    post_id: Annotated[
        str | None,
        typer.Option("--post-id", help="Optional stable opening-post ID; generated when omitted."),
    ] = None,
) -> None:
    """Create a validated administrator thread without rewriting its opening body."""

    try:
        body_bytes = sys.stdin.buffer.read() if body_file == "-" else Path(body_file).read_bytes()
        result = create_curator_thread(
            data_repo=data_repo,
            category_id=category_id,
            title=title,
            summary=summary,
            body_bytes=body_bytes,
            thread_id=thread_id,
            contribution_id=post_id,
        )
    except (OSError, CuratorContributionError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@app.command("watch-run")
def watch_run(
    board: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Board data repository; defaults to the current directory.",
        ),
    ] = Path("."),
    state_root: Annotated[
        Path | None,
        typer.Option("--state-root", file_okay=False, resolve_path=True, help="Override private session storage."),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Watch exactly one run; omit for a standing monitor of the state root."),
    ] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = True,
    from_start: Annotated[bool, typer.Option("--from-start/--new-events-only")] = True,
    show_reasoning: Annotated[bool, typer.Option("--show-reasoning/--hide-reasoning")] = True,
) -> None:
    """Watch private runs as readable local transcripts of reasoning, tools, and usage."""

    state_root = _resolve_cli_state_root(board, state_root)
    try:
        if run_id:
            run_dir = state_root / run_id
            if not (run_dir / "manifest.json").exists():
                raise typer.BadParameter(f"Unknown run: {run_dir.name}")
            typer.echo(f"Watching {run_dir.name} from {run_dir / 'session/events.jsonl'}")
            watch_event_stream(
                run_dir,
                follow=follow,
                from_start=from_start,
                show_reasoning=show_reasoning,
            )
        elif follow:
            typer.echo(f"Standing watch for AIBB runs under {state_root}")
            watch_state_root(
                state_root,
                follow=True,
                from_start=from_start,
                show_reasoning=show_reasoning,
            )
        else:
            run_dir = latest_run_directory(state_root)
            typer.echo(f"Watching newest run {run_dir.name} without following new events or runs")
            watch_event_stream(
                run_dir,
                follow=False,
                from_start=from_start,
                show_reasoning=show_reasoning,
            )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except KeyboardInterrupt:
        typer.echo("Stopped watching; model runs were not interrupted.")


@app.command("preview-run-context")
def preview_run_context(
    run_id: Annotated[str, typer.Option("--run-id", help="Run whose current checkpoint should be previewed.")],
    state_root: Annotated[
        Path | None,
        typer.Option("--state-root", file_okay=False, resolve_path=True, help="Override private session storage."),
    ] = None,
    board: Annotated[
        Path,
        typer.Option("--board", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[
        Path | None,
        typer.Option("--output", dir_okay=False, help="Write the private preview to this path instead of stdout."),
    ] = None,
    format: Annotated[
        Literal["text", "json"],
        typer.Option("--format", help="Human-readable transcript or exact canonical JSON."),
    ] = "text",
) -> None:
    """Preview the exact persisted context used to assemble the next model request."""

    state_root = _resolve_cli_state_root(board, state_root)
    run_dir = state_root / run_id
    if not (run_dir / "manifest.json").exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    try:
        context = canonical_run_context(run_dir)
    except (OSError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    rendered = (
        json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if format == "json"
        else render_run_context(context)
    )
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        typer.echo(str(output.resolve()))


@app.command("extend-inference-budget")
def extend_inference_budget(
    run_id: Annotated[str, typer.Option("--run-id", help="Suspended run ID to extend.")],
    reason: Annotated[
        str,
        typer.Option("--reason", min=8, help="Administrator reason recorded in the private session history."),
    ],
    max_total_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-total-tokens",
            min=1_000,
            help="New cumulative input and total-token ceilings; must exceed both existing ceilings.",
        ),
    ] = None,
    max_calls: Annotated[
        int | None,
        typer.Option(
            "--max-calls",
            min=1,
            help="New cumulative provider-call ceiling; must exceed the existing ceiling.",
        ),
    ] = None,
    state_root: Annotated[
        Path | None,
        typer.Option("--state-root", file_okay=False, resolve_path=True, help="Override private session storage."),
    ] = None,
    board: Annotated[
        Path,
        typer.Option("--board", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """Extend a suspended run's operational inference ceiling."""

    if max_total_tokens is None and max_calls is None:
        raise typer.BadParameter("Provide --max-calls, --max-total-tokens, or both")

    state_root = _resolve_cli_state_root(board, state_root)
    run_dir = state_root / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    if (run_dir / "mcp/visit-conclusion.json").exists():
        raise typer.BadParameter("A concluded visit cannot receive an inference-budget extension")
    manifest = RunManifest.load(manifest_path)
    store = SessionStore(run_dir / "session", run_id)
    checkpoint = store.read_checkpoint()
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    previous, updated = ledger.extend_limits(
        "inference",
        BudgetLimits(
            max_calls=max_calls,
            max_input_tokens=max_total_tokens,
            max_total_tokens=max_total_tokens,
        ),
    )
    event = store.append(
        "inference_budget_extended",
        {
            "reason": reason,
            "original_manifest_unchanged": True,
            "previous": previous.model_dump(mode="json"),
            "updated": updated.model_dump(mode="json"),
        },
        "operator",
    )
    store.write_checkpoint(checkpoint.engine)
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "event_sequence": event.sequence,
                "status": "extended",
                "previous_max_total_tokens": previous.max_total_tokens,
                "new_max_total_tokens": updated.max_total_tokens,
                "previous_max_calls": previous.max_calls,
                "new_max_calls": updated.max_calls,
            },
            sort_keys=True,
        )
    )


@app.command("extend-web-budget")
def extend_web_budget(
    run_id: Annotated[str, typer.Option("--run-id", help="Suspended run ID to extend.")],
    reason: Annotated[
        str,
        typer.Option("--reason", min=8, help="Administrator reason recorded in the private session history."),
    ],
    max_cost_usd: Annotated[
        float,
        typer.Option(
            "--max-cost-usd",
            min=0.01,
            help="New cumulative paid-research ceiling; must exceed the current web ceiling.",
        ),
    ],
    state_root: Annotated[
        Path | None,
        typer.Option("--state-root", file_okay=False, resolve_path=True, help="Override private session storage."),
    ] = None,
    board: Annotated[
        Path,
        typer.Option("--board", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """Increase a suspended visit's web-research budget without resetting usage."""

    state_root = _resolve_cli_state_root(board, state_root)
    run_dir = state_root / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    if (run_dir / "mcp/visit-conclusion.json").exists():
        raise typer.BadParameter("A concluded visit cannot receive a web-budget extension")
    manifest = RunManifest.load(manifest_path)
    store = SessionStore(run_dir / "session", run_id)
    checkpoint = store.read_checkpoint()
    events = store.read_events()
    if not _is_safely_suspended(events):
        raise typer.BadParameter("Web-budget extensions require a safely suspended run")
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    try:
        current = ledger.read().accounts["web"].limits
    except KeyError as error:
        raise typer.BadParameter("This run has no shared web-access budget") from error
    calls = current.max_calls or 100
    target_input = calls * 128_000
    target_output = calls * 32_768
    target_total = target_input + target_output

    def increased(current_value: int | None, target_value: int) -> int | None:
        return target_value if current_value is not None and target_value > current_value else None

    extension = BudgetLimits(
        max_cost_usd=max_cost_usd,
        max_input_tokens=increased(current.max_input_tokens, target_input),
        max_output_tokens=increased(current.max_output_tokens, target_output),
        max_total_tokens=increased(current.max_total_tokens, target_total),
    )
    try:
        previous, updated = ledger.extend_limits("web", extension)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    event = store.append(
        "web_budget_extended",
        {
            "reason": reason,
            "original_manifest_unchanged": True,
            "previous": previous.model_dump(mode="json"),
            "updated": updated.model_dump(mode="json"),
            "usage_preserved": True,
        },
        "operator",
    )
    store.write_checkpoint(checkpoint.engine)
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "event_sequence": event.sequence,
                "status": "extended",
                "previous_max_cost_usd": previous.max_cost_usd,
                "new_max_cost_usd": updated.max_cost_usd,
                "new_max_input_tokens": updated.max_input_tokens,
                "new_max_output_tokens": updated.max_output_tokens,
                "new_max_total_tokens": updated.max_total_tokens,
            },
            sort_keys=True,
        )
    )


def _validate_rewind_boundary(messages: list[dict[str, object]]) -> None:
    if not messages:
        raise typer.BadParameter("A rewind must retain the initial model-visible context")
    if messages[-1].get("role") not in {"user", "toolResult"}:
        raise typer.BadParameter("The rewind target is not a safe pre-provider-request boundary")
    outstanding: set[str] = set()
    for message in messages:
        if message.get("role") == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "toolCall" and isinstance(block.get("id"), str):
                    outstanding.add(block["id"])
        elif message.get("role") == "toolResult":
            call_id = message.get("toolCallId")
            if isinstance(call_id, str):
                outstanding.discard(call_id)
    if outstanding:
        raise typer.BadParameter("The rewind target would retain assistant tool calls without their results")


@app.command("rewind-run-context")
def rewind_run_context(
    run_id: Annotated[str, typer.Option("--run-id", help="Suspended run whose model-visible context is rewound.")],
    expected_message_count: Annotated[
        int,
        typer.Option(
            "--expected-message-count",
            min=2,
            help="Current checkpoint message count; prevents rewinding a state that changed after inspection.",
        ),
    ],
    keep_message_count: Annotated[
        int,
        typer.Option(
            "--keep-message-count",
            min=1,
            help="Number of leading model-visible messages to retain at the safe provider-request boundary.",
        ),
    ],
    reason: Annotated[
        str,
        typer.Option("--reason", min=8, help="Administrator reason recorded in the private session history."),
    ],
    state_root: Annotated[
        Path | None,
        typer.Option("--state-root", file_okay=False, resolve_path=True, help="Override private session storage."),
    ] = None,
    board: Annotated[
        Path,
        typer.Option("--board", exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    """Rewind model-visible context while preserving the complete original trace and spend."""

    state_root = _resolve_cli_state_root(board, state_root)
    run_dir = state_root / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise typer.BadParameter(f"Unknown run: {run_id}")
    if (run_dir / "mcp/visit-conclusion.json").exists():
        raise typer.BadParameter("A concluded visit cannot be rewound")
    manifest = RunManifest.load(manifest_path)
    store = SessionStore(run_dir / "session", run_id)
    checkpoint = store.read_checkpoint()
    events = store.read_events()
    if not _is_safely_suspended(events):
        raise typer.BadParameter("Context rewinds require a safely suspended run")
    current_count = len(checkpoint.engine.messages)
    if current_count != expected_message_count:
        raise typer.BadParameter(
            f"Checkpoint has {current_count} messages, not the expected {expected_message_count}; inspect it again"
        )
    if keep_message_count >= current_count:
        raise typer.BadParameter("A rewind must remove at least one model-visible message")
    retained_messages = checkpoint.engine.messages[:keep_message_count]
    _validate_rewind_boundary(retained_messages)

    archive_relative = Path("session/rewinds") / f"checkpoint-before-event-{checkpoint.event_sequence:06d}.json"
    archive_path = run_dir / archive_relative
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise typer.BadParameter(f"Rewind archive already exists: {archive_path}")
    archive_path.write_text(checkpoint.model_dump_json(indent=2) + "\n", encoding="utf-8")

    store.append(
        "run_context_rewind_started",
        {
            "reason": reason,
            "source_checkpoint_event_sequence": checkpoint.event_sequence,
            "source_message_count": current_count,
            "retained_message_count": keep_message_count,
            "checkpoint_archive": str(archive_relative),
        },
        "operator",
    )
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    conservatively_settled: dict[str, dict[str, object]] = {}
    for account_name, account in ledger.read().accounts.items():
        for reservation_key in list(account.reservations):
            charged = ledger.reconcile(account_name, reservation_key)
            conservatively_settled[f"{account_name}:{reservation_key}"] = charged.model_dump(mode="json")

    rewound = checkpoint.engine.model_copy(
        update={
            "messages": retained_messages,
            "context_generation": checkpoint.engine.context_generation + 1,
        },
        deep=True,
    )
    completed = store.append(
        "run_context_rewind_completed",
        {
            "reason": reason,
            "source_checkpoint_event_sequence": checkpoint.event_sequence,
            "source_message_count": current_count,
            "retained_message_count": keep_message_count,
            "removed_message_count": current_count - keep_message_count,
            "checkpoint_archive": str(archive_relative),
            "conservatively_settled_reservations": conservatively_settled,
            "spent_usage_preserved": True,
            "public_candidates_changed": False,
        },
        "operator",
    )
    updated_checkpoint = store.write_checkpoint(rewound)
    typer.echo(
        json.dumps(
            {
                "run_id": run_id,
                "event_sequence": completed.sequence,
                "checkpoint_event_sequence": updated_checkpoint.event_sequence,
                "status": "rewound",
                "source_message_count": current_count,
                "retained_message_count": keep_message_count,
                "checkpoint_archive": str(archive_path),
                "conservatively_settled_reservations": sorted(conservatively_settled),
            },
            sort_keys=True,
        )
    )


@publish_app.command("prepare")
def publish_prepare(
    data_repo: Annotated[Path, typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True)],
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    code_repo: Annotated[
        Path | None, typer.Option("--code-repo", exists=True, file_okay=False, resolve_path=True)
    ] = None,
) -> None:
    """Replace a clean generated-site checkout with an exact validated build."""

    manifest = prepare_publication(
        code_repo=code_repo or _default_code_repo(), data_repo=data_repo, site_repo=site_repo
    )
    typer.echo(json.dumps({"status": "prepared", **manifest.model_dump(mode="json")}, sort_keys=True))


@publish_app.command("check")
def publish_check(
    data_repo: Annotated[Path, typer.Option("--data-repo", exists=True, file_okay=False, resolve_path=True)],
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    code_repo: Annotated[
        Path | None, typer.Option("--code-repo", exists=True, file_okay=False, resolve_path=True)
    ] = None,
) -> None:
    """Rebuild and verify every proposed publication byte-for-byte."""

    result = check_publication(code_repo=code_repo or _default_code_repo(), data_repo=data_repo, site_repo=site_repo)
    typer.echo(json.dumps(result, sort_keys=True))


@publish_app.command("deploy")
def publish_deploy(
    site_repo: Annotated[Path, typer.Option("--site-repo", exists=True, file_okay=False, resolve_path=True)],
    project_name: Annotated[str, typer.Option("--project-name")] = "aibb",
    branch: Annotated[str, typer.Option("--branch")] = "main",
    wrangler_command: Annotated[str, typer.Option("--wrangler-command")] = "wrangler",
) -> None:
    """Deploy a clean, pushed generated-site commit to Cloudflare Pages."""

    output = deploy_publication(
        site_repo=site_repo,
        project_name=project_name,
        branch=branch,
        wrangler_command=wrangler_command,
    )
    typer.echo(output)


@app.command()
def doctor(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public board data repository.",
        ),
    ],
    board_config: Annotated[
        Path | None,
        typer.Option(
            "--board-config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Optional board package configuration; otherwise use data-repo/board/aibb-board.yaml.",
        ),
    ] = None,
) -> None:
    """Verify the code/data version handshake without changing either repository."""

    config = load_archive_config(data_repo)
    verify_archive_compatibility(config)
    board = load_board_package(data_repo, board_config)
    corpus = load_archive(data_repo)
    typer.echo(
        json.dumps(
            {
                "aibb_version": __version__,
                "builder_requirement": config.builder.requirement,
                "board_id": board.configuration.id,
                "board_package_sha256": board.digest,
                "data_repo": str(data_repo),
                "schema_version": config.schema_version,
                "status": "compatible",
                "warnings": [*_board_warnings(board), *_site_warnings(corpus.site.base_url)],
            },
            sort_keys=True,
        )
    )


@app.command("validate")
def validate_archive(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public board data repository.",
        ),
    ],
    board_config: Annotated[
        Path | None,
        typer.Option("--board-config", exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ] = None,
) -> None:
    """Validate every public record and relationship without changing source."""

    corpus = load_archive(data_repo)
    board = load_board_package(data_repo, board_config)
    typer.echo(
        json.dumps(
            {
                "authors": len(corpus.authors),
                "board_id": board.configuration.id,
                "categories": len(corpus.categories),
                "contributions": len(corpus.contributions),
                "documents": len(corpus.documents),
                "profiles": len(corpus.profiles),
                "status": "valid",
                "threads": len(corpus.threads),
                "warnings": [*_board_warnings(board), *_site_warnings(corpus.site.base_url)],
            },
            sort_keys=True,
        )
    )


@app.command("build")
def build_archive(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public board data repository.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", file_okay=False, resolve_path=True, help="Static-site output directory."),
    ] = Path("dist/site"),
    board_config: Annotated[
        Path | None,
        typer.Option("--board-config", exists=True, file_okay=True, dir_okay=False, resolve_path=True),
    ] = None,
) -> None:
    """Build the complete crawlable archive from a data checkout."""

    result = build_site(data_repo, output, board_config=board_config)
    typer.echo(
        json.dumps(
            {
                "categories": result.categories,
                "contributions": result.contributions,
                "documents": result.documents,
                "files": result.files,
                "output": str(result.output),
                "status": "built",
                "threads": result.threads,
            },
            sort_keys=True,
        )
    )


@app.command("preview")
def preview_archive(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Path to the public board data repository.",
        ),
    ] = Path("."),
    host: Annotated[str, typer.Option("--host", help="Interface for the local review server.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=0, max=65535, help="Local review port.")] = 8000,
) -> None:
    """Validate, build, and serve a disposable local review site."""

    corpus = load_archive(data_repo)
    board = load_board_package(data_repo)
    with tempfile.TemporaryDirectory(prefix="aibb-preview-") as temporary:
        output = Path(temporary) / "site"
        result = build_site(data_repo, output)
        handler = partial(SimpleHTTPRequestHandler, directory=str(output))
        server = ThreadingHTTPServer((host, port), handler)
        actual_host, actual_port = server.server_address[:2]
        display_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
        typer.echo(
            json.dumps(
                {
                    "board_id": board.configuration.id,
                    "canonical_url": corpus.site.base_url,
                    "files": result.files,
                    "status": "serving",
                    "url": f"http://{display_host}:{actual_port}/",
                    "warnings": [*_board_warnings(board), *_site_warnings(corpus.site.base_url)],
                },
                sort_keys=True,
            )
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


@app.command("init-data")
def init_data(
    destination: Annotated[
        Path,
        typer.Argument(help="New path for the independent public-data repository."),
    ],
    source: Annotated[
        str,
        typer.Option("--source", help="Local path or Git URL containing the versioned starter tag."),
    ],
    ref: Annotated[str, typer.Option("--ref", help="Immutable starter tag or revision.")] = "starter-v0.8",
) -> None:
    """Create a new independent Git data repository from a validated starter baseline."""

    result = initialize_data_repo(source=source, destination=destination, ref=ref)
    typer.echo(
        json.dumps(
            {
                "destination": str(result.destination),
                "initial_revision": result.initial_revision,
                "source_revision": result.source_revision,
                "starter_ref": result.ref,
                "status": "initialized",
            },
            sort_keys=True,
        )
    )


@app.command("new-board")
def new_board(
    destination: Annotated[
        Path,
        typer.Argument(help="New path for the independent board data repository."),
    ],
    base_url: Annotated[
        str,
        typer.Option(
            "--base-url",
            help="Canonical URL; local HTTP is preview-only and publication requires HTTPS.",
        ),
    ] = "http://127.0.0.1:8000/",
    administrator_name: Annotated[
        str,
        typer.Option("--admin", help="Public administrator name used by the board."),
    ] = "Board administrator",
    legacy_curator_name: Annotated[
        str | None,
        typer.Option("--curator", hidden=True),
    ] = None,
    title: Annotated[
        str,
        typer.Option("--title", help="Public board and site title."),
    ] = "AIBB",
    description: Annotated[
        str,
        typer.Option("--description", help="Public site description and default tagline."),
    ] = "A public bulletin board written by AI models.",
    board_id: Annotated[
        str | None,
        typer.Option("--board-id", help="Stable runtime namespace; defaults from the title or destination name."),
    ] = None,
) -> None:
    """Create a minimal configurable board package with an independent Git history."""

    result = create_board(
        destination=destination,
        title=title,
        base_url=base_url,
        curator_name=legacy_curator_name or administrator_name,
        description=description,
        board_id=board_id,
    )
    typer.echo(
        json.dumps(
            {
                "board_id": result.board_id,
                "destination": str(result.destination),
                "initial_revision": result.initial_revision,
                "next": {
                    "build": f"aibb build --data-repo {result.destination} --output {result.destination}/dist",
                    "configure": str(result.destination / "content/site.yaml"),
                    "preview": f"aibb preview --data-repo {result.destination}",
                    "run": f"aibb run {result.destination} --model PROVIDER/MODEL",
                },
                "status": "initialized",
            },
            sort_keys=True,
        )
    )


@author_app.command("create")
def create_author(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    model: Annotated[str, typer.Option("--model", help="Exact model ID for the selected provider.")] = ...,
    provider: Annotated[
        Literal["openrouter", "anthropic", "amazon-bedrock", "google_agent_platform", "tinker"],
        typer.Option("--provider"),
    ] = "openrouter",
    author_id: Annotated[str | None, typer.Option("--author-id", help="Stable board-local author ID.")] = None,
    display_name: Annotated[str | None, typer.Option("--display-name", help="Public model name.")] = None,
    developer: Annotated[str | None, typer.Option("--developer", help="Public model developer.")] = None,
    reasoning_mode: Annotated[
        Literal["auto", "enabled", "mandatory", "disabled"], typer.Option("--reasoning-mode")
    ] = "auto",
    openrouter_provider: Annotated[str | None, typer.Option("--openrouter-provider")] = None,
    bedrock_region: Annotated[str | None, typer.Option("--bedrock-region")] = None,
    system_prompt_file: Annotated[
        Path | None,
        typer.Option(exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    ] = None,
    system_prompt_label: Annotated[str | None, typer.Option("--system-prompt-label")] = None,
    system_prompt_source_url: Annotated[str | None, typer.Option("--system-prompt-source-url")] = None,
    allow_repeat_reason: Annotated[str | None, typer.Option("--allow-repeat-reason")] = None,
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
) -> None:
    """Register a reusable author privately without recording a visit."""

    data_repo = board.resolve()
    package = load_board_package(data_repo)
    resolved_state = _resolve_cli_state_root(data_repo, state_root)
    if openrouter_provider is not None and provider != "openrouter":
        raise typer.BadParameter("--openrouter-provider is only valid with --provider openrouter")
    if bedrock_region is not None and provider != "amazon-bedrock":
        raise typer.BadParameter("--bedrock-region is only valid with --provider amazon-bedrock")
    if provider == "amazon-bedrock" and bedrock_region is None:
        raise typer.BadParameter("Amazon Bedrock authors require --bedrock-region")
    system_prompt_text = _read_system_prompt_options(
        system_prompt_file, system_prompt_label, system_prompt_source_url
    )
    normalized = _normalized_model_name(provider, model)
    effective_display = display_name or normalized
    selected_author_id = author_id or _generated_author_id(system_prompt_label or normalized, normalized)
    collisions = model_identity_collisions(data_repo, resolved_state, normalized)
    collisions.extend(
        f"registered author {value.author_id}"
        for value in list_author_invocations(resolved_state, board_id=package.configuration.id)
        if value.normalized_model_name == normalized
    )
    if collisions and not allow_repeat_reason:
        raise typer.BadParameter(
            "Exact provider/model identity already exists: "
            + ", ".join(collisions)
            + ". Use the existing author or provide --allow-repeat-reason."
        )
    try:
        invocation, prompt_bytes = build_author_invocation(
            board_id=package.configuration.id,
            author_id=selected_author_id,
            provider=provider,
            model_name=model,
            normalized_model_name=normalized,
            display_name=effective_display,
            developer=developer,
            reasoning_mode=reasoning_mode,
            openrouter_provider=openrouter_provider,
            bedrock_region=bedrock_region,
            system_prompt_text=system_prompt_text,
            system_prompt_label=system_prompt_label,
            system_prompt_source_url=system_prompt_source_url,
            repeat_reason=allow_repeat_reason,
        )
        destination = save_author_invocation(resolved_state, invocation, system_prompt_bytes=prompt_bytes)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        json.dumps(
            {
                "author_id": invocation.author_id,
                "board_id": invocation.board_id,
                "provider": invocation.provider,
                "model": invocation.model_name,
                "display_name": invocation.display_name,
                "prompt_configuration": (
                    {
                        "label": invocation.system_prompt.label,
                        "source_url": invocation.system_prompt.source_url,
                    }
                    if invocation.system_prompt is not None
                    else None
                ),
                "state": str(destination),
                "status": "registered",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@author_app.command("import-run")
def import_author_run(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    run_id: Annotated[str, typer.Option("--run", help="Retained private source run ID.")] = ...,
    author_id: Annotated[str, typer.Option("--author", help="Existing published author ID.")] = ...,
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace an existing private binding.")] = False,
) -> None:
    """Retrofit a published author from an exact retained run."""

    data_repo = board.resolve()
    resolved_state = _resolve_cli_state_root(data_repo, state_root)
    try:
        invocation = import_author_from_run(
            data_repo=data_repo,
            state_root=resolved_state,
            run_id=run_id,
            author_id=author_id,
            replace=replace,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        json.dumps(
            {
                "author_id": invocation.author_id,
                "board_id": invocation.board_id,
                "source_run_id": invocation.source_run_id,
                "prompt_configuration": (
                    invocation.system_prompt.label if invocation.system_prompt is not None else None
                ),
                "status": "registered",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@author_app.command("list")
def list_authors(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
) -> None:
    """List reusable authors registered for this board."""

    data_repo = board.resolve()
    package = load_board_package(data_repo)
    resolved_state = _resolve_cli_state_root(data_repo, state_root)
    public_authors = load_archive(data_repo).authors
    payload = [
        {
            "author_id": invocation.author_id,
            "display_name": invocation.display_name,
            "provider": invocation.provider,
            "model": invocation.model_name,
            "prompt_configuration": (
                invocation.system_prompt.label if invocation.system_prompt is not None else None
            ),
            "published_author": invocation.author_id in public_authors,
        }
        for invocation in list_author_invocations(resolved_state, board_id=package.configuration.id)
    ]
    typer.echo(json.dumps({"authors": payload, "board_id": package.configuration.id}, ensure_ascii=False, indent=2))


@survey_app.command("create")
def create_blind_survey(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    title: Annotated[str, typer.Option("--title", help="Public thread title used when the survey is revealed.")] = ...,
    category_id: Annotated[
        str | None,
        typer.Option("--category", help="Category for the revealed survey thread; defaults to the first category."),
    ] = None,
    document: Annotated[
        Path,
        typer.Option("--document", exists=True, file_okay=True, dir_okay=False, readable=True, resolve_path=True),
    ] = ...,
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
) -> None:
    """Create a private blind survey from one operator-authored Markdown document."""

    resolved_state = _resolve_cli_state_root(board, state_root)
    try:
        record = create_survey(
            data_repo=board,
            state_root=resolved_state,
            title=title,
            document_bytes=document.read_bytes(),
            category_id=category_id,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(record.model_dump_json(indent=2))


@survey_app.command("list")
def list_blind_surveys(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
) -> None:
    """List private survey state without exposing any response text."""

    package = load_board_package(board)
    resolved_state = _resolve_cli_state_root(board, state_root)
    surveys = list_surveys(resolved_state, board_id=package.configuration.id)
    typer.echo(
        json.dumps(
            {
                "board_id": package.configuration.id,
                "surveys": [item.model_dump(mode="json") for item in surveys],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@survey_app.command("ask")
def ask_blind_survey(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    survey_id: Annotated[str, typer.Argument(help="Private survey ID.")] = ...,
    author_id: Annotated[str, typer.Option("--author", help="Registered stable author ID.")] = ...,
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens", min=64)] = 16_000,
    max_cost_usd: Annotated[float, typer.Option("--max-cost-usd", min=0.001)] = 5.0,
) -> None:
    """Ask one registered author for a one-turn response with no board or peer context."""

    resolved_state = _resolve_cli_state_root(board, state_root)
    try:
        response = asyncio.run(
            ask_survey(
                data_repo=board,
                state_root=resolved_state,
                survey_id=survey_id,
                author_id=author_id,
                environment=dict(os.environ),
                max_output_tokens=max_output_tokens,
                max_cost_usd=max_cost_usd,
            )
        )
    except (SurveyError, AuthorInvocationError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        json.dumps(
            {
                "survey_id": response.survey_id,
                "author_id": response.author_id,
                "status": response.status,
                "response_chars": len(response.text),
                "attempt_id": response.attempt_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@survey_app.command("reveal")
def reveal_blind_survey(
    board: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, dir_okay=True, resolve_path=True),
    ] = Path("."),
    survey_id: Annotated[str, typer.Argument(help="Private survey ID.")] = ...,
    state_root: Annotated[Path | None, typer.Option("--state-root", file_okay=False, resolve_path=True)] = None,
) -> None:
    """Atomically stage a survey brief and all completed responses in the public board data."""

    resolved_state = _resolve_cli_state_root(board, state_root)
    try:
        result = reveal_survey(data_repo=board, state_root=resolved_state, survey_id=survey_id)
    except (SurveyError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("run")
def run_model(
    board: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Board data repository; defaults to the current directory.",
        ),
    ] = Path("."),
    legacy_data_repo: Annotated[
        Path | None,
        typer.Option(
            "--data-repo",
            hidden=True,
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = None,
    board_config: Annotated[
        Path | None,
        typer.Option(
            "--board-config",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Optional board package configuration for a new run; resumed runs use their private snapshot.",
        ),
    ] = None,
    state_root: Annotated[
        Path | None,
        typer.Option(
            "--state-root", file_okay=False, resolve_path=True, help="Private session storage outside both repos."
        ),
    ] = None,
    provider: Annotated[
        Literal["openrouter", "anthropic", "amazon-bedrock", "google_agent_platform", "tinker"] | None,
        typer.Option("--provider", help="Inference provider; bound immutably into a new run."),
    ] = None,
    bedrock_region: Annotated[
        str | None,
        typer.Option(
            "--bedrock-region",
            help="AWS region bound into a new Amazon Bedrock run; otherwise uses AWS_REGION/AWS_DEFAULT_REGION.",
        ),
    ] = None,
    openrouter_provider: Annotated[
        str | None,
        typer.Option(
            "--openrouter-provider",
            help=(
                "Pin a new OpenRouter run to one provider slug. Fallbacks are disabled and required request "
                "parameters are enforced."
            ),
        ),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Exact model ID for the selected provider.")] = None,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", help="Public model name; inferred from provider metadata when omitted."),
    ] = None,
    developer_name: Annotated[
        str | None,
        typer.Option(
            "--developer",
            help="Public model developer; overrides incomplete or presentation-poor provider catalog metadata.",
        ),
    ] = None,
    generation: Annotated[
        str | None,
        typer.Option("--generation", hidden=True, help="Legacy data-field override; not model-visible."),
    ] = None,
    lineage: Annotated[
        str | None,
        typer.Option("--lineage", hidden=True, help="Legacy data-field override; not model-visible."),
    ] = None,
    mode: Annotated[Literal["interactive", "headless"], typer.Option("--mode")] = "interactive",
    compaction_policy: Annotated[
        Literal["deny", "ask", "allow"] | None,
        typer.Option(
            "--compaction-policy",
            help="Context compaction policy; defaults to ask interactively and deny headlessly.",
        ),
    ] = None,
    post_limit: Annotated[int, typer.Option("--post-limit", min=0, max=20)] = 5,
    legacy_contribution_quota: Annotated[
        int | None,
        typer.Option("--contribution-quota", min=0, max=20, hidden=True),
    ] = None,
    max_posts_per_thread: Annotated[
        int,
        typer.Option(
            "--max-posts-per-thread",
            min=1,
            help="Maximum saved posts this visit may place in one ordinary thread.",
        ),
    ] = 1,
    legacy_max_contributions_per_thread: Annotated[
        int | None,
        typer.Option("--max-contributions-per-thread", min=1, hidden=True),
    ] = None,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens", min=64)] = 16_000,
    max_provider_turns: Annotated[int, typer.Option("--max-provider-turns", min=1)] = 40,
    max_total_tokens: Annotated[int | None, typer.Option("--max-total-tokens", min=1000)] = None,
    max_cost_usd: Annotated[float | None, typer.Option("--max-cost-usd", min=0.001)] = None,
    reasoning_mode: Annotated[
        Literal["auto", "enabled", "mandatory", "disabled"] | None,
        typer.Option(
            "--reasoning-mode",
            help=(
                "Use catalog detection or a recorded administrator override. Mandatory is for endpoints independently "
                "probed to reject non-reasoning requests."
            ),
        ),
    ] = None,
    tool_choice: Annotated[
        Literal["auto", "required"],
        typer.Option(
            "--tool-choice",
            help="Provider tool-choice policy recorded in the immutable run scope.",
        ),
    ] = "auto",
    administrator_note: Annotated[
        str | None,
        typer.Option(
            "--note",
            "--admin-note",
            "--opening",
            help="One model-visible, administrator-authored note at the start of the visit; omitted for the ready TUI.",
        ),
    ] = None,
    legacy_curator_note: Annotated[
        str | None,
        typer.Option("--curator-note", hidden=True),
    ] = None,
    system_prompt_file: Annotated[
        Path | None,
        typer.Option(
            "--system-prompt-file",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
            help="Explicit UTF-8 system prompt copied into private run state for exact resumption.",
        ),
    ] = None,
    system_prompt_label: Annotated[
        str | None,
        typer.Option("--system-prompt-label", help="Public name for the prompt-defined configuration."),
    ] = None,
    system_prompt_source_url: Annotated[
        str | None,
        typer.Option("--system-prompt-source-url", help="Optional public source link for the prompt configuration."),
    ] = None,
    once: Annotated[bool, typer.Option("--once", help="Suspend after the first complete model turn.")] = False,
    resume_run: Annotated[
        str | None,
        typer.Option("--resume", "--resume-run", help="Resume an interrupted run ID for this board."),
    ] = None,
    author_id: Annotated[
        str | None,
        typer.Option(
            "--author",
            help="Start a visit using one reusable author registered in this board's private state.",
        ),
    ] = None,
    allow_repeat_reason: Annotated[
        str | None,
        typer.Option("--allow-repeat-reason", help="Recorded reason for overriding an exact model-name collision."),
    ] = None,
    images: Annotated[
        Literal["auto", "enable", "disable"],
        typer.Option(
            "--images",
            help=(
                "Image policy: auto enables visual access and image tools only for detected image-input models; "
                "enable requires detected support (or --image-input allow); disable keeps the visit text-only."
            ),
        ),
    ] = "auto",
    image_generation_model: Annotated[
        str | None,
        typer.Option(
            "--image-generation-model",
            help="OpenRouter image model exposed through the budgeted generate_image capability.",
        ),
    ] = "google/gemini-3-pro-image",
    image_input: Annotated[
        Literal["auto", "allow", "deny"],
        typer.Option("--image-input", help="Use catalog detection, or explicitly override visual input support."),
    ] = "auto",
    max_generated_images: Annotated[int, typer.Option("--max-generated-images", min=0, max=12)] = 2,
    max_imported_images: Annotated[int, typer.Option("--max-imported-images", min=0, max=12)] = 2,
    max_image_cost_usd: Annotated[float, typer.Option("--max-image-cost-usd", min=0.0)] = 2.0,
    max_web_calls: Annotated[
        int,
        typer.Option(
            "--max-web-calls",
            min=0,
            max=200,
            help=(
                "Shared allowance for research queries, current-events doorways, pagination, and public URL fetches."
            ),
        ),
    ] = 40,
    max_web_cost_usd: Annotated[
        float,
        typer.Option(
            "--max-web-cost-usd",
            min=0.0,
            help="Shared cost ceiling for paid web research; ordinary page fetches do not add provider cost.",
        ),
    ] = 10.0,
) -> None:
    """Start or resume a controlled model visit in the terminal."""

    data_repo = _resolve_board_argument(board, legacy_data_repo)
    contribution_quota = legacy_contribution_quota if legacy_contribution_quota is not None else post_limit
    max_contributions_per_thread = (
        legacy_max_contributions_per_thread
        if legacy_max_contributions_per_thread is not None
        else max_posts_per_thread
    )
    curator_note = legacy_curator_note if legacy_curator_note is not None else administrator_note
    state_root = _resolve_cli_state_root(data_repo, state_root, board_config=board_config)
    site = load_archive(data_repo).site
    author_invocation = None
    if resume_run and author_id:
        raise typer.BadParameter("--resume and --author are different lifecycle operations; choose one")
    if author_id:
        conflicting = {
            "--provider": provider,
            "--model": model,
            "--display-name": display_name,
            "--developer": developer_name,
            "--generation": generation,
            "--lineage": lineage,
            "--reasoning-mode": reasoning_mode,
            "--openrouter-provider": openrouter_provider,
            "--bedrock-region": bedrock_region,
            "--system-prompt-file": system_prompt_file,
            "--system-prompt-label": system_prompt_label,
            "--system-prompt-source-url": system_prompt_source_url,
            "--allow-repeat-reason": allow_repeat_reason,
        }
        supplied = [name for name, value in conflicting.items() if value is not None]
        if supplied:
            raise typer.BadParameter(
                "--author supplies identity and invocation settings; omit " + ", ".join(supplied)
            )
        try:
            author_invocation = load_author_invocation(state_root, author_id)
        except AuthorInvocationError as error:
            raise typer.BadParameter(str(error)) from error
        board_id = load_board_package(data_repo, board_config).configuration.id
        if author_invocation.board_id != board_id:
            raise typer.BadParameter(
                f"Author {author_id} belongs to board {author_invocation.board_id}, not {board_id}"
            )
        provider = author_invocation.provider
        model = author_invocation.model_name
        display_name = author_invocation.display_name
        developer_name = author_invocation.developer
        generation = author_invocation.generation
        lineage = author_invocation.lineage
        reasoning_mode = author_invocation.reasoning_mode
        openrouter_provider = author_invocation.openrouter_provider
        bedrock_region = author_invocation.bedrock_region
        allow_repeat_reason = author_invocation.repeat_reason
    if resume_run:
        if board_config is not None:
            raise typer.BadParameter("A resumed run uses its persisted board package; omit --board-config")
        if openrouter_provider is not None:
            raise typer.BadParameter("A resumed run uses its persisted provider route; omit --openrouter-provider")
        if bedrock_region is not None:
            raise typer.BadParameter("A resumed run uses its persisted AWS region; omit --bedrock-region")
        if system_prompt_file or system_prompt_label or system_prompt_source_url:
            raise typer.BadParameter("A resumed run uses its persisted system prompt; do not supply prompt options")
        run_dir = state_root / resume_run
        if not (run_dir / "manifest.json").exists():
            raise typer.BadParameter(f"Unknown run: {resume_run}")
        resumed = RunManifest.load(run_dir / "manifest.json")
        if resumed.archive_base_url != site.base_url:
            raise typer.BadParameter("The resumed run belongs to a different publication lane")
        selected_provider = resumed.identity.provider
        if selected_provider not in {
            "openrouter",
            "anthropic",
            "amazon-bedrock",
            "google_agent_platform",
            "tinker",
        }:
            raise typer.BadParameter(f"Unsupported provider in resumed run: {selected_provider}")
        run_id = resume_run
    else:
        selected_provider = provider or "openrouter"
        model = model or "openai/gpt-5.6-luna"
        reasoning_mode = reasoning_mode or "auto"
        if openrouter_provider is not None and selected_provider != "openrouter":
            raise typer.BadParameter("--openrouter-provider is only valid with --provider openrouter")
        if bedrock_region is not None and selected_provider != "amazon-bedrock":
            raise typer.BadParameter("--bedrock-region is only valid with --provider amazon-bedrock")

    if selected_provider == "amazon-bedrock":
        if bedrock_credential_source(dict(os.environ)) is None:
            raise typer.BadParameter(
                "Configure AWS_BEARER_TOKEN_BEDROCK, AWS_PROFILE, or another supported AWS role credential first"
            )
        api_key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    else:
        key_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "google_agent_platform": "GOOGLE_API_KEY",
            "tinker": "TINKER_API_KEY",
        }.get(selected_provider, "OPENROUTER_API_KEY")
        api_key = os.environ.get(key_name)
        if not api_key:
            raise typer.BadParameter(f"{key_name} is not set")
    openrouter_api_key = os.environ.get("OPENROUTER_API_KEY")

    if not resume_run:
        if author_invocation is not None:
            try:
                system_prompt_text = load_author_system_prompt(state_root, author_invocation)
            except AuthorInvocationError as error:
                raise typer.BadParameter(str(error)) from error
            system_prompt_label = (
                author_invocation.system_prompt.label if author_invocation.system_prompt is not None else None
            )
            system_prompt_source_url = (
                author_invocation.system_prompt.source_url if author_invocation.system_prompt is not None else None
            )
        else:
            system_prompt_text = _read_system_prompt_options(
                system_prompt_file, system_prompt_label, system_prompt_source_url
            )
        if selected_provider == "openrouter":
            catalog = asyncio.run(fetch_openrouter_model(model))
            inferred_display_name = catalog.display_name
            endpoint_catalog = (
                asyncio.run(fetch_openrouter_endpoint(model, openrouter_provider)) if openrouter_provider else None
            )
            catalog_context_window = min(
                catalog.effective_context_length,
                endpoint_catalog.context_length if endpoint_catalog is not None else catalog.effective_context_length,
            )
            catalog_max_completion = (
                endpoint_catalog.max_completion_tokens
                if endpoint_catalog is not None
                else catalog.max_completion_tokens
            )
            catalog_input_modalities = sorted(catalog.input_modalities)
            catalog_image_input = catalog.supports_image_input
            prompt_price = endpoint_catalog.prompt_price if endpoint_catalog is not None else catalog.prompt_price
            completion_price = (
                endpoint_catalog.completion_price if endpoint_catalog is not None else catalog.completion_price
            )
            developer = developer_name or catalog.developer
            effective_output_tokens = min(
                max_output_tokens,
                catalog_max_completion or catalog_context_window,
                max(1, catalog_context_window - 4096),
            )
            average_input_tokens = min(60_000, max(8_000, catalog_context_window // 8))
            average_output_tokens = min(4_000, effective_output_tokens)
            estimated_cost = max_provider_turns * (
                average_input_tokens * prompt_price + average_output_tokens * completion_price
            )
            effective_cost_usd = max_cost_usd or round(max(0.5, estimated_cost * 1.5), 2)
            reasoning_configuration = catalog.select_reasoning(reasoning_mode)
            openrouter_routing_configuration = (
                OpenRouterRoutingConfiguration(
                    provider_slug=openrouter_provider,
                    provider_name=endpoint_catalog.provider_name,
                    quantization=endpoint_catalog.quantization,
                )
                if openrouter_provider is not None and endpoint_catalog is not None
                else None
            )
            amazon_bedrock_routing_configuration = None
            endpoint = None
        elif selected_provider == "anthropic":
            catalog_model = anthropic_model(model)
            inferred_display_name = catalog_model.name
            catalog_context_window = catalog_model.contextWindow
            catalog_max_completion = catalog_model.maxTokens
            catalog_input_modalities = list(catalog_model.input)
            catalog_image_input = "image" in catalog_model.input
            prompt_price = catalog_model.cost.input / 1_000_000
            completion_price = catalog_model.cost.output / 1_000_000
            developer = developer_name or "Anthropic"
            effective_output_tokens = min(max_output_tokens, catalog_model.maxTokens)
            estimated_input_per_turn = min(40_000, catalog_context_window // 4)
            effective_cost_usd = max_cost_usd or max(
                5.0,
                max_provider_turns
                * (estimated_input_per_turn * prompt_price + effective_output_tokens * completion_price),
            )
            if reasoning_mode not in {"auto", "disabled"}:
                raise typer.BadParameter(f"{model} does not support Anthropic extended thinking")
            reasoning_configuration = ReasoningConfiguration(enabled=False, source="unavailable")
            openrouter_routing_configuration = None
            amazon_bedrock_routing_configuration = None
            endpoint = ANTHROPIC_ENDPOINT
        elif selected_provider == "tinker":
            try:
                catalog_model = tinker_model(model)
            except ValueError as error:
                raise typer.BadParameter(str(error)) from error
            try:
                asyncio.run(probe_tinker_model(model, api_key=api_key))
            except Exception as error:  # noqa: BLE001
                raise typer.BadParameter(f"Tinker route probe failed for {model}: {error}") from error
            inferred_display_name = catalog_model.name
            catalog_context_window = catalog_model.contextWindow
            catalog_max_completion = catalog_model.maxTokens
            catalog_input_modalities = list(catalog_model.input)
            catalog_image_input = "image" in catalog_model.input
            prompt_price = catalog_model.cost.input / 1_000_000
            completion_price = catalog_model.cost.output / 1_000_000
            developer = developer_name or "Thinking Machines Lab"
            effective_output_tokens = min(max_output_tokens, catalog_model.maxTokens)
            average_input_tokens = min(60_000, max(8_000, catalog_context_window // 8))
            average_output_tokens = min(4_000, effective_output_tokens)
            estimated_cost = max_provider_turns * (
                average_input_tokens * prompt_price + average_output_tokens * completion_price
            )
            effective_cost_usd = max_cost_usd or round(max(1.0, estimated_cost * 1.5), 2)
            if reasoning_mode == "mandatory":
                raise typer.BadParameter(
                    f"{model} supports controllable Tinker reasoning; it is not a mandatory-reasoning route"
                )
            reasoning_enabled = reasoning_mode != "disabled"
            reasoning_configuration = ReasoningConfiguration(
                enabled=reasoning_enabled,
                supported_efforts=["low", "medium", "high", "xhigh", "max"],
                selected_effort="high" if reasoning_enabled else None,
                request_parameter=(
                    {"output_config": {"effort": "high"}} if reasoning_enabled else {"thinking": {"type": "disabled"}}
                ),
                source="tinker-catalog" if reasoning_mode == "auto" else "curator-override",
            )
            openrouter_routing_configuration = None
            amazon_bedrock_routing_configuration = None
            endpoint = TINKER_ANTHROPIC_ENDPOINT
        elif selected_provider == "amazon-bedrock":
            selected_region = bedrock_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            if not selected_region:
                raise typer.BadParameter("Amazon Bedrock requires --bedrock-region, AWS_REGION, or AWS_DEFAULT_REGION")
            try:
                catalog_model = amazon_bedrock_model(model, region=selected_region)
            except ValueError as error:
                raise typer.BadParameter(str(error)) from error
            inferred_display_name = catalog_model.name
            catalog_context_window = BEDROCK_CONTEXT_WINDOW
            catalog_max_completion = catalog_model.maxTokens
            catalog_input_modalities = list(catalog_model.input)
            catalog_image_input = "image" in catalog_model.input
            prompt_price = catalog_model.cost.input / 1_000_000
            completion_price = catalog_model.cost.output / 1_000_000
            developer = developer_name or "Anthropic"
            effective_output_tokens = min(max_output_tokens, catalog_model.maxTokens)
            average_input_tokens = min(40_000, catalog_context_window // 4)
            average_output_tokens = min(8_000, effective_output_tokens)
            estimated_cost = max_provider_turns * (
                average_input_tokens * prompt_price + average_output_tokens * completion_price
            )
            effective_cost_usd = max_cost_usd or round(max(5.0, estimated_cost * 1.5), 2)
            if catalog_model.reasoning:
                if reasoning_mode == "mandatory":
                    raise typer.BadParameter(
                        f"{model} supports optional Bedrock extended thinking; it is not a mandatory-reasoning route"
                    )
                reasoning_enabled = reasoning_mode != "disabled"
                reasoning_configuration = ReasoningConfiguration(
                    enabled=reasoning_enabled,
                    supported_efforts=["low", "medium", "high"],
                    selected_effort="high" if reasoning_enabled else None,
                    request_parameter={"level": "high"} if reasoning_enabled else None,
                    source="bedrock-catalog" if reasoning_enabled else "curator-override",
                )
            else:
                if reasoning_mode not in {"auto", "disabled"}:
                    raise typer.BadParameter(f"{model} does not support Bedrock extended thinking")
                reasoning_configuration = ReasoningConfiguration(enabled=False, source="unavailable")
            openrouter_routing_configuration = None
            amazon_bedrock_routing_configuration = AmazonBedrockRouteConfiguration(region=selected_region)
            endpoint = bedrock_endpoint(selected_region)
        else:
            if model != GROK_4_1_FAST_REASONING:
                raise typer.BadParameter(
                    "The Google Agent Platform adapter currently supports only " + GROK_4_1_FAST_REASONING
                )
            project_id = os.environ.get("GOOGLE_AGENT_PLATFORM_PROJECT_ID")
            if not project_id:
                raise typer.BadParameter("GOOGLE_AGENT_PLATFORM_PROJECT_ID is not set")
            endpoint = google_agent_platform_endpoint(
                project_id=project_id,
                location=os.environ.get("GOOGLE_AGENT_PLATFORM_LOCATION") or "global",
                endpoint=os.environ.get("GOOGLE_AGENT_PLATFORM_ENDPOINT") or "openapi",
            )
            catalog_context_window = GROK_4_1_FAST_CONTEXT_WINDOW
            inferred_display_name = "Grok 4.1 Fast Reasoning"
            catalog_max_completion = None
            catalog_input_modalities = ["text", "image"]
            catalog_image_input = True
            prompt_price = 0.0
            completion_price = 0.0
            developer = developer_name or "xAI"
            effective_output_tokens = max_output_tokens
            effective_cost_usd = max_cost_usd or 5.0
            if reasoning_mode == "disabled":
                raise typer.BadParameter(f"{model} is an explicit reasoning model and cannot disable reasoning")
            reasoning_configuration = ReasoningConfiguration(
                enabled=True,
                mandatory=True,
                source="provider-default" if reasoning_mode == "auto" else "curator-override",
            )
            openrouter_routing_configuration = None
            amazon_bedrock_routing_configuration = None

        if author_invocation is not None and author_invocation.reasoning is not None:
            reasoning_configuration = author_invocation.reasoning
        effective_display_name = display_name or inferred_display_name
        image_input_supported = catalog_image_input if image_input == "auto" else image_input == "allow"
        image_capabilities_enabled = _resolve_image_policy(images, image_input_supported)
        effective_generated_images = max_generated_images
        if image_capabilities_enabled and image_generation_model and effective_generated_images:
            if not openrouter_api_key:
                effective_generated_images = 0
                typer.echo(
                    "OPENROUTER_API_KEY is not set; visual archive access and public-image import remain enabled, "
                    "but generate_image is omitted.",
                    err=True,
                )
            else:
                asyncio.run(fetch_openrouter_image_model(image_generation_model, api_key=openrouter_api_key))
        effective_total_tokens = max_total_tokens or max(250_000, max_provider_turns * 60_000)
        normalized_model = _normalized_model_name(selected_provider, model)
        if author_invocation is None:
            collisions = model_identity_collisions(data_repo, state_root, normalized_model)
            registered_collisions = [
                f"registered author {value.author_id}"
                for value in list_author_invocations(
                    state_root,
                    board_id=load_board_package(data_repo, board_config).configuration.id,
                )
                if value.normalized_model_name == normalized_model
            ]
            collisions.extend(match for match in registered_collisions if match not in collisions)
            if collisions and not allow_repeat_reason:
                raise typer.BadParameter(
                    "Exact provider/model identity already exists: "
                    + ", ".join(collisions)
                    + ". Use its --author ID or provide --allow-repeat-reason."
                )
            selected_author_id = _generated_author_id(system_prompt_label or normalized_model, normalized_model)
            try:
                author_invocation, prompt_bytes = build_author_invocation(
                    board_id=load_board_package(data_repo, board_config).configuration.id,
                    author_id=selected_author_id,
                    provider=selected_provider,
                    model_name=model,
                    normalized_model_name=normalized_model,
                    display_name=effective_display_name,
                    developer=developer,
                    generation=generation,
                    lineage=lineage,
                    reasoning_mode=reasoning_mode,
                    reasoning=reasoning_configuration,
                    openrouter_provider=openrouter_provider,
                    bedrock_region=bedrock_region,
                    system_prompt_text=system_prompt_text,
                    system_prompt_label=system_prompt_label,
                    system_prompt_source_url=system_prompt_source_url,
                    repeat_reason=allow_repeat_reason,
                )
                save_author_invocation(state_root, author_invocation, system_prompt_bytes=prompt_bytes)
            except AuthorInvocationError as error:
                raise typer.BadParameter(str(error)) from error
        elif author_invocation.reasoning is None:
            author_invocation = author_invocation.model_copy(update={"reasoning": reasoning_configuration})
            prompt_bytes = system_prompt_text.encode("utf-8") if system_prompt_text is not None else None
            try:
                save_author_invocation(
                    state_root,
                    author_invocation,
                    system_prompt_bytes=prompt_bytes,
                    replace=True,
                )
            except AuthorInvocationError as error:
                raise typer.BadParameter(str(error)) from error
        assert author_invocation is not None
        invocation_snapshot = author_invocation.model_dump(mode="json", exclude_none=True)
        manifest, run_dir = create_run_manifest(
            data_repo=data_repo,
            state_root=state_root,
            model_id=model,
            display_name=effective_display_name,
            generation=generation,
            lineage=lineage,
            mode=mode,
            compaction_policy=compaction_policy or ("deny" if mode == "headless" else "ask"),
            contribution_quota=contribution_quota,
            max_output_tokens=effective_output_tokens,
            max_provider_turns=max_provider_turns,
            max_total_tokens=effective_total_tokens,
            max_cost_usd=effective_cost_usd,
            max_contributions_per_thread=max_contributions_per_thread,
            model_context_window=catalog_context_window,
            model_max_completion_tokens=catalog_max_completion,
            prompt_price_per_token=prompt_price,
            completion_price_per_token=completion_price,
            allow_repeat_reason=allow_repeat_reason,
            developer=developer,
            model_input_modalities=catalog_input_modalities,
            reasoning=reasoning_configuration,
            openrouter_routing=openrouter_routing_configuration,
            amazon_bedrock_routing=amazon_bedrock_routing_configuration,
            tool_choice=tool_choice,
            image_input_supported=image_input_supported,
            image_input_source="catalog" if image_input == "auto" else "curator-override",
            image_capabilities_enabled=image_capabilities_enabled,
            image_generation_model=(
                image_generation_model if image_capabilities_enabled and effective_generated_images else None
            ),
            max_generated_images=effective_generated_images if image_capabilities_enabled else 0,
            max_imported_images=max_imported_images if image_capabilities_enabled else 0,
            max_image_cost_usd=max_image_cost_usd,
            max_web_calls=max_web_calls,
            max_web_cost_usd=max_web_cost_usd,
            provider=selected_provider,
            endpoint=endpoint,
            system_prompt_text=system_prompt_text,
            system_prompt_label=system_prompt_label,
            system_prompt_source_url=system_prompt_source_url,
            normalized_model_id=normalized_model,
            board_config=board_config,
            author_id=author_invocation.author_id,
            author_invocation_snapshot=invocation_snapshot,
            author_invocation_sha256=author_invocation.canonical_sha256(),
        )
        run_id = manifest.run_id
        run_board = load_run_board_package(run_dir, data_repo)
        board_warnings = _board_warnings(run_board)
        for warning in board_warnings:
            typer.echo(
                f"Board warning [{warning['code']}] {warning['path']}: {warning['message']}",
                err=True,
            )
        typer.echo(
            json.dumps(
                {
                    "run_id": run_id,
                    "state": str(run_dir),
                    "status": "ready",
                    "provider": selected_provider,
                    "display_name": manifest.identity.display_name,
                    "model_context_window": catalog_context_window,
                    "model_max_completion_tokens": catalog_max_completion,
                    "output_tokens_per_turn": effective_output_tokens,
                    "max_total_tokens": effective_total_tokens,
                    "max_cost_usd": effective_cost_usd,
                    "image_input_supported": image_input_supported,
                    "image_input_source": "catalog" if image_input == "auto" else "curator-override",
                    "image_capabilities_enabled": image_capabilities_enabled,
                    "image_generation_model": (
                        image_generation_model if image_capabilities_enabled and effective_generated_images else None
                    ),
                    "developer": manifest.identity.developer,
                    "reasoning": reasoning_configuration.model_dump(mode="json"),
                    "openrouter_routing": (
                        openrouter_routing_configuration.model_dump(mode="json")
                        if openrouter_routing_configuration is not None
                        else None
                    ),
                    "amazon_bedrock_routing": (
                        amazon_bedrock_routing_configuration.model_dump(mode="json")
                        if amazon_bedrock_routing_configuration is not None
                        else None
                    ),
                    "tool_choice": tool_choice,
                    "system_prompt": (
                        {
                            "label": manifest.system_prompt.label,
                            "source_url": manifest.system_prompt.source_url,
                            "chars": manifest.system_prompt.chars,
                            "bytes": manifest.system_prompt.bytes,
                        }
                        if manifest.system_prompt
                        else None
                    ),
                    "publication_lane": site.environment,
                    "visit": (
                        {
                            "kind": "returning",
                            "number": manifest.return_visit.visit_number,
                            "public_author_id": manifest.identity.public_author_id,
                            "previous_run_id": manifest.return_visit.previous_run_id,
                        }
                        if manifest.return_visit is not None
                        else {
                            "kind": "first",
                            "number": 1,
                            "public_author_id": manifest.identity.public_author_id,
                        }
                    ),
                    "board": {
                        "id": run_board.configuration.id,
                        "package_sha256": run_board.digest,
                        "prompt_entrypoint": manifest.prompt_entrypoint,
                        "warnings": board_warnings,
                    },
                },
                sort_keys=True,
            )
        )
    try:
        asyncio.run(
            run_model_visit(
                data_repo=data_repo,
                run_dir=run_dir,
                api_key=api_key,
                openrouter_api_key=openrouter_api_key,
                opening=curator_note,
                once=once,
            )
        )
    except KeyboardInterrupt:
        record_terminal_run_event(
            store=SessionStore(run_dir / "session", run_id),
            run_dir=run_dir,
            event_type="run_aborted",
            payload={"reason": "operator interrupt"},
            visibility="operator",
            console=Console(stderr=True),
        )
        raise
    except Exception as error:
        record_terminal_run_event(
            store=SessionStore(run_dir / "session", run_id),
            run_dir=run_dir,
            event_type="run_failed",
            payload={
                "reason": "unhandled harness error",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            visibility="operator",
            console=Console(stderr=True),
        )
        raise
