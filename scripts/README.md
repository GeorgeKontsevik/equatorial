# Script Map

Only current and supporting scripts belong in this directory. Superseded and
one-off experiments belong under `old/`.

## Final accessibility workflow

- `build_cluster_connected_graphs.py`: build the final connected road/crop graph.
- `build_component_connected_graphs.py`: shared graph-connection implementation
  used by the final builder; its filename is historical, but the module is active.
- `run_weekly_astar_accessibility.py`: calculate weekly baseline and penalized A*
  accessibility.
- `run_remaining_cluster_connected_batch.py`: batch orchestration for the final scope.
- `render_weekly_astar_accessibility.py`: country/crop weekly plots.
- `render_weekly_astar_accessibility_heatmaps.py`: final accessibility heatmaps.

## Current analysis and figures

- `compute_base_route_surface_mix.py`: baseline route surface composition.
- `compute_route_change_diagnostics.py`: wet-versus-baseline route diagnostics.
- `render_accessibility_visual_experiments.py`: analysis tables used by paper experiments.
- `render_base_route_surface_mix.py`: route-surface plots.
- `render_paper_*`: reproducible paper experiment tables and figures.
- `render_crop_*`, `render_four_country_*`, `render_lbr_*`: dissertation figures.
- `render_lbr_era5_grid_coverage_precip.py`: preserved self-contained LBR ERA5
  grid-coverage and weekly-precipitation renderer; its frozen GeoPackage is in
  `supporting_materials/lbr_era5_grid_coverage/`.
- `render_top12_country_temporal_ru.py`: Russian top-12 rainfall/delay panel.
- `compose_lbr_heatmap_with_side_panel.py`: compose the LBR chapter panel.
- `render_equator_belt_road_surface_status_map.py`: canonical and latest renderer
  for `outputs/equator_country_belt_road_surface_missing_hatched.png`. Thesis
  crops and translated copies are derived artifacts, not the source render.
- `render_equatorial_network_decomposition.py`: canonical renderer for the
  English equatorial crop-logistics network scheme at
  `outputs/equatorial_network_decomposition.png`. It distinguishes the active
  precipitation-driven, surface-sensitive routing path from contextual or
  future multi-hazard and road-closure extensions.

## Input and database services

- `fetch_equator_700km_full_year_data.sh`, `fetch_equatorial_countries.py`: raw-data fetch.
- `convert_road_surface_gpkg_to_parquet.py`: road format conversion.
- `load_road_surface_to_postgis.py`: road loading.
- `load_city_port_destinations_to_postgis.py`: destination loading.
- `load_overlay_parquet_to_postgis.py`: weekly factor loading.
- `rebuild_noded_road_graph.py`: noded base-road graph construction.
- `migrate_crop_origins_to_cropgrids.py`: reproducible CROPGRIDS migration and audit.
- `run_era5_only_overlay_then_boxplots_all.zsh`, `run_db_only_boxplot_stats.sh`,
  `render_db_weekly_boxplots.py`: supporting road-factor diagnostics.

The final workflow and exact arguments are documented in
`../CLUSTER_CONNECTED_ACCESSIBILITY_PIPELINE.md`.
