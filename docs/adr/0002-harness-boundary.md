# ADR 0002: Harn core behind an AIBB-owned harness boundary

Status: accepted for the initial implementation

Use the low-level `harn_agent.Agent` tool loop and event types behind `AibbHarnessEngine`. AIBB supplies the exact board-selected prompt, restored model-visible messages, MCP-backed tools, provider `streamFn`, retry behavior, and canonical session log.

Do not launch the Harn coding-agent CLI or load its settings, filesystem tools, context files, skills, extensions, prompt templates, automatic compaction, or session lifecycle. Interactive presentation may reuse `harn_tui` components. Compaction is an explicit AIBB context transition whose source and result remain recorded.

If a future dependency upgrade breaks exact provider payloads, tool isolation, event fidelity, or faithful reconstruction and a thin upstream-compatible fix is not possible, test the same boundary against low-level Pi before writing a custom agent loop.

The 2026-07-17 spike passed against pinned `harn-agent==0.1.0`: an exact system prompt and one allowlisted tool reached a faux provider despite hostile global/project Harn context files; the tool crossed a real MCP stdio subprocess; tool and message events were observable; an operator message queued during tool execution arrived at the next boundary; and a serialized, hash-bound checkpoint reconstructed the same model-visible messages and opaque provider-state envelope. Harn performed no extra provider turn or automatic compaction.

This accepts Harn core, not every Harn provider implementation. Each real endpoint adapter must separately prove raw response and continuation-state fidelity.
