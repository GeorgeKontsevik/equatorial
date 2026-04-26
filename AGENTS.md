# Equatorial Working Rules

This subproject inherits the root repository rules:

- Follow `/Users/gk/Code/super-duper-disser/AGENTS.md` first.
- If local instructions here conflict with the root rules, prefer the stricter verification habit unless the user explicitly says otherwise.

## Local Notes

- Treat `equatorial` outputs as data products, not just script side effects: after important runs, inspect the written CSV/JSON summaries and PNG previews directly.
- For Pandana workflows, use the root Pandana environment when needed:
  - `/Users/gk/Code/super-duper-disser/.venv-pandana`
- Keep threshold/accessibility results reproducible by recording the exact config, origin set, output directory, and period used for each run.
- When reporting numerical results from generated summaries, round user-facing values to one decimal place unless higher precision is specifically requested.
