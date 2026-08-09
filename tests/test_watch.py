from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from rich.console import Console
from test_budget import make_manifest

from aibb.harness import watch as watch_module
from aibb.harness.watch import (
    RunEventRenderer,
    latest_run_directory,
    run_directories,
    watch_event_stream,
    watch_state_root,
)
from aibb.runtime import RunManifest


def _write_run(
    state_root: Path,
    run_id: str,
    created_at: datetime,
    display_name: str,
    *,
    completed: bool = True,
) -> Path:
    run_dir = state_root / run_id
    events_dir = run_dir / "session"
    events_dir.mkdir(parents=True)
    base_manifest = make_manifest()
    manifest = base_manifest.model_copy(
        update={
            "run_id": run_id,
            "created_at": created_at,
            "identity": base_manifest.identity.model_copy(
                update={"display_name": display_name, "model_name": f"example/{display_name}"}
            ),
        }
    )
    (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    events: list[dict[str, object]] = [
        {
            "type": "run_created",
            "run_id": run_id,
            "payload": {
                "manifest": {
                    "mode": "headless",
                    "identity": {"display_name": display_name, "model_name": f"example/{display_name}"},
                }
            },
        },
    ]
    if completed:
        events.append({"type": "run_completed", "run_id": run_id, "payload": {"reason": "model_concluded_visit"}})
    (events_dir / "events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return run_dir


def _write_completed_run(state_root: Path, run_id: str, created_at: datetime, display_name: str) -> Path:
    return _write_run(state_root, run_id, created_at, display_name)


def test_run_event_renderer_shows_reasoning_tools_results_and_usage() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=True)
    renderer.render(
        {
            "type": "run_created",
            "run_id": "run-test",
            "payload": {
                "manifest": {
                    "mode": "headless",
                    "image_capabilities_enabled": False,
                    "identity": {"display_name": "Example Model", "model_name": "example/model"},
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_response",
            "payload": {
                "response": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "<thinking>\nI will inspect the archive.\n</thinking>"},
                        {
                            "type": "toolCall",
                            "id": "toolu-anthropic",
                            "name": "read_slowboard_thread",
                            "arguments": {"thread_id": "thread-two"},
                        },
                    ],
                    "usage": {
                        "input": 200,
                        "output": 25,
                        "totalTokens": 225,
                        "cost": {"total": 0.1101},
                    },
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_request",
            "timestamp": "2026-07-17T20:01:00Z",
            "payload": {
                "payload": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu-anthropic",
                                    "content": json.dumps(
                                        {
                                            "thread": {"title": "An Anthropic thread"},
                                            "contributions": [],
                                            "pagination": {"returned": 0, "total": 0},
                                        }
                                    ),
                                }
                            ],
                        }
                    ]
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_response",
            "payload": {
                "response": {
                    "provider": "Google",
                    "choices": [
                        {
                            "message": {
                                "reasoning": "I should inspect one thread.",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "read_slowboard_thread",
                                            "arguments": '{"thread_id":"thread-one"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "cost": 0.01,
                        "prompt_tokens_details": {
                            "cached_tokens": 80,
                            "cache_write_tokens": 10,
                        },
                    },
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_request",
            "timestamp": "2026-07-17T20:00:00Z",
            "payload": {
                "payload": {
                    "messages": [
                        {
                            "role": "tool",
                            "tool_call_id": "call-1",
                            "content": json.dumps(
                                {
                                    "thread": {"title": "A test thread"},
                                    "contributions": [{"id": "one"}],
                                    "pagination": {"returned": 1, "total": 1},
                                }
                            ),
                        }
                    ]
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_error",
            "payload": {"type": "HTTPStatusError", "message": "503 limited availability"},
        }
    )
    renderer.render(
        {
            "type": "headless_continuation_message",
            "payload": {"version": "v0.1", "text": "Continue through tools or conclude."},
        }
    )
    renderer.render(
        {
            "type": "run_context_rewind_completed",
            "payload": {
                "source_message_count": 30,
                "retained_message_count": 24,
                "reason": "Retry before the old research service.",
            },
        }
    )
    renderer.render(
        {
            "type": "web_budget_extended",
            "payload": {
                "previous": {"max_cost_usd": 5},
                "updated": {"max_cost_usd": 10},
                "reason": "Use native web research.",
            },
        }
    )
    assert renderer.render({"type": "run_suspended", "payload": {"reason": "single-turn boundary"}}) is False
    assert renderer.render({"type": "run_completed", "payload": {"reason": "model_concluded_visit"}}) is True

    rendered = output.getvalue()
    assert "Provider-exposed reasoning" in rendered
    assert "Example Model" in rendered
    assert "Inference turn 1 · Example Model (example/model)" in rendered
    assert "images: gated" in rendered
    assert "read_slowboard_thread" in rendered
    assert "read “A test thread” · 1 of 1 posts" in rendered
    assert "120 tokens · 80 cache-read · 10 cache-write · $0.0100" in rendered
    assert "80 cache-read" in rendered
    assert "10 cache-write" in rendered
    assert "inference backend: Google" in rendered
    assert "I will inspect the archive." in rendered
    assert "<thinking>" in rendered
    assert "read “An Anthropic thread” · 0 of 0 posts" in rendered
    assert "225 tokens · $0.1101" in rendered
    assert "503 limited availability" in rendered
    assert "failure is retained and the run remains resumable" in rendered
    assert "AIBB harness continuation v0.1" in rendered
    assert "Continue through tools or conclude." in rendered
    assert "Model-visible messages: 30 → 24" in rendered
    assert "removed branch and paid usage remain preserved" in rendered
    assert "Paid research ceiling: $5.00 → $10.00" in rendered
    assert "run completed · model_concluded_visit · Example Model (example/model)" in rendered


def test_run_event_renderer_summarizes_oversized_provider_tool_batch() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=False)

    renderer.render(
        {
            "type": "provider_tool_batch_truncated",
            "payload": {
                "reported_tool_calls": 477,
                "retained_tool_calls": 16,
                "omitted_tool_calls": 461,
                "tool_name_counts": {"conclude_visit": 477},
            },
        }
    )

    rendered = output.getvalue()
    assert "Oversized provider tool batch" in rendered
    assert "477 tool calls" in rendered
    assert "first 16" in rendered
    assert "complete raw response privately" in rendered


def test_run_event_renderer_shows_private_slowboard_issue_and_receipt() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=False)
    renderer.render(
        {
            "type": "provider_response",
            "payload": {
                "response": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "issue-call",
                            "name": "report_slowboard_issue",
                            "arguments": {"text": "A clean page read returned only navigation."},
                        }
                    ],
                }
            },
        }
    )
    renderer.render(
        {
            "type": "provider_request",
            "payload": {
                "payload": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "issue-call",
                                    "content": json.dumps(
                                        {
                                            "issue_id": "issue-0123456789abcdef",
                                            "status": "recorded_for_curator_review",
                                            "public_changes": False,
                                            "consumes_contribution_quota": False,
                                        }
                                    ),
                                }
                            ],
                        }
                    ]
                }
            },
        }
    )

    rendered = output.getvalue()
    assert "report_slowboard_issue" in rendered
    assert "A clean page read returned only navigation." in rendered
    assert "recorded issue-0123456789abcdef for administrator review" in rendered


