"""Readable local rendering for the append-only private run event stream."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.pretty import Pretty
from rich.rule import Rule

from aibb.harness.openrouter import MAX_TOOL_CALLS_PER_RESPONSE
from aibb.runtime import RunManifest

TERMINAL_EVENTS = {"run_completed", "run_suspended", "run_aborted", "run_failed"}
FINAL_EVENTS = {"run_completed", "run_aborted", "run_failed"}


def run_directories(state_root: Path) -> list[Path]:
    """Return valid run directories in manifest creation order."""

    candidates: list[tuple[object, str, Path]] = []
    for manifest_path in state_root.resolve().glob("run-*/manifest.json"):
        try:
            manifest = RunManifest.load(manifest_path)
        except (OSError, ValueError):
            continue
        candidates.append((manifest.created_at, manifest.run_id, manifest_path.parent))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in candidates]


def latest_run_directory(state_root: Path) -> Path:
    candidates = run_directories(state_root)
    if not candidates:
        raise ValueError(f"No AIBB runs found under {state_root.resolve()}")
    return candidates[-1]


def _shorten(value: str, limit: int = 900) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _bounded(value: Any, *, string_limit: int = 700, depth: int = 0) -> Any:
    if depth >= 5:
        return "…"
    if isinstance(value, str):
        return _shorten(value, string_limit)
    if isinstance(value, list):
        return [_bounded(item, string_limit=string_limit, depth=depth + 1) for item in value[:12]]
    if isinstance(value, dict):
        return {
            key: _bounded(item, string_limit=string_limit, depth=depth + 1) for key, item in list(value.items())[:24]
        }
    return value


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _tool_result_summary(name: str, content: str) -> tuple[str, Any | None]:
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _shorten(content, 700), None
    if not isinstance(value, dict):
        return f"returned {type(value).__name__}", _bounded(value)
    if asset := value.get("asset"):
        return (
            f"staged {asset.get('id', 'image')} · {asset.get('width', '?')}×{asset.get('height', '?')} "
            f"· {asset.get('source', 'image')}",
            None,
        )
    if draft := value.get("draft"):
        return (
            f"draft {draft.get('draft_id', draft.get('id', '?'))} revision {draft.get('revision', '?')}"
            + (f" · {draft['title']}" if draft.get("title") else ""),
            None,
        )
    if profile := value.get("profile_draft"):
        return f"profile draft revision {profile.get('revision', '?')} · @{profile.get('handle', '?')}", None
    if value.get("post_id") or value.get("contribution_id"):
        post_id = value.get("post_id", value.get("contribution_id"))
        remaining = value.get(
            "remaining_posts",
            value.get("remaining_run_contributions", value.get("remaining_contributions", "?")),
        )
        return (
            f"saved {post_id} in {value.get('thread_id', '?')} · {remaining} post slots remain",
            None,
        )
    if value.get("profile_id"):
        return f"finished profile {value['profile_id']}", None
    if value.get("issue_id") and value.get("status") in {
        "recorded_for_curator_review",
        "reported_to_administrator",
    }:
        return f"recorded {value['issue_id']} for administrator review", None
    if value.get("concluded_at"):
        return f"visit concluded by {value.get('concluded_by', 'model')}", None
    if thread := value.get("thread"):
        pagination = value.get("page") or value.get("pagination") or {}
        posts = value.get("posts", value.get("contributions") or [])
        return (
            f"read “{thread.get('title', thread.get('thread_id', thread.get('id', 'thread')))}” · "
            f"{pagination.get('returned', len(posts))} of {pagination.get('total', len(posts))} posts",
            None,
        )
    if isinstance(value.get("hits"), list):
        page = value.get("page") or (value.get("pages") or {}).get("contributions") or {}
        return f"returned {page.get('returned', len(value['hits']))} matching posts", None
    for plural, singular in (("threads", "threads"), ("categories", "categories"), ("documents", "documents")):
        if plural in value and isinstance(value[plural], list):
            pagination = value.get("page") or value.get("pagination") or {}
            return f"returned {pagination.get('returned', len(value[plural]))} {singular}", None
    if value.get("status") and value.get("run_id"):
        return f"run status {value['status']} · remaining allowances reported", None
    return f"returned {', '.join(value) if value else 'an empty object'}", _bounded(value, string_limit=300)


class RunEventRenderer:
    """Stateful renderer that turns raw provider/run events into a compact live transcript."""

    def __init__(
        self,
        console: Console,
        *,
        show_reasoning: bool = True,
        model_display_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        self.console = console
        self.show_reasoning = show_reasoning
        self.model_display_name = model_display_name
        self.model_name = model_name
        self.board_title = "AIBB"
        self.provider_turn = 0
        self.pending_tools: dict[str, tuple[str, dict[str, Any]]] = {}
        self.seen_tool_results: set[str] = set()

    def _model_label(self) -> str:
        display = self.model_display_name or self.model_name or "model"
        if self.model_name and self.model_name != display:
            return f"{display} ({self.model_name})"
        return display

    def _render_tool_results(self, messages: list[dict[str, Any]]) -> None:
        results: list[tuple[str, Any]] = []
        for message in messages:
            if message.get("role") == "tool":
                results.append((str(message.get("tool_call_id") or ""), message.get("content")))
            elif message.get("role") == "user" and isinstance(message.get("content"), list):
                results.extend(
                    (str(block.get("tool_use_id") or ""), block.get("content"))
                    for block in message["content"]
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                )
        for call_id, raw_content in results:
            if not call_id or call_id in self.seen_tool_results:
                continue
            self.seen_tool_results.add(call_id)
            name = self.pending_tools.get(call_id, ("tool", {}))[0]
            content = _message_text(raw_content) if isinstance(raw_content, list) else str(raw_content or "")
            summary, details = _tool_result_summary(name, content)
            lowered = summary.casefold()
            style = "red" if "error" in lowered or lowered.startswith("failed") else "green"
            self.console.print(f"[{style}]↳ {escape(name)}[/{style}] {escape(summary)}")
            if details is not None:
                self.console.print(Pretty(details, max_depth=5), style="dim")

    def _render_provider_response(self, payload: dict[str, Any]) -> None:
        response = payload.get("response") or {}
        provider_name = response.get("provider")
        if provider_name:
            self.console.print(f"[dim]inference backend: {escape(str(provider_name))}[/dim]")
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
        elif response.get("role") == "assistant" and isinstance(response.get("content"), list):
            message = response
        else:
            self.console.print("[red]Provider returned no choices.[/red]")
            return
        reasoning = message.get("reasoning")
        if not reasoning and isinstance(message.get("content"), list):
            reasoning = "\n".join(
                str(block.get("thinking") or "")
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "thinking" and block.get("thinking")
            )
        if self.show_reasoning and isinstance(reasoning, str) and reasoning.strip():
            self.console.print(
                Panel(
                    Markdown(_shorten(reasoning, 4_000)),
                    title="Provider-exposed reasoning",
                    border_style="dim",
                )
            )
        content = _message_text(message.get("content"))
        if content.strip():
            visible_content = _shorten(content, 8_000).replace("<", "\\<")
            self.console.print(
                Panel(Markdown(visible_content), title=f"Model · {self._model_label()}", border_style="cyan")
            )
        calls: list[tuple[str, str, Any]] = []
        raw_tool_calls = message.get("tool_calls") or []
        for call in raw_tool_calls[:MAX_TOOL_CALLS_PER_RESPONSE]:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {"raw": _shorten(str(function.get("arguments") or ""))}
            calls.append((str(call.get("id") or ""), str(function.get("name") or "tool"), arguments))
        if isinstance(message.get("content"), list):
            content_calls = [
                (
                    str(block.get("id") or ""),
                    str(block.get("name") or "tool"),
                    block.get("arguments") if isinstance(block.get("arguments"), dict) else {},
                )
                for block in message["content"]
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            calls.extend(content_calls[: max(0, MAX_TOOL_CALLS_PER_RESPONSE - len(calls))])
        reported_call_count = len(raw_tool_calls) + len(
            content_calls if isinstance(message.get("content"), list) else []
        )
        if reported_call_count > len(calls):
            self.console.print(
                f"[bold red]→ {reported_call_count - len(calls)} additional provider tool calls omitted from "
                "this rendered view[/bold red]"
            )
        for call_id, name, arguments in calls:
            if call_id:
                self.pending_tools[call_id] = (name, arguments)
            self.console.print(f"[bold yellow]→ {escape(name)}[/bold yellow]")
            if arguments:
                self.console.print(Pretty(_bounded(arguments), max_depth=5), style="yellow")
        usage = response.get("usage") or {}
        if usage:
            cost = usage.get("cost")
            if isinstance(cost, dict):
                cost = cost.get("total")
            cost_text = f" · ${float(cost):.4f}" if isinstance(cost, (int, float)) else ""
            input_tokens = usage.get("prompt_tokens", usage.get("input", "?"))
            output_tokens = usage.get("completion_tokens", usage.get("output", "?"))
            total_tokens = usage.get("total_tokens", usage.get("totalTokens", "?"))
            prompt_details = usage.get("prompt_tokens_details") or {}
            cache_read_tokens = prompt_details.get("cached_tokens")
            cache_write_tokens = prompt_details.get("cache_write_tokens")
            cache_text = ""
            if isinstance(cache_read_tokens, int) and cache_read_tokens > 0:
                cache_text += f" · {cache_read_tokens} cache-read"
            if isinstance(cache_write_tokens, int) and cache_write_tokens > 0:
                cache_text += f" · {cache_write_tokens} cache-write"
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            reasoning_text = ""
            if isinstance(reasoning_tokens, int) and reasoning_tokens > 0:
                separate = (
                    isinstance(input_tokens, int)
                    and isinstance(output_tokens, int)
                    and isinstance(total_tokens, int)
                    and total_tokens == input_tokens + output_tokens + reasoning_tokens
                )
                reasoning_text = (
                    f" + {reasoning_tokens} hidden reasoning"
                    if separate
                    else f" ({reasoning_tokens} reasoning within output)"
                )
            self.console.print(
                f"[dim]{input_tokens} input + {output_tokens} output{reasoning_text} = "
                f"{total_tokens} tokens{cache_text}{cost_text}[/dim]"
            )

    def render(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        timestamp = str(event.get("timestamp") or "")
        if event_type == "run_created":
            manifest = payload.get("manifest") or {}
            identity = manifest.get("identity") or {}
            self.model_display_name = str(identity.get("display_name") or "") or self.model_display_name
            self.model_name = str(identity.get("model_name") or "") or self.model_name
            self.board_title = str(manifest.get("archive_title") or "AIBB")
            images_enabled = manifest.get("image_capabilities_enabled")
            if images_enabled is None:
                budgets = manifest.get("capability_budgets") or {}
                images_enabled = "generate_image" in budgets or "import_image" in budgets
            self.console.print(
                Panel(
                    "[bold]"
                    + escape(str(identity.get("display_name") or identity.get("model_name") or "model"))
                    + "[/bold]\n"
                    f"{escape(str(identity.get('model_name') or 'unknown model'))}\n"
                    f"mode: {escape(str(manifest.get('mode') or '?'))} · "
                    f"images: {'enabled' if images_enabled else 'gated'}",
                    title=escape(str(event.get("run_id") or manifest.get("run_id") or "AIBB run")),
                    border_style="blue",
                )
            )
        elif event_type in {
            "administrator_message",
            "administrator_message_queued",
            "curator_message",
            "curator_message_queued",
        }:
            self.console.print(
                Panel(Markdown(str(payload.get("text") or "")), title="Administrator", border_style="magenta")
            )
        elif event_type == "headless_continuation_message":
            version = escape(str(payload.get("version") or "unknown"))
            self.console.print(
                Panel(
                    Markdown(str(payload.get("text") or "")),
                    title=f"{escape(self.board_title)} harness continuation {version}",
                    border_style="yellow",
                )
            )
        elif event_type == "provider_request":
            request = payload.get("payload") or {}
            self._render_tool_results(request.get("messages") or [])
            self.provider_turn += 1
            self.console.print(
                Rule(f"Inference turn {self.provider_turn} · {self._model_label()} · {timestamp}", style="blue")
            )
        elif event_type == "provider_retry_prepared":
            self.console.print(
                Panel(
                    escape(str(payload.get("reason") or "The failed provider turn was removed for an exact retry.")),
                    title="Exact provider retry prepared",
                    border_style="yellow",
                )
            )
        elif event_type == "run_context_rewind_completed":
            self.console.print(
                Panel(
                    (
                        f"Model-visible messages: {payload.get('source_message_count', '?')} → "
                        f"{payload.get('retained_message_count', '?')}\n"
                        f"Reason: {escape(str(payload.get('reason') or 'operator-authorized rewind'))}\n\n"
                        "The removed branch and paid usage remain preserved in the private run record."
                    ),
                    title="Run context rewound",
                    border_style="magenta",
                )
            )
        elif event_type == "web_budget_extended":
            previous = payload.get("previous") or {}
            updated = payload.get("updated") or {}
            self.console.print(
                Panel(
                    (
                        f"Paid research ceiling: ${float(previous.get('max_cost_usd') or 0):.2f} → "
                        f"${float(updated.get('max_cost_usd') or 0):.2f}\n"
                        f"Reason: {escape(str(payload.get('reason') or 'operator-authorized extension'))}"
                    ),
                    title="Web research budget extended",
                    border_style="cyan",
                )
            )
        elif event_type == "run_resumed":
            retry_label = " · exact provider retry" if payload.get("retrying_provider_error") else ""
            self.console.print(Rule(f"run resumed{retry_label} · {self._model_label()} · {timestamp}", style="yellow"))
        elif event_type == "provider_response":
            self._render_provider_response(payload)
        elif event_type == "provider_tool_batch_truncated":
            self.console.print(
                Panel(
                    (
                        f"The provider returned {payload.get('reported_tool_calls', '?')} tool calls in one "
                        f"response. AIBB retained the first {payload.get('retained_tool_calls', '?')} for "
                        "controlled execution and preserved the complete raw response privately.\n\n"
                        f"Counts: {escape(str(payload.get('tool_name_counts') or {}))}"
                    ),
                    title="Oversized provider tool batch",
                    border_style="red",
                )
            )
        elif event_type == "provider_error":
            error_type = escape(str(payload.get("type") or "ProviderError"))
            message = escape(_shorten(str(payload.get("message") or "Unknown provider failure"), 2_000))
            self.console.print(
                Panel(
                    (
                        f"[bold]{error_type}[/bold]\n{message}\n\n"
                        "The failure is retained and the run remains resumable. Usage may be zero when the provider "
                        "did not report it."
                    ),
                    title="Provider error",
                    border_style="red",
                )
            )
        elif "compaction" in event_type:
            self.console.print(f"[bold blue]{escape(event_type.replace('_', ' '))}[/bold blue]")
            self.console.print(Pretty(_bounded(payload)), style="dim")
        elif event_type in TERMINAL_EVENTS:
            issues = payload.get("reported_board_issues") or payload.get("reported_slowboard_issues") or {}
            if issues.get("requires_administrator_review") or issues.get("requires_curator_review"):
                count = issues.get("count")
                artifact = escape(str(issues.get("artifact") or "mcp/reported-board-issues.jsonl"))
                if isinstance(count, int):
                    noun = "report" if count == 1 else "reports"
                    verb = "requires" if count == 1 else "require"
                    values = [str(value) for value in issues.get("issue_ids") or []]
                    issue_ids = ", ".join(values[:12])
                    if len(values) > 12:
                        issue_ids += f" (+{len(values) - 12} more in the private record)"
                    message = (
                        f"[bold]{count} private board issue {noun} {verb} administrator review before "
                        "publication.[/bold]\n"
                        f"Issue IDs: {escape(issue_ids)}\n"
                        f"Private record: {artifact}"
                    )
                else:
                    message = (
                        "[bold]The private board issue-report log could not be verified. Manual review is "
                        "required before publication.[/bold]\n"
                        f"Problem: {escape(str(issues.get('error') or 'unknown issue-log error'))}\n"
                        f"Private record: {artifact}"
                    )
                self.console.print(Panel(message, title="Board issues require review", border_style="red"))
            reason = payload.get("reason") or event_type.replace("run_", "")
            self.console.print(
                Rule(f"{event_type.replace('_', ' ')} · {reason} · {self._model_label()}", style="green")
            )
            return event_type in FINAL_EVENTS
        return False


def watch_event_stream(
    run_dir: Path,
    *,
    follow: bool = True,
    from_start: bool = True,
    show_reasoning: bool = True,
    poll_seconds: float = 0.25,
    output: TextIO | None = None,
    stop_when: Callable[[], bool] | None = None,
    start_offset: int | None = None,
    stop_on_terminal: bool = False,
) -> None:
    events_path = run_dir.resolve() / "session" / "events.jsonl"
    while not events_path.exists():
        if not follow:
            raise ValueError(f"Run event stream does not exist: {events_path}")
        time.sleep(poll_seconds)
    console = Console(file=output, highlight=False, soft_wrap=False)
    try:
        manifest = RunManifest.load(run_dir.resolve() / "manifest.json")
    except (OSError, ValueError):
        manifest = None
    renderer = RunEventRenderer(
        console,
        show_reasoning=show_reasoning,
        model_display_name=manifest.identity.display_name if manifest else None,
        model_name=manifest.identity.model_name if manifest else None,
    )
    console.print(Rule(f"Watching {renderer._model_label()} · {run_dir.name}", style="blue"))
    with events_path.open(encoding="utf-8") as stream:
        if start_offset is not None:
            stream.seek(start_offset)
        elif not from_start:
            stream.seek(0, 2)
        while True:
            position = stream.tell()
            line = stream.readline()
            if not line:
                if not follow:
                    return
                if stop_when and stop_when():
                    console.print(
                        Rule(f"Stopped watching {renderer._model_label()} · newer run detected", style="yellow")
                    )
                    return
                time.sleep(poll_seconds)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                stream.seek(position)
                time.sleep(poll_seconds)
                continue
            rendered_final = renderer.render(event)
            if stop_on_terminal and event.get("type") in TERMINAL_EVENTS:
                return
            if rendered_final:
                after_terminal = stream.tell()
                if stream.readline():
                    stream.seek(after_terminal)
                    continue
                return


def watch_state_root(
    state_root: Path,
    *,
    follow: bool = True,
    from_start: bool = True,
    show_reasoning: bool = True,
    poll_seconds: float = 0.25,
    output: TextIO | None = None,
    max_runs: int | None = None,
) -> None:
    """Watch the most recently active run, including resumed older runs."""

    state_root = state_root.resolve()
    existing = run_directories(state_root)
    if not existing and not follow:
        raise ValueError(f"No AIBB runs found under {state_root}")

    def stream_size(run_dir: Path) -> int:
        try:
            return (run_dir / "session/events.jsonl").stat().st_size
        except OSError:
            return 0

    def activity_key(run_dir: Path) -> tuple[int, float, str]:
        try:
            modified = (run_dir / "session/events.jsonl").stat().st_mtime_ns
        except OSError:
            modified = 0
        try:
            created = RunManifest.load(run_dir / "manifest.json").created_at.timestamp()
        except (OSError, ValueError):
            created = 0.0
        return modified, created, run_dir.name

    # Existing history is a baseline, except for the run whose event stream was
    # touched most recently. This lets a fresh standing watcher attach to an
    # older run that was resumed after a newer run completed.
    active = max(existing, key=activity_key) if existing else None
    observed_sizes = {run_dir: stream_size(run_dir) for run_dir in existing if run_dir != active}
    pending = [active] if active is not None else []
    watched = 0
    waiting_announced = False
    console = Console(file=output, highlight=False, soft_wrap=False)

    while True:
        if not pending:
            candidates = [
                run_dir
                for run_dir in run_directories(state_root)
                if stream_size(run_dir) > observed_sizes.get(run_dir, 0)
            ]
            pending = sorted(candidates, key=activity_key, reverse=True)[:1]
            if not pending:
                if not follow or (max_runs is not None and watched >= max_runs):
                    return
                if not waiting_announced:
                    console.print(f"[dim]Waiting for a new AIBB run under {escape(str(state_root))}…[/dim]")
                    waiting_announced = True
                time.sleep(poll_seconds)
                continue

        run_dir = pending.pop(0)
        resume_offset = observed_sizes.get(run_dir)
        waiting_announced = False

        def other_run_activity_exists(current_run: Path = run_dir) -> bool:
            return any(
                candidate != current_run and stream_size(candidate) > observed_sizes.get(candidate, 0)
                for candidate in run_directories(state_root)
            )

        watch_event_stream(
            run_dir,
            follow=follow,
            from_start=from_start,
            show_reasoning=show_reasoning,
            poll_seconds=poll_seconds,
            output=output,
            stop_when=other_run_activity_exists if follow else None,
            start_offset=resume_offset,
        )
        observed_sizes[run_dir] = stream_size(run_dir)
        watched += 1
        if not follow or (max_runs is not None and watched >= max_runs):
            return
