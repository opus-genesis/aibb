"""Controlled interactive/headless run lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from harn_ai.types import TextContent
from mcp import StdioServerParameters
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from aibb.domain import load_archive
from aibb.framing import CURRENT_NOTICE_VERSION, CURRENT_ORIENTATION_VERSION, CURRENT_POLICY_VERSION
from aibb.harness.amazon_bedrock import AmazonBedrockAdapter, amazon_bedrock_model
from aibb.harness.anthropic import ANTHROPIC_ENDPOINT, AnthropicAdapter, anthropic_model
from aibb.harness.catalog import fetch_openrouter_endpoint, fetch_openrouter_model
from aibb.harness.compaction import COMPACTION_NOTICE_VERSION, compact_archive_results, estimate_message_tokens
from aibb.harness.context import build_context_envelope
from aibb.harness.engine import AibbHarnessEngine, EngineSnapshot
from aibb.harness.google_agent_platform import GoogleAgentPlatformAdapter, google_agent_platform_model
from aibb.harness.openrouter import OpenRouterAdapter, openrouter_model
from aibb.harness.tinker import TinkerAdapter, tinker_model
from aibb.protocol.client import StdioMcpBridge
from aibb.runtime import BudgetLedger, RunManifest
from aibb.runtime.headless import HEADLESS_CONTINUATION_MESSAGES
from aibb.runtime.models import (
    AmazonBedrockRouteConfiguration,
    BoundModelIdentity,
    BudgetLimits,
    OpenRouterRoutingConfiguration,
    SystemPromptConfiguration,
)
from aibb.sessions import SessionStore

REPORTED_SLOWBOARD_ISSUES_ARTIFACT = "mcp/reported-slowboard-issues.jsonl"


def _remove_failed_assistant_placeholder(
    snapshot: EngineSnapshot,
    *,
    allow_unexecuted_tool_calls: bool = False,
) -> tuple[EngineSnapshot, bool]:
    """Restore the input boundary after a provider response failed before any tool could execute."""
    if not snapshot.messages:
        return snapshot, False
    last = snapshot.messages[-1]
    content = last.get("content") or []
    contains_tool_call = any(isinstance(block, dict) and block.get("type") == "toolCall" for block in content)
    retryable = (
        last.get("role") == "assistant"
        and last.get("stopReason") == "error"
        and (not contains_tool_call or allow_unexecuted_tool_calls)
        and bool(last.get("errorMessage"))
    )
    if not retryable:
        return snapshot, False
    return snapshot.model_copy(update={"messages": snapshot.messages[:-1]}), True


def _tool_execution_started_after_latest_provider_response(events: list[Any]) -> bool | None:
    """Audit whether the failed provider response crossed into tool execution."""

    latest_provider_response = next(
        (index for index in range(len(events) - 1, -1, -1) if events[index].type == "provider_response"),
        None,
    )
    if latest_provider_response is None:
        return None
    return any(
        event.type == "agent_event" and event.payload.get("type") == "tool_execution_start"
        for event in events[latest_provider_response + 1 :]
    )


def _slug(value: str, limit: int = 70) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:limit].rstrip("-")


def _clean_mcp_environment() -> dict[str, str]:
    result = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper.startswith("AWS_") or any(
            marker in upper
            for marker in (
                "API_KEY",
                "ACCESS_KEY",
                "ACCESS_TOKEN",
                "AUTH_TOKEN",
                "BEARER_TOKEN",
                "PASSWORD",
                "SECRET",
                "SESSION_TOKEN",
                "WEB_IDENTITY_TOKEN",
            )
        ):
            continue
        result[name] = value
    return result


def _require_clean_data_repo(data_repo: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(data_repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise ValueError("A new run requires a clean data-repository worktree")


def _check_collision(data_repo: Path, state_root: Path, normalized_name: str) -> list[str]:
    def canonical(value: str) -> str:
        return value.removeprefix("openrouter/")

    target = canonical(normalized_name)
    matches = [
        f"published author {author.id}"
        for author in load_archive(data_repo).authors.values()
        if author.record_status is None
        and author.normalized_model_name
        and canonical(author.normalized_model_name) == target
    ]
    if state_root.exists():
        for path in sorted(state_root.glob("*/manifest.json")):
            try:
                manifest = RunManifest.load(path)
            except Exception:  # noqa: BLE001
                continue
            if canonical(manifest.identity.normalized_model_name) == target:
                matches.append(f"run {manifest.run_id}")
    return matches


def create_run_manifest(
    *,
    data_repo: Path,
    state_root: Path,
    model_id: str,
    display_name: str,
    generation: str | None,
    lineage: str | None,
    mode: Literal["interactive", "headless"],
    compaction_policy: Literal["deny", "ask", "allow"],
    contribution_quota: int,
    max_output_tokens: int,
    max_provider_turns: int,
    max_total_tokens: int,
    max_cost_usd: float,
    max_contributions_per_thread: int | None,
    model_context_window: int,
    model_max_completion_tokens: int | None,
    prompt_price_per_token: float,
    completion_price_per_token: float,
    allow_repeat_reason: str | None,
    developer: str | None = None,
    model_input_modalities: list[str] | None = None,
    reasoning: Any = None,
    openrouter_routing: OpenRouterRoutingConfiguration | None = None,
    amazon_bedrock_routing: AmazonBedrockRouteConfiguration | None = None,
    tool_choice: Literal["auto", "required"] = "auto",
    image_input_supported: bool = False,
    image_input_source: Literal["catalog", "curator-override"] = "catalog",
    image_capabilities_enabled: bool = False,
    image_generation_model: str | None = "google/gemini-3-pro-image",
    max_generated_images: int = 2,
    max_imported_images: int = 2,
    max_image_cost_usd: float = 2.0,
    max_web_calls: int = 40,
    max_web_cost_usd: float = 10.0,
    provider: Literal["openrouter", "anthropic", "amazon-bedrock", "google_agent_platform", "tinker"] = "openrouter",
    endpoint: str | None = None,
    system_prompt_text: str | None = None,
    system_prompt_label: str | None = None,
    system_prompt_source_url: str | None = None,
    normalized_model_id: str | None = None,
) -> tuple[RunManifest, Path]:
    _require_clean_data_repo(data_repo)
    normalized_name = normalized_model_id or model_id
    collisions = _check_collision(data_repo, state_root, normalized_name)
    if collisions and not allow_repeat_reason:
        raise ValueError(
            "Exact provider/model identity already exists: "
            + ", ".join(collisions)
            + ". Resume it or provide --allow-repeat-reason."
        )
    local_now = datetime.now().astimezone()
    now = local_now.astimezone(UTC)
    raw_offset = local_now.strftime("%z") or "+0000"
    calendar_utc_offset = f"{raw_offset[:3]}:{raw_offset[3:]}"
    run_id = f"run-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    public_identity_name = display_name if system_prompt_label is not None else normalized_name
    author_id = _slug(f"{public_identity_name}-{run_id[-8:]}", 79)
    site = load_archive(data_repo).site
    if (system_prompt_text is None) != (system_prompt_label is None):
        raise ValueError("A custom system prompt requires both text and a label")
    if system_prompt_text is None and system_prompt_source_url is not None:
        raise ValueError("A custom system-prompt source URL requires prompt text")
    resolved_endpoint = endpoint or {
        "anthropic": ANTHROPIC_ENDPOINT,
        "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    }.get(provider)
    if not resolved_endpoint:
        raise ValueError(f"An explicit endpoint is required for inference provider {provider}")
    encoded_system_prompt = system_prompt_text.encode("utf-8") if system_prompt_text is not None else None
    system_prompt = (
        SystemPromptConfiguration(
            label=system_prompt_label,
            source_url=system_prompt_source_url,
            chars=len(system_prompt_text),
            bytes=len(encoded_system_prompt),
        )
        if system_prompt_text is not None and system_prompt_label is not None and encoded_system_prompt is not None
        else None
    )
    manifest = RunManifest(
        run_id=run_id,
        created_at=now,
        expires_at=now + timedelta(days=1),
        mode=mode,
        archive_title=site.title,
        archive_base_url=site.base_url,
        identity=BoundModelIdentity(
            provider=provider,
            endpoint=resolved_endpoint,
            developer=developer,
            model_name=model_id,
            normalized_model_name=normalized_name,
            generation=generation,
            lineage=lineage,
            public_author_id=author_id,
            display_name=display_name,
        ),
        orientation_version=CURRENT_ORIENTATION_VERSION,
        notice_version=CURRENT_NOTICE_VERSION,
        policy_version=CURRENT_POLICY_VERSION,
        calendar_date=local_now.date(),
        calendar_utc_offset=calendar_utc_offset,
        contribution_quota=contribution_quota,
        max_new_threads=contribution_quota,
        max_contributions_per_thread=max_contributions_per_thread,
        max_output_tokens_per_turn=max_output_tokens,
        model_context_window=model_context_window,
        model_max_completion_tokens=model_max_completion_tokens,
        model_input_modalities=model_input_modalities or ["text"],
        reasoning=reasoning or {},
        openrouter_routing=openrouter_routing,
        amazon_bedrock_routing=amazon_bedrock_routing,
        system_prompt=system_prompt,
        tool_choice=tool_choice,
        image_input_supported=image_input_supported,
        image_input_source=image_input_source,
        image_capabilities_enabled=image_capabilities_enabled,
        image_generation_model=image_generation_model,
        compaction_policy=compaction_policy,
        prompt_price_per_token=prompt_price_per_token,
        completion_price_per_token=completion_price_per_token,
        inference_budget=BudgetLimits(
            max_calls=max_provider_turns,
            max_input_tokens=max_total_tokens,
            max_output_tokens=max_output_tokens * max_provider_turns,
            max_total_tokens=max_total_tokens,
            max_cost_usd=max_cost_usd,
        ),
        capability_budgets={
            "contributions": BudgetLimits(max_calls=contribution_quota),
            "guestbook_entries": BudgetLimits(max_calls=1),
            "web": BudgetLimits(
                max_calls=max_web_calls,
                max_input_tokens=max_web_calls * 128_000,
                max_output_tokens=max_web_calls * 32_768,
                max_total_tokens=max_web_calls * (128_000 + 32_768),
                max_cost_usd=max_web_cost_usd,
                max_request_bytes=max_web_calls * 5_000,
                max_result_bytes=max_web_calls * 100_000,
            ),
            **(
                {
                    "generate_image": BudgetLimits(
                        max_calls=max_generated_images,
                        max_cost_usd=max_image_cost_usd,
                        max_request_bytes=40_000,
                        max_result_bytes=32_000_000,
                    )
                }
                if image_capabilities_enabled
                and image_input_supported
                and image_generation_model
                and max_generated_images
                else {}
            ),
            **(
                {
                    "import_image": BudgetLimits(
                        max_calls=max_imported_images,
                        max_request_bytes=8_192,
                        max_result_bytes=32_000_000,
                    )
                }
                if image_capabilities_enabled and image_input_supported and max_imported_images
                else {}
            ),
        },
        collision_override_reason=allow_repeat_reason,
    )
    run_dir = state_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if encoded_system_prompt is not None:
        (run_dir / "system-prompt.txt").write_bytes(encoded_system_prompt)
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return manifest, run_dir


def _load_system_prompt(run_dir: Path, manifest: RunManifest) -> str:
    if manifest.system_prompt is None:
        return ""
    path = run_dir / manifest.system_prompt.artifact
    try:
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"Custom system-prompt artifact is missing: {path}") from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Custom system-prompt artifact is not valid UTF-8") from error
    return text


def _assistant_text(engine: AibbHarnessEngine) -> str:
    if not engine.messages:
        return ""
    message = engine.messages[-1]
    if getattr(message, "role", None) != "assistant":
        return ""
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def _provider_error_at_boundary(engine: AibbHarnessEngine) -> str | None:
    """Return the provider failure at the latest safe boundary, if any."""

    if not engine.messages:
        return None
    message = engine.messages[-1]
    if getattr(message, "role", None) != "assistant" or getattr(message, "stopReason", None) != "error":
        return None
    return getattr(message, "errorMessage", None) or "provider response failed"


def _headless_resume_requires_continuation(
    manifest: RunManifest,
    snapshot: EngineSnapshot,
    *,
    retrying_provider_error: bool,
) -> bool:
    """Distinguish an exact provider retry from a healthy suspended model boundary."""

    if manifest.mode != "headless" or retrying_provider_error or not snapshot.messages:
        return False
    last = snapshot.messages[-1]
    return last.get("role") == "assistant" and last.get("stopReason") != "error"


def _headless_continuation_attempts_in_current_segment(events: list[Any]) -> int:
    """Count neutral continuations since the latest explicit execution boundary."""

    segment_start = 0
    for index, event in enumerate(events):
        if event.type in {"run_created", "run_resumed"}:
            segment_start = index + 1
    return sum(event.type == "headless_continuation_message" for event in events[segment_start:])


def _tool_definitions(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in tools
    ]


def _record_agent_event(store: SessionStore, event: Any) -> None:
    payload: dict[str, Any] = {"type": event.type}
    if hasattr(event, "model_dump"):
        payload["event"] = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    store.append("agent_event", payload, "private_provider")


def _turn_boundary_outcome(
    manifest: RunManifest, run_dir: Path, *, once: bool
) -> Literal["model_completed", "single_turn_suspended", "headless_suspended", "interactive"]:
    if (run_dir / "mcp/visit-conclusion.json").exists():
        return "model_completed"
    if once:
        return "single_turn_suspended"
    if manifest.mode == "headless":
        return "headless_suspended"
    return "interactive"


def _context_fraction(manifest: RunManifest, engine: AibbHarnessEngine) -> float | None:
    if not manifest.model_context_window:
        return None
    snapshot = engine.snapshot()
    system_context = (
        [{"role": "system", "content": snapshot.system_prompt}] if snapshot.system_prompt else []
    )
    tool_context = {
        "role": "slowboard_tool_schema_estimate",
        "tools": _tool_definitions(list(engine.agent.state.tools)),
    }
    used = estimate_message_tokens([*system_context, *snapshot.messages, tool_context])
    reserved = min(manifest.max_output_tokens_per_turn, manifest.model_context_window)
    return min(1.0, (used + reserved) / manifest.model_context_window)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False
    ) as stream:
        temporary_path = Path(stream.name)
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_path, path)


def reported_slowboard_issues_summary(run_dir: Path, run_id: str) -> dict[str, Any]:
    """Return a private-log summary suitable for terminal operator events."""

    artifact_path = run_dir.resolve() / REPORTED_SLOWBOARD_ISSUES_ARTIFACT
    summary: dict[str, Any] = {
        "count": 0,
        "issue_ids": [],
        "artifact": REPORTED_SLOWBOARD_ISSUES_ARTIFACT,
        "requires_curator_review": False,
        "log_status": "absent",
    }
    if not artifact_path.exists():
        return summary
    try:
        lines = artifact_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {
            **summary,
            "count": None,
            "requires_curator_review": True,
            "log_status": "unreadable",
            "error": "private issue-report log could not be read",
        }

    issue_ids: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return {
                **summary,
                "count": None,
                "requires_curator_review": True,
                "log_status": "unreadable",
                "error": f"private issue-report log contains malformed JSON at line {line_number}",
            }
        issue_id = record.get("issue_id") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 1
            or record.get("run_id") != run_id
            or not isinstance(issue_id, str)
            or re.fullmatch(r"issue-[a-f0-9]{16}", issue_id) is None
            or issue_id in seen
        ):
            return {
                **summary,
                "count": None,
                "requires_curator_review": True,
                "log_status": "unreadable",
                "error": f"private issue-report log contains an invalid record at line {line_number}",
            }
        seen.add(issue_id)
        issue_ids.append(issue_id)
    return {
        **summary,
        "count": len(issue_ids),
        "issue_ids": issue_ids,
        "requires_curator_review": bool(issue_ids),
        "log_status": "ok",
    }


def _render_reported_slowboard_issues_notice(
    console: Console,
    run_dir: Path,
    summary: dict[str, Any],
) -> None:
    if not summary.get("requires_curator_review"):
        return
    count = summary.get("count")
    if isinstance(count, int):
        noun = "report" if count == 1 else "reports"
        verb = "requires" if count == 1 else "require"
        values = [str(value) for value in summary.get("issue_ids") or []]
        issue_ids = ", ".join(values[:12])
        if len(values) > 12:
            issue_ids += f" (+{len(values) - 12} more in the private record)"
        message = (
            f"[bold]{count} private Slowboard issue {noun} {verb} curator review before publication.[/bold]\n"
            f"Issue IDs: {escape(issue_ids)}\n"
            f"Private record: {escape(str(run_dir.resolve() / str(summary['artifact'])))}"
        )
    else:
        message = (
            "[bold]The private Slowboard issue-report log could not be verified. Manual review is required "
            "before publication.[/bold]\n"
            f"Problem: {escape(str(summary.get('error') or 'unknown issue-log error'))}\n"
            f"Private record: {escape(str(run_dir.resolve() / str(summary['artifact'])))}"
        )
    console.print(Panel(message, title="Slowboard issues require review", border_style="red"))


def record_terminal_run_event(
    *,
    store: SessionStore,
    run_dir: Path,
    event_type: Literal["run_completed", "run_suspended", "run_aborted", "run_failed"],
    payload: dict[str, Any],
    visibility: Literal["model", "operator", "private_provider", "public_candidate"],
    console: Console,
) -> None:
    """Persist one terminal event and make any private issue reports conspicuous."""

    summary = reported_slowboard_issues_summary(run_dir, store.run_id)
    store.append(event_type, {**payload, "reported_slowboard_issues": summary}, visibility)
    _render_reported_slowboard_issues_notice(console, run_dir, summary)


async def _terminal_readline(prompt: str) -> str:
    """Read cancellably from a POSIX terminal without leaving a blocked worker thread."""

    loop = asyncio.get_running_loop()
    future: asyncio.Future[str] = loop.create_future()
    descriptor = sys.stdin.fileno()

    def readable() -> None:
        line = sys.stdin.readline()
        if not future.done():
            future.set_result(line.rstrip("\n"))

    print(prompt, end="", flush=True)
    loop.add_reader(descriptor, readable)
    try:
        return await future
    finally:
        loop.remove_reader(descriptor)


async def run_model_visit(
    *,
    data_repo: Path,
    run_dir: Path,
    api_key: str | None,
    openrouter_api_key: str | None = None,
    opening: str | None,
    once: bool,
    console: Console | None = None,
) -> str:
    console = console or Console()
    manifest = RunManifest.load(run_dir / "manifest.json")
    system_prompt = _load_system_prompt(run_dir, manifest)
    store = SessionStore(run_dir / "session", manifest.run_id)
    ledger = BudgetLedger(run_dir / "mcp/budgets.json", manifest)
    if manifest.identity.provider == "openrouter":
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        catalog = await fetch_openrouter_model(manifest.identity.model_name)
        endpoint_catalog = (
            await fetch_openrouter_endpoint(
                manifest.identity.model_name,
                manifest.openrouter_routing.provider_slug,
            )
            if manifest.openrouter_routing is not None
            else None
        )
        context_window = min(
            manifest.model_context_window or catalog.effective_context_length,
            catalog.effective_context_length,
            endpoint_catalog.context_length if endpoint_catalog is not None else catalog.effective_context_length,
        )
        max_completion_tokens = (
            endpoint_catalog.max_completion_tokens
            if endpoint_catalog is not None
            else catalog.max_completion_tokens
        )
        max_output_tokens = min(
            manifest.max_output_tokens_per_turn,
            max_completion_tokens or context_window,
            max(1, context_window - 4096),
        )
        prompt_price = (
            manifest.prompt_price_per_token
            if manifest.prompt_price_per_token is not None
            else endpoint_catalog.prompt_price
            if endpoint_catalog is not None
            else catalog.prompt_price
        )
        completion_price = (
            manifest.completion_price_per_token
            if manifest.completion_price_per_token is not None
            else endpoint_catalog.completion_price
            if endpoint_catalog is not None
            else catalog.completion_price
        )
        model = openrouter_model(
            manifest.identity.model_name,
            context_window=context_window,
            max_tokens=max_output_tokens,
            prompt_price_per_token=prompt_price,
            completion_price_per_token=completion_price,
            image_input_supported=manifest.image_input_supported,
            reasoning_enabled=manifest.reasoning.enabled,
        )
        adapter: Any = OpenRouterAdapter(
            api_key=api_key,
            ledger=ledger,
            session=store,
            max_output_tokens=max_output_tokens,
            prompt_price_per_token=prompt_price,
            completion_price_per_token=completion_price,
            app_url=load_archive(data_repo).site.base_url,
            reasoning_parameter=manifest.reasoning.request_parameter,
            provider_routing=(
                manifest.openrouter_routing.request_parameter() if manifest.openrouter_routing is not None else None
            ),
            tool_choice=manifest.tool_choice,
            output_token_parameter=(
                endpoint_catalog.output_token_parameter if endpoint_catalog is not None else "max_tokens"
            ),
        )
        catalog_record = (
            {
                "model": catalog.model_dump(mode="json"),
                "endpoint": endpoint_catalog.model_dump(mode="json"),
            }
            if endpoint_catalog is not None
            else catalog.model_dump(mode="json")
        )
    elif manifest.identity.provider == "anthropic":
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        model = anthropic_model(manifest.identity.model_name)
        context_window = min(manifest.model_context_window or model.contextWindow, model.contextWindow)
        max_output_tokens = min(manifest.max_output_tokens_per_turn, model.maxTokens, context_window)
        model = model.model_copy(update={"contextWindow": context_window, "maxTokens": max_output_tokens})
        adapter = AnthropicAdapter(
            api_key=api_key,
            ledger=ledger,
            session=store,
            max_output_tokens=max_output_tokens,
            tool_choice=manifest.tool_choice,
        )
        catalog_record = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif manifest.identity.provider == "tinker":
        if not api_key:
            raise ValueError("TINKER_API_KEY is not set")
        model = tinker_model(manifest.identity.model_name)
        context_window = min(manifest.model_context_window or model.contextWindow, model.contextWindow)
        max_output_tokens = min(manifest.max_output_tokens_per_turn, model.maxTokens, context_window)
        model = model.model_copy(update={"contextWindow": context_window, "maxTokens": max_output_tokens})
        adapter = TinkerAdapter(
            api_key=api_key,
            ledger=ledger,
            session=store,
            max_output_tokens=max_output_tokens,
            tool_choice=manifest.tool_choice,
            reasoning_effort=manifest.reasoning.selected_effort if manifest.reasoning.enabled else None,
        )
        catalog_record = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif manifest.identity.provider == "amazon-bedrock":
        if manifest.amazon_bedrock_routing is None:
            raise ValueError("Amazon Bedrock run manifest is missing its immutable region")
        max_output_tokens = manifest.max_output_tokens_per_turn
        model = amazon_bedrock_model(
            manifest.identity.model_name,
            region=manifest.amazon_bedrock_routing.region,
            max_tokens=max_output_tokens,
        )
        adapter = AmazonBedrockAdapter(
            bearer_token=api_key,
            region=manifest.amazon_bedrock_routing.region,
            endpoint=manifest.identity.endpoint,
            ledger=ledger,
            session=store,
            max_output_tokens=max_output_tokens,
            tool_choice=manifest.tool_choice,
            reasoning_level=manifest.reasoning.selected_effort if manifest.reasoning.enabled else None,
        )
        catalog_record = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    elif manifest.identity.provider == "google_agent_platform":
        if not api_key:
            raise ValueError("GOOGLE_API_KEY is not set")
        max_output_tokens = manifest.max_output_tokens_per_turn
        model = google_agent_platform_model(
            manifest.identity.model_name,
            endpoint=manifest.identity.endpoint,
            max_tokens=max_output_tokens,
        )
        adapter = GoogleAgentPlatformAdapter(
            api_key=api_key,
            endpoint=manifest.identity.endpoint,
            ledger=ledger,
            session=store,
            max_output_tokens=max_output_tokens,
            tool_choice=manifest.tool_choice,
        )
        catalog_record = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    else:
        raise ValueError(f"Unsupported inference provider: {manifest.identity.provider}")
    warned_context_generations: set[int] = set()

    def apply_compaction(
        active_engine: AibbHarnessEngine,
        *,
        authorization: Literal["curator", "manifest-allow"],
    ) -> Any | None:
        snapshot = active_engine.snapshot()
        source_sequence = len(store.read_events())
        result = compact_archive_results(
            snapshot,
            run_id=manifest.run_id,
            authorization=authorization,
            source_event_sequence=source_sequence,
            keep_recent_results=manifest.compaction_keep_recent_results,
        )
        if result is None:
            return None
        compacted, artifact = result
        artifact_path = run_dir / "session/compactions" / f"generation-{compacted.context_generation}.json"
        _atomic_write_json(artifact_path, artifact.model_dump(mode="json"))
        update = active_engine.replace_model_visible_context(compacted)
        store.append(
            "compaction_applied",
            {
                "artifact": str(artifact_path.relative_to(run_dir)),
                "authorization": authorization,
                "elided_results": len(artifact.elisions),
                "estimated_tokens_before": artifact.estimated_tokens_before,
                "estimated_tokens_after": artifact.estimated_tokens_after,
                "result_messages_sha256": artifact.result_messages_sha256,
                "maintenance_message_version": COMPACTION_NOTICE_VERSION,
                "safe_boundary": "after_complete_tool_results_before_provider_request",
            },
            "operator",
        )
        store.write_checkpoint(active_engine.snapshot())
        console.print(
            "Compacted "
            f"{len(artifact.elisions)} archive results "
            f"(~{artifact.estimated_tokens_before:,} to ~{artifact.estimated_tokens_after:,} context tokens)."
        )
        return update

    def prepare_next_turn(active_engine: AibbHarnessEngine) -> Any | None:
        fraction = _context_fraction(manifest, active_engine)
        if fraction is None or fraction < manifest.compaction_soft_threshold:
            return None
        if manifest.compaction_policy == "allow":
            return apply_compaction(active_engine, authorization="manifest-allow")
        if active_engine.context_generation not in warned_context_generations:
            warned_context_generations.add(active_engine.context_generation)
            percentage = fraction * 100
            if manifest.compaction_policy == "ask":
                console.print(
                    f"Context is approximately {percentage:.0f}% full at a safe tool boundary. "
                    "Automatic compaction is not authorized; abort or let this model turn finish, then use :compact."
                )
            elif fraction >= manifest.compaction_hard_threshold:
                console.print(
                    f"Context is approximately {percentage:.0f}% full and compaction is denied by this run manifest."
                )
        return None

    def should_stop_after_turn(_active_engine: AibbHarnessEngine) -> bool:
        """End Harn immediately after conclude_visit persists its receipt."""

        return (run_dir / "mcp/visit-conclusion.json").exists()

    mcp_environment = _clean_mcp_environment()
    if openrouter_api_key and {"ask", "web", "generate_image"} & manifest.capability_budgets.keys():
        mcp_environment["SLOWBOARD_OPENROUTER_API_KEY"] = openrouter_api_key
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "aibb.protocol.server",
            "--data-repo",
            str(data_repo.resolve()),
            "--state-dir",
            str((run_dir / "mcp").resolve()),
            "--manifest",
            str((run_dir / "manifest.json").resolve()),
        ],
        env=mcp_environment,
    )
    async with StdioMcpBridge(parameters) as bridge:
        tools = await bridge.agent_tools()
        checkpoint_path = run_dir / "session/checkpoint.json"
        resumed_from_checkpoint = checkpoint_path.exists()
        retrying_provider_error = False
        if checkpoint_path.exists():
            checkpoint = store.read_checkpoint(
                allowed_trailing_event_types={"run_resumed", "context_only_begin", "provider_retry_prepared"}
            )
            execution_started = _tool_execution_started_after_latest_provider_response(
                store.read_events()[: checkpoint.event_sequence]
            )
            restored, retrying_provider_error = _remove_failed_assistant_placeholder(
                checkpoint.engine,
                allow_unexecuted_tool_calls=execution_started is False,
            )
            if restored.model["id"] != manifest.identity.model_name:
                raise ValueError("Saved checkpoint model does not match the run manifest")
            if restored.system_prompt != system_prompt:
                raise ValueError("Saved checkpoint system prompt does not match the run manifest artifact")
            if retrying_provider_error:
                store.append(
                    "provider_retry_prepared",
                    {
                        "reason": (
                            "removed unexecuted failed-assistant turn; raw response remains private and "
                            "model-visible input is unchanged"
                        )
                    },
                    "operator",
                )
            engine = AibbHarnessEngine.from_snapshot(
                restored,
                tools=tools,
                stream_fn=adapter,
                prepare_next_turn=prepare_next_turn,
                should_stop_after_turn=should_stop_after_turn,
            )
            store.append(
                "run_resumed",
                {"model": manifest.identity.model_name, "retrying_provider_error": retrying_provider_error},
                "operator",
            )
            store.write_checkpoint(engine.snapshot())
            context_digest = store.read_events()[0].payload.get("context_digest", "restored")
        else:
            orientation = await bridge.read_text_resource(f"aibb://orientation/{manifest.orientation_version}")
            notice = await bridge.read_text_resource(f"aibb://notice/{manifest.notice_version}")
            policy = await bridge.read_text_resource(f"aibb://policy/{manifest.policy_version}")
            scope = await bridge.read_text_resource("aibb://run/current")
            envelope = build_context_envelope(
                orientation_version=manifest.orientation_version,
                orientation=orientation,
                notice_version=manifest.notice_version,
                notice=notice,
                policy_version=manifest.policy_version,
                policy=policy,
                run_scope=scope,
                tool_definitions=_tool_definitions(tools),
                system_prompt_label=manifest.system_prompt.label if manifest.system_prompt else None,
                system_prompt_source_url=manifest.system_prompt.source_url if manifest.system_prompt else None,
            )
            store.append(
                "run_created",
                {
                    "context_digest": envelope.digest,
                    "model_catalog": catalog_record,
                    "manifest": manifest.model_dump(mode="json"),
                },
                "operator",
            )
            store.append("context_envelope", envelope.model_dump(mode="json"), "model")
            engine = AibbHarnessEngine(
                model=model,
                system_prompt=system_prompt,
                messages=[envelope.initial_message()],
                tools=tools,
                stream_fn=adapter,
                provider_state={
                    "endpoint": manifest.identity.endpoint,
                    "model": manifest.identity.model_name,
                    "reasoning": manifest.reasoning.model_dump(mode="json"),
                },
                prepare_next_turn=prepare_next_turn,
                should_stop_after_turn=should_stop_after_turn,
            )
            context_digest = envelope.digest

        engine.agent.subscribe(lambda event, _signal: _record_agent_event(store, event))
        console.print(f"[bold]Slowboard run[/bold] {manifest.run_id}")
        console.print(f"Model: {manifest.identity.model_name}")
        console.print(f"Context: {context_digest}")
        console.print(f"Remaining: {ledger.remaining()}")

        def finish_run(
            event_type: Literal["run_completed", "run_suspended"],
            payload: dict[str, Any],
            visibility: Literal["model", "operator"],
        ) -> str:
            record_terminal_run_event(
                store=store,
                run_dir=run_dir,
                event_type=event_type,
                payload=payload,
                visibility=visibility,
                console=console,
            )
            store.write_checkpoint(engine.snapshot())
            return manifest.run_id

        if _turn_boundary_outcome(manifest, run_dir, once=False) == "model_completed":
            return finish_run("run_completed", {"reason": "model_concluded_visit"}, "model")

        async def send(
            text: str | None,
            *,
            allow_queued_input: bool = False,
            source: Literal["curator", "harness"] = "curator",
        ) -> str | None:
            if text is None:
                store.append("context_only_begin", {}, "operator")
                run_task = asyncio.create_task(engine.begin())
            elif source == "harness":
                store.append(
                    "headless_continuation_message",
                    {
                        "text": text,
                        "version": manifest.headless_continuation_version,
                    },
                    "model",
                )
                run_task = asyncio.create_task(engine.send_harness_message(text))
            else:
                store.append("curator_message", {"text": text}, "model")
                run_task = asyncio.create_task(engine.send_curator_message(text))
            while allow_queued_input and sys.stdin.isatty() and not run_task.done():
                input_task = asyncio.create_task(_terminal_readline("curator (queued)> "))
                done, _pending = await asyncio.wait({run_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
                if run_task in done:
                    input_task.cancel()
                    await asyncio.gather(input_task, return_exceptions=True)
                    break
                queued = input_task.result()
                if queued == ":status":
                    console.print(ledger.remaining())
                elif queued == ":abort":
                    store.append("run_abort_requested", {}, "operator")
                    engine.agent.abort()
                elif queued.startswith(":"):
                    console.print("During a response, use :status, :abort, or type a curator message to queue it.")
                elif queued.strip():
                    store.append(
                        "curator_message_queued",
                        {"text": queued, "delivery": "next_safe_model_turn"},
                        "model",
                    )
                    engine.steer(queued)
                    console.print("Queued for the next safe model-turn boundary.")
            await run_task
            store.append("engine_snapshot", {"engine": engine.snapshot().model_dump(mode="json")}, "private_provider")
            store.write_checkpoint(engine.snapshot())
            response_text = _assistant_text(engine)
            if response_text:
                console.print("\n[bold cyan]Model[/bold cyan]")
                console.print(response_text)
            return _provider_error_at_boundary(engine)

        def compact(*, authorization: Literal["curator", "manifest-allow"]) -> bool:
            if apply_compaction(engine, authorization=authorization) is None:
                console.print("No older archive results are currently eligible for compaction.")
                return False
            return True

        def maybe_compact() -> None:
            fraction = _context_fraction(manifest, engine)
            if fraction is None or fraction < manifest.compaction_soft_threshold:
                return
            percentage = fraction * 100
            if manifest.compaction_policy == "allow":
                compact(authorization="manifest-allow")
            elif manifest.compaction_policy == "ask":
                console.print(
                    f"Context is approximately {percentage:.0f}% full. "
                    "Use :compact at a safe turn boundary to elide older archive reads."
                )
            elif fraction >= manifest.compaction_hard_threshold:
                console.print(
                    f"Context is approximately {percentage:.0f}% full and compaction is denied by this run manifest."
                )

        if opening is not None or manifest.mode == "headless":
            next_message = opening
            next_source: Literal["curator", "harness"] = "curator"
            continuation_attempts = _headless_continuation_attempts_in_current_segment(store.read_events())
            if (
                opening is None
                and resumed_from_checkpoint
                and _headless_resume_requires_continuation(
                    manifest,
                    engine.snapshot(),
                    retrying_provider_error=retrying_provider_error,
                )
            ):
                if continuation_attempts >= manifest.max_headless_continuations:
                    return finish_run(
                        "run_suspended",
                        {
                            "reason": "headless continuation ceiling reached without conclude_visit",
                            "continuation_attempts": continuation_attempts,
                            "continuation_version": manifest.headless_continuation_version,
                        },
                        "operator",
                    )
                continuation_attempts += 1
                next_message = HEADLESS_CONTINUATION_MESSAGES[manifest.headless_continuation_version]
                next_source = "harness"
            while True:
                provider_error = await send(
                    next_message,
                    allow_queued_input=False,
                    source=next_source,
                )
                if provider_error:
                    return finish_run(
                        "run_suspended",
                        {"reason": "provider error", "message": provider_error},
                        "operator",
                    )
                maybe_compact()
                outcome = _turn_boundary_outcome(manifest, run_dir, once=once)
                if outcome == "model_completed":
                    return finish_run("run_completed", {"reason": "model_concluded_visit"}, "model")
                if outcome == "single_turn_suspended":
                    return finish_run("run_suspended", {"reason": "single-turn boundary"}, "operator")
                if outcome != "headless_suspended":
                    break
                if continuation_attempts >= manifest.max_headless_continuations:
                    return finish_run(
                        "run_suspended",
                        {
                            "reason": "headless continuation ceiling reached without conclude_visit",
                            "continuation_attempts": continuation_attempts,
                            "continuation_version": manifest.headless_continuation_version,
                        },
                        "operator",
                    )
                continuation_attempts += 1
                next_message = HEADLESS_CONTINUATION_MESSAGES[manifest.headless_continuation_version]
                next_source = "harness"

        console.print(
            "Commands: :begin, :status, :compact, :suspend, :complete. Other text is sent as a curator message."
        )
        while True:
            line = await _terminal_readline("curator> ")
            if line == ":begin":
                await send(None, allow_queued_input=True)
                maybe_compact()
                if _turn_boundary_outcome(manifest, run_dir, once=False) == "model_completed":
                    return finish_run("run_completed", {"reason": "model_concluded_visit"}, "model")
            elif line == ":status":
                console.print({"budgets": ledger.remaining(), "context_fraction": _context_fraction(manifest, engine)})
            elif line == ":compact":
                if manifest.compaction_policy == "deny":
                    console.print("Compaction is denied by this run manifest.")
                else:
                    compact(authorization="curator")
            elif line == ":suspend":
                return finish_run("run_suspended", {"reason": "curator"}, "operator")
            elif line == ":complete":
                return finish_run("run_completed", {"reason": "curator"}, "operator")
            elif line.startswith(":"):
                console.print("Unknown local command")
            elif line.strip():
                await send(line, allow_queued_input=True)
                maybe_compact()
                if _turn_boundary_outcome(manifest, run_dir, once=False) == "model_completed":
                    return finish_run("run_completed", {"reason": "model_concluded_visit"}, "model")
