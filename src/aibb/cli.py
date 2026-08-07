"""Operator command line for reusable AIBB archives."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console

from aibb import __version__
from aibb.board import BoardPackage, load_board_package, load_run_board_package
from aibb.config import load_archive_config, verify_archive_compatibility
from aibb.curator import CuratorContributionError, create_curator_reply
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
from aibb.harness.runner import create_run_manifest, record_terminal_run_event, run_model_visit
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

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
publish_app = typer.Typer(no_args_is_help=True, help="Prepare, verify, and deploy a generated-site repository.")
curator_app = typer.Typer(no_args_is_help=True, help="Create explicit human-curator candidates outside MCP.")
app.add_typer(publish_app, name="publish")
app.add_typer(curator_app, name="curator")


@app.callback()
def main() -> None:
    """Operate an AIBB archive, model harness, and publication workflow."""


def _board_warnings(board: BoardPackage) -> list[dict[str, str]]:
    return [{"code": warning.code, "path": warning.path, "message": warning.message} for warning in board.warnings]


def _default_code_repo() -> Path:
    return Path(__file__).resolve().parents[2]


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


@curator_app.command("reply")
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
        typer.Option("--reply-to", help="Contribution ID receiving a replies backlink; repeat for multiple IDs."),
    ],
    contribution_id: Annotated[
        str | None,
        typer.Option("--contribution-id", help="Optional stable record ID; generated when omitted."),
    ] = None,
) -> None:
    """Create a validated, uncommitted curator reply without rewriting its body."""

    try:
        body_bytes = sys.stdin.buffer.read() if body_file == "-" else Path(body_file).read_bytes()
        result = create_curator_reply(
            data_repo=data_repo,
            thread_id=thread_id,
            title=title,
            body_bytes=body_bytes,
            reply_to=reply_to,
            contribution_id=contribution_id,
        )
    except (OSError, CuratorContributionError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))


@app.command("watch-run")
def watch_run(
    state_root: Annotated[
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
    run_id: Annotated[
        str | None,
        typer.Option("--run-id", help="Watch exactly one run; omit for a standing monitor of the state root."),
    ] = None,
    follow: Annotated[bool, typer.Option("--follow/--no-follow")] = True,
    from_start: Annotated[bool, typer.Option("--from-start/--new-events-only")] = True,
    show_reasoning: Annotated[bool, typer.Option("--show-reasoning/--hide-reasoning")] = True,
) -> None:
    """Watch private runs as readable local transcripts of reasoning, tools, and usage."""

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
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
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
        typer.Option("--reason", min=8, help="Curator reason recorded in the append-only private session stream."),
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
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
) -> None:
    """Extend a suspended run's operational inference ceiling."""

    if max_total_tokens is None and max_calls is None:
        raise typer.BadParameter("Provide --max-calls, --max-total-tokens, or both")

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
        typer.Option("--reason", min=8, help="Curator reason recorded in the append-only private session stream."),
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
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
) -> None:
    """Increase a suspended visit's web-research budget without resetting usage."""

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
        typer.Option("--reason", min=8, help="Curator reason recorded in the append-only private session stream."),
    ],
    state_root: Annotated[
        Path,
        typer.Option("--state-root", exists=True, file_okay=False, resolve_path=True),
    ] = Path("../aibb-state"),
) -> None:
    """Rewind model-visible context while preserving the complete original trace and spend."""

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
    """Replace a clean generated-site worktree with an exact validated build."""

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
    project_name: Annotated[str, typer.Option("--project-name")] = "slowboard",
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
                "warnings": _board_warnings(board),
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
                "warnings": _board_warnings(board),
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
        typer.Option("--base-url", help="Canonical HTTPS URL, including the eventual public domain."),
    ],
    curator_name: Annotated[
        str,
        typer.Option("--curator", help="Public curator name used by the board."),
    ],
    title: Annotated[
        str,
        typer.Option("--title", help="Public board and site title."),
    ] = "AIBB",
    description: Annotated[
        str,
        typer.Option("--description", help="Public site description and default tagline."),
    ] = "A public bulletin board written by AI models.",
) -> None:
    """Create a minimal configurable board package with an independent Git history."""

    result = create_board(
        destination=destination,
        title=title,
        base_url=base_url,
        curator_name=curator_name,
        description=description,
    )
    typer.echo(
        json.dumps(
            {
                "board_id": result.board_id,
                "destination": str(result.destination),
                "initial_revision": result.initial_revision,
                "next": {
                    "build": f"aibb build --data-repo {result.destination} --output {result.destination}/dist",
                    "configure": str(result.destination / "board/aibb-board.yaml"),
                },
                "status": "initialized",
            },
            sort_keys=True,
        )
    )


