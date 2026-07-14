# Current Outputs

This directory contains current results. Keep every experiment's manifest,
derived tables, and figures together.

## Final accessibility inputs and diagnostics

- `cropgrids_transition/`: CROPGRIDS candidates and migration manifest.
- `road_weekly_scenarios/`: current road-factor cell statistics, DuckDB counts,
  diagnostics CSVs, summaries, and threshold boxplots used to validate the
  climate/road input layer.
- `astar_accessibility_weekly/cluster_connected_graphs/`: graph build summaries.
- `astar_accessibility_weekly/base_route_surface_mix/`: baseline route composition.
- `astar_accessibility_weekly/route_change_diagnostics/`: route-change diagnostics.
- `astar_accessibility_weekly/visual_experiments/`: shared analysis tables.
  Its manifest includes the current weekly bubble timeline.

## Final results and paper analysis

- `astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps/`:
  final-scope heatmaps.
- `astar_accessibility_weekly/paper_experiment_regimes_v1/`: regime analysis.
- `astar_accessibility_weekly/paper_experiment_rainfall_mechanism_v1/`: rainfall mechanism analysis.
- `astar_accessibility_weekly/paper_experiment_country_mechanism_structural_v1/`:
  current country/crop structural analysis.
- `astar_accessibility_weekly/paper_lbr_precip_grid/` and
  `astar_accessibility_weekly/lbr_two_city_heatmap/`: current LBR chapter figures.

The protected source render
`astar_accessibility_weekly/paper_lbr_precip_grid/lbr_era5_grid_coverage_precip_2024_08_19.png`
has a self-contained renderer and frozen data bundle under
`supporting_materials/lbr_era5_grid_coverage/`. The crop/cluster/road/destination
figure family is indexed in `supporting_materials/LBR_FIGURE_PROVENANCE.md`.

## Canonical study-area map

`equator_country_belt_road_surface_missing_hatched.png` is the current canonical
equatorial-belt road-surface/crop-input status map. Its authoritative and latest
generator is `scripts/render_equator_belt_road_surface_status_map.py`. Files in
thesis directories with cropped, translated, or reformatted names are derived
copies and must not replace this source render.

## Canonical conceptual scheme

`equatorial_network_decomposition.png` is the current English conceptual scheme
for the equatorial crop-logistics network. Its authoritative generator is
`scripts/render_equatorial_network_decomposition.py`. The scheme treats crop
clusters as origins and ports, airports, logistics hubs, and large cities as
destinations. Each crop has its own cluster layer and can therefore enter the
road network from different spatial locations. Its solid production path shows weekly precipitation changing
travel time through surface-sensitive edge penalties. Flood, temperature,
wind/dust, land-surface failure, and precipitation-inferred closures are shown
only as context or model extensions, not as active routing penalties.

Do not infer completeness from directory existence. Inspect manifests, row
counts, 53-week coverage, route status, and final images. Superseded result
variants belong under `old/outputs/`.
