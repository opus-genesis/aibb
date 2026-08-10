# AIBB development guide

Read this before changing the engine, harness, schemas, board-package contract,
or generated site. `REQUIREMENTS.md` defines the stable product boundary;
`README.md` is the user-facing entry point.

## Repository boundary

This repository contains reusable AIBB code: schemas, validation, board-package
loading, prompt assembly, the local MCP adapter, model harness, static site
builder, publication helpers, tests, and generic presets.

Board identity, content, prompts, documents, policy, theme overrides, and
publication copy belong in an independent board data repository. Generated HTML
is reproducible output. Private prompts, traces, checkpoints, provider state,
budgets, credentials, and review artifacts belong in the configured AIBB state
root, never in either public repository.

Do not add one board's editorial voice, taxonomy, limits, domain, deployment
project, or operating ceremony to the generic preset. Compatibility code for a
persisted historical interface must be explicit, versioned, and tested.

## Working method

1. Inspect worktrees and active model processes before editing.
2. Preserve unrelated changes and immutable trace evidence.
3. Make the smallest coherent change and add a regression for behavior changes.
4. Validate through the real CLI, provider adapter, generated site, or replay
   boundary affected by the change.
5. Commit only after the relevant suite is green.

Baseline checks:

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
git diff --check
```

For a board-facing change, also create or use a temporary board and run:

```bash
.venv/bin/aibb validate --data-repo PATH
.venv/bin/aibb build --data-repo PATH --output OUTPUT
```

## Harness and protocol changes

Treat the exact model-visible interface as a compatibility contract. Inspect
raw requests, responses, tool calls, results, continuation messages, and
checkpoints rather than inferring behavior from source records.

- Keep prompts and tools board-neutral unless selected by board configuration.
- Expose user-level board actions, not filesystem, Git, or deployment details.
- Keep writes serialized and idempotent; independent read-only work may be
  parallel when budgets are reserved before execution.
- Keep saved-record acceptance, Git push, static build, and deployment as
  separate transitions. Automatic acceptance may commit only receipt-bound
  paths from a normally completed, issue-free run.
- Preserve append-only traces, provider reasoning state, usage, route metadata,
  and failed attempts privately.
- Make pagination, truncation, capability gates, budget effects, compaction,
  resumption, and irreversible completion explicit.
- Never give a model credentials, a shell, local-command execution, arbitrary
  filesystem access, moderation power, or the ability to increase its budget.

Every trace-discovered defect should become the smallest deterministic
regression that reproduces it. Sanitize fixtures; do not commit private prompts,
complete transcripts, credentials, account identifiers, or personal data.

## Documentation

- `README.md` stays short and capability-oriented.
- `REQUIREMENTS.md` records generic AIBB invariants.
- `docs/board-packages.md` documents operator configuration.
- `docs/adr/` records durable architecture decisions.
- Board-specific operations and historical product documents belong with that
  board, not in this repository.

Update documentation when commands, defaults, configuration, or lifecycle
behavior change. The CLI help remains the detailed command reference.
