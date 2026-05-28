# Next Agent Notes

These notes are only for quick orientation. Follow `AGENTS.md` first.

## Current Scope

- Active scenario: full-year `2024` data collection for the equatorial `700 km` belt
- Active analysis target: crop accessibility change
- Legacy materials were moved under `old/`

## Active Code Path

- Fetch launcher: `scripts/fetch_equator_700km_full_year_data.sh`
- Overlay builder: `src/data/run_multisource_road_overlay.py`
- Crop candidate builder: `src/data/build_spam_crop_top_origins.py`
- Baseline-connected selector: `src/data/select_baseline_connected_crop_origins.py`
- Accessibility engine: `src/data/run_weekly_accessibility_dijkstra.py`
- Orchestrator only: `src/data/run_road_hazard_overnight_worker.py`

## Important Shape

- `run_road_hazard_overnight_worker.py` should stay a thin orchestrator.
- Baseline-connected crop-origin logic now lives in its own script and should not be re-inlined into the worker.
- `run_weekly_accessibility_pandana.py` is not the preferred runner anymore.
  Current code still imports shared helper functions from it.
- `run_road_monthly_scenarios.py` is still used as a helper source for country/city loading.

## Outputs To Inspect

- Overlay: `outputs/road_multisource_overlay/<ISO3>/<period>/`
- Crop candidates: `outputs/road_weekly_scenarios/<ISO3>/origins_spam_top<N>_by_crop_candidates/`
- Baseline-connected origins: `outputs/road_weekly_scenarios/<ISO3>/origins_spam_top<N>_by_crop_baseline_connected/`
- Accessibility results: `outputs/road_weekly_scenarios/<ISO3>/<period>_crop_connected_visibility_speed_dijkstra/`

## Verification Habit

- Do not trust worker success alone.
- Inspect:
  - `summary.json` / `overnight_worker_summary.json`
  - origin selection CSV/GPKG outputs
  - weekly accessibility CSV summaries
  - generated PNG plots

## Legacy

- Old `sep_nov` launchers, flood-depth / single-snapshot OD experiments, Bolivia/sample materials, and research spreadsheets/CSVs are under `old/`.
