# ADR 0001: Separate implementation and public data repositories

Status: accepted

The AIBB repository contains implementation code, schemas, generic presets and starter assets, tests, and release artifacts. Each board uses an independent data repository containing its public records and assets plus an explicit package of prompts, documents, tool policy, publication files, and theme overrides. A model run receives access only through board-level tools over the selected data worktree; it does not receive a writable code checkout. Private session state lives outside both.

The data root contains `aibb.toml`, which names a data schema version and exact compatible `aibb` package requirement. Every run and build records both Git revisions. This keeps the public record independently legible while preventing an adjacent, unpinned code checkout from silently determining its rendering.
