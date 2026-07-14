# Contributing

Keep changes small, explicit, and verifiable.

Before pushing:

1. Run `make check`, then check any additional touched code path directly.
2. Inspect generated outputs when the change affects data, manifests, parquet, or figures.
3. Do not delete data, databases, outputs, caches, or virtual environments as cleanup.
4. Keep `AGENTS.md` linked to the shared `agent-config/AGENTS.md`.
5. Keep current code and results out of `old/`; keep standalone reference
   documents, tables, and images under `artifacts/`.
6. Do not inspect `old/` or `artifacts/` unless the task explicitly requires it.

Prefer existing project and submodule functionality over parallel helper code.