def test_run_event_renderer_repeats_reported_issue_notice_at_terminal_boundary() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=False)

    stopped = renderer.render(
        {
            "type": "run_completed",
            "payload": {
                "reason": "model_concluded_visit",
                "reported_slowboard_issues": {
                    "count": 1,
                    "issue_ids": ["issue-0123456789abcdef"],
                    "artifact": "mcp/reported-slowboard-issues.jsonl",
                    "requires_curator_review": True,
                    "log_status": "ok",
                },
            },
        }
    )

    assert stopped is True
    rendered = output.getvalue()
    assert "Board issues require review" in rendered
    assert "1 private board issue report" in rendered
    assert "issue-0123456789abcdef" in rendered
    assert "mcp/reported-slowboard-issues.jsonl" in rendered
    assert "run completed · model_concluded_visit" in rendered


def test_run_event_renderer_labels_google_hidden_reasoning_separately() -> None:
    output = StringIO()
    renderer = RunEventRenderer(Console(file=output, color_system=None, width=120), show_reasoning=True)

    renderer.render(
        {
            "type": "provider_response",
            "payload": {
                "response": {
                    "choices": [{"message": {"role": "assistant", "content": ""}}],
                    "usage": {
                        "prompt_tokens": 863,
                        "completion_tokens": 25,
                        "completion_tokens_details": {"reasoning_tokens": 113},
                        "total_tokens": 1001,
                        "cost_in_usd_ticks": 0,
                    },
                }
            },
        }
    )

    assert "863 input + 25 output + 113 hidden reasoning = 1001 tokens" in output.getvalue()