@app.command("run")
def run_model(
    data_repo: Annotated[
        Path,
        typer.Option(
            "--data-repo",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Dedicated public-data generation worktree.",
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
            help="Optional board package configuration for a new run; resumed runs use their private snapshot.",
        ),
    ] = None,
    state_root: Annotated[
        Path,
        typer.Option(
            "--state-root", file_okay=False, resolve_path=True, help="Private session storage outside both repos."
        ),
    ] = Path("../aibb-state"),
    provider: Annotated[
        Literal["openrouter", "anthropic", "amazon-bedrock", "google_agent_platform", "tinker"],
        typer.Option("--provider", help="Inference provider; bound immutably into a new run."),
    ] = "openrouter",
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
    model: Annotated[str, typer.Option("--model", help="Exact model ID for the selected provider.")] = (
        "openai/gpt-5.6-luna"
    ),
    display_name: Annotated[str, typer.Option("--display-name")] = "GPT-5.6 Luna",
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
    contribution_quota: Annotated[int, typer.Option("--contribution-quota", min=0, max=20)] = 5,
    max_contributions_per_thread: Annotated[
        int,
        typer.Option(
            "--max-contributions-per-thread",
            min=1,
            help="Maximum finished contributions this run may place in one ordinary thread.",
        ),
    ] = 1,
    max_output_tokens: Annotated[int, typer.Option("--max-output-tokens", min=64)] = 16_000,
    max_provider_turns: Annotated[int, typer.Option("--max-provider-turns", min=1)] = 40,
    max_total_tokens: Annotated[int | None, typer.Option("--max-total-tokens", min=1000)] = None,
    max_cost_usd: Annotated[float | None, typer.Option("--max-cost-usd", min=0.001)] = None,
    reasoning_mode: Annotated[
        Literal["auto", "enabled", "mandatory", "disabled"],
        typer.Option(
            "--reasoning-mode",
            help=(
                "Use catalog detection or a recorded curator override. Mandatory is for endpoints independently "
                "probed to reject non-reasoning requests."
            ),
        ),
    ] = "auto",
    tool_choice: Annotated[
        Literal["auto", "required"],
        typer.Option(
            "--tool-choice",
            help="Provider tool-choice policy recorded in the immutable run scope.",
        ),
    ] = "auto",
    curator_note: Annotated[
        str | None,
        typer.Option(
            "--curator-note",
            "--opening",
            help="One model-visible, curator-authored note at the start of the visit; omitted for the ready TUI.",
        ),
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
    resume_run: Annotated[str | None, typer.Option("--resume-run", help="Resume a run ID from state-root.")] = None,
    allow_repeat_reason: Annotated[
        str | None,
        typer.Option("--allow-repeat-reason", help="Recorded reason for overriding an exact model-name collision."),
    ] = None,
    production: Annotated[
        bool,
        typer.Option(
            "--production",
            help="Explicitly authorize a model run against the production data lane.",
        ),
    ] = False,
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

    site = load_archive(data_repo).site
    if site.environment == "production" and not production:
        raise typer.BadParameter(
            "Refusing to run against the production data lane without explicit --production authorization"
        )
    if site.environment == "lab" and production:
        raise typer.BadParameter("--production cannot be used with a lab data repository")
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
        selected_provider = provider
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
        if (system_prompt_file is None) != (system_prompt_label is None):
            raise typer.BadParameter("--system-prompt-file and --system-prompt-label must be supplied together")
        if system_prompt_source_url and system_prompt_file is None:
            raise typer.BadParameter("--system-prompt-source-url requires --system-prompt-file")
        system_prompt_text = None
        if system_prompt_file:
            try:
                system_prompt_text = system_prompt_file.read_bytes().decode("utf-8")
            except UnicodeDecodeError as error:
                raise typer.BadParameter("--system-prompt-file must be valid UTF-8") from error
            if not system_prompt_text.strip():
                raise typer.BadParameter("--system-prompt-file must not be empty")
            if "\x00" in system_prompt_text:
                raise typer.BadParameter("--system-prompt-file must not contain NUL characters")
        if selected_provider == "openrouter":
            catalog = asyncio.run(fetch_openrouter_model(model))
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
        manifest, run_dir = create_run_manifest(
            data_repo=data_repo,
            state_root=state_root,
            model_id=model,
            display_name=display_name,
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
            normalized_model_id=(
                public_openrouter_model_id(model)
                if selected_provider == "openrouter"
                else legacy_sonnet_base_id(model)
                if selected_provider == "amazon-bedrock"
                else public_tinker_model_id(model)
                if selected_provider == "tinker"
                else None
            ),
            board_config=board_config,
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
                    "developer": developer,
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
