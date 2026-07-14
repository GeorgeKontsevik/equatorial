# Next Agent Notes

These notes are only for quick orientation. Follow `AGENTS.md` first.

## Current Scope

- Active scenario: full-year `2024` weekly road-penalty analysis for the equatorial `700 km` belt
- Active analysis target: CROPGRIDS crop-cluster accessibility change
- Current result scope: `cluster_connected_allclusters_10small_3large_3ports_3airports`
- Legacy materials were moved under `old/`

## Active Code Path

- Crop candidates: `eq.crop_origin_candidates`, built from CROPGRIDS
- Graph builder: `scripts/build_cluster_connected_graphs.py`
- Accessibility engine: `scripts/run_weekly_astar_accessibility.py`
- Plot renderer: `scripts/render_weekly_astar_accessibility.py`
- Heatmap renderer: `scripts/render_weekly_astar_accessibility_heatmaps.py`
- Full run instructions: `CLUSTER_CONNECTED_ACCESSIBILITY_PIPELINE.md`

## Important Shape

- Use all stored crop-cluster terminals per crop; do not apply top-N filtering.
- Each crop terminal begins as its own graph component.
- Connect road and terminal components iteratively in the `cluster_connected` graph.
- Treat `unpaved_synthetic_line` as unpaved for precipitation penalties.
- Exclude `GNQ` because it has no crop candidates.
- Do not run `BRA` or `IDN` with the current component-connection strategy.

## Outputs To Inspect

- Crop migration audit: `outputs/cropgrids_transition/`
- Graph summaries: `outputs/astar_accessibility_weekly/cluster_connected_graphs/`
- Country plots: `outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_plots/`
- Heatmaps: `outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps/`
- Paper experiments: `outputs/astar_accessibility_weekly/paper_experiment_*/`

## Verification Habit

- Do not trust runner success alone.
- Inspect:
  - graph component counts (expected final count: `1`)
  - weekly row counts and 53-week coverage
  - `route_status` counts (expected `not_ok = 0`)
  - experiment manifests and derived CSV summaries
  - generated PNG plots

## Legacy

- Old `sep_nov` launchers, flood-depth / single-snapshot OD experiments, Bolivia/sample materials, and research spreadsheets/CSVs are under `old/`.
