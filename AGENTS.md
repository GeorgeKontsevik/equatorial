# Equatorial Agent Guide

## Scope
This project is now focused on a single production path:
1. Full-year 2024 fetch for the 700km country list.
2. ERA5 precipitation-only road overlay.
3. Weekly factor boxplots (cell aggregation).

Anything outside this path is legacy and should not be reintroduced without explicit request.

## Canonical Entrypoints
- Fetch: `scripts/fetch_equator_700km_full_year_data.sh`
- Overlay + boxplots batch launcher:
  - `scripts/run_era5_only_overlay_then_boxplots_all.zsh`
- Core modules:
  - `src/data/fetch.py`
  - `src/data/run_multisource_road_overlay.py`
  - `src/data/run_weekly_factor_boxplots_streaming.py`

## Required Runtime
- Use project venv only: `equatorial/.venv/bin/python`
- Run from any directory via absolute script paths when possible.

## Data/Output Conventions
- Overlay output:
  - `outputs/road_multisource_overlay/<ISO3>/2024-01-01_to_2024-12-31_7d/`
- Boxplots output:
  - `outputs/road_weekly_scenarios/<ISO3>/2024_full_year_<RUN_ID>/factor_boxplots_cell/`
- Generated run configs:
  - `config/generated/<RUN_ID>_tmp/`

## Logging Rules
- Prefer compact weekly progress logs.
- Avoid verbose per-factor spam unless debugging.
- Treat warnings/errors as actionable; do not ignore silently.

## Cleanup Policy
- Keep only artifacts relevant to listed countries and current run objective.
- Move stale experiments/legacy runs to Trash, not permanent delete.
- Do not keep duplicate launchers/workflows for the same purpose.

## Change Discipline
- Keep pipeline code short and direct.
- Avoid parallel alternative implementations.
- If adding options, default behavior must stay production-safe and deterministic.