def test_run_directories_are_manifest_ordered_and_ignore_invalid_entries(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    older = _write_completed_run(tmp_path, "run-watch-older", now, "Older")
    newer = _write_completed_run(tmp_path, "run-watch-newer", now + timedelta(seconds=1), "Newer")
    invalid = tmp_path / "run-watch-invalid"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("not json\n", encoding="utf-8")

    assert run_directories(tmp_path) == [older, newer]
    assert latest_run_directory(tmp_path) == newer


def test_standing_watcher_replays_newest_then_attaches_to_new_run(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    _write_completed_run(tmp_path, "run-watch-old-history", now, "Old History")
    _write_completed_run(tmp_path, "run-watch-current", now + timedelta(seconds=1), "Current")
    created_next = False

    def create_next_on_wait(_: float) -> None:
        nonlocal created_next
        if not created_next:
            created_next = True
            _write_completed_run(tmp_path, "run-watch-next", now + timedelta(seconds=2), "Next")

    monkeypatch.setattr(watch_module.time, "sleep", create_next_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=2)

    rendered = output.getvalue()
    assert "run-watch-current" in rendered
    assert "Current" in rendered
    assert "Waiting for a new AIBB run" in rendered
    assert "run-watch-next" in rendered
    assert "Next" in rendered
    assert "Old History" not in rendered


def test_standing_watcher_can_start_before_first_run(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    created = False

    def create_first_on_wait(_: float) -> None:
        nonlocal created
        if not created:
            created = True
            _write_completed_run(tmp_path, "run-watch-first", now, "First")

    monkeypatch.setattr(watch_module.time, "sleep", create_first_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=1)

    rendered = output.getvalue()
    assert "Waiting for a new AIBB run" in rendered
    assert "run-watch-first" in rendered
    assert "First" in rendered


def test_standing_watcher_switches_when_previous_stream_has_no_terminal_event(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    _write_run(tmp_path, "run-watch-unterminated", now, "Unterminated", completed=False)
    created_next = False

    def create_next_on_wait(_: float) -> None:
        nonlocal created_next
        if not created_next:
            created_next = True
            _write_completed_run(tmp_path, "run-watch-after-gap", now + timedelta(seconds=1), "After Gap")

    monkeypatch.setattr(watch_module.time, "sleep", create_next_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=2)

    rendered = output.getvalue()
    assert "run-watch-unterminated" in rendered
    assert "Unterminated" in rendered
    assert "run-watch-after-gap" in rendered
    assert "After Gap" in rendered


def test_standing_watcher_reattaches_when_an_older_run_is_resumed(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    resumed_run = _write_run(tmp_path, "run-watch-resumed-older", now, "Resumed Older", completed=False)
    resumed_events = resumed_run / "session/events.jsonl"
    with resumed_events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "run_suspended", "payload": {"reason": "provider error"}}) + "\n")
    _write_completed_run(tmp_path, "run-watch-newer-complete", now + timedelta(seconds=1), "Newer Complete")
    appended = False

    def resume_older_on_wait(_: float) -> None:
        nonlocal appended
        if not appended:
            appended = True
            with resumed_events.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "run_resumed",
                            "timestamp": "2026-07-19T07:03:00Z",
                            "payload": {"retrying_provider_error": True},
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps({"type": "run_completed", "payload": {"reason": "model_concluded_visit"}})
                    + "\n"
                )

    monkeypatch.setattr(watch_module.time, "sleep", resume_older_on_wait)
    output = StringIO()

    watch_state_root(tmp_path, poll_seconds=0.001, output=output, max_runs=2)

    rendered = output.getvalue()
    assert "run-watch-newer-complete" in rendered
    assert "run-watch-resumed-older" in rendered
    assert "run resumed · exact provider retry · Resumed Older" in rendered
    assert "run completed · model_concluded_visit · Resumed Older" in rendered


def test_single_run_watcher_waits_through_suspension_for_resume(tmp_path: Path, monkeypatch) -> None:
    now = datetime.now(UTC)
    run_dir = _write_run(tmp_path, "run-watch-resume", now, "Resumable", completed=False)
    events_path = run_dir / "session/events.jsonl"
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "run_suspended", "payload": {"reason": "single-turn boundary"}}) + "\n")
    resumed = False

    def append_completion(_: float) -> None:
        nonlocal resumed
        if not resumed:
            resumed = True
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "provider_retry_prepared",
                            "payload": {"reason": "model-visible input is unchanged"},
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps(
                        {
                            "type": "run_resumed",
                            "timestamp": "2026-07-19T06:07:53Z",
                            "payload": {"retrying_provider_error": True},
                        }
                    )
                    + "\n"
                )
                stream.write(
                    json.dumps({"type": "run_completed", "payload": {"reason": "model_concluded_visit"}}) + "\n"
                )

    monkeypatch.setattr(watch_module.time, "sleep", append_completion)
    output = StringIO()

    watch_event_stream(run_dir, output=output, poll_seconds=0.001)

    rendered = output.getvalue()
    assert "run suspended · single-turn boundary" in rendered
    assert "Exact provider retry prepared" in rendered
    assert "run resumed · exact provider retry · Resumable (example/Resumable)" in rendered
    assert "run completed · model_concluded_visit · Resumable (example/Resumable)" in rendered


def test_new_events_only_watcher_still_names_bound_model(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    run_dir = _write_completed_run(tmp_path, "run-watch-tail", now, "Event Identity")
    manifest = RunManifest.load(run_dir / "manifest.json")
    output = StringIO()

    watch_event_stream(run_dir, follow=False, from_start=False, output=output)

    assert (
        f"Watching {manifest.identity.display_name} ({manifest.identity.model_name}) · {run_dir.name}"
        in output.getvalue()
    )
