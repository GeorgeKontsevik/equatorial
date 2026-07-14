# Protected LBR Figure Provenance

These two current figure families and their reproduction inputs must not be
moved to `old/`.

## ERA5 coverage and weekly precipitation

- Preserved PNG:
  `outputs/astar_accessibility_weekly/paper_lbr_precip_grid/lbr_era5_grid_coverage_precip_2024_08_19.png`
- Renderer: `scripts/render_lbr_era5_grid_coverage_precip.py`
- Frozen input bundle:
  `supporting_materials/lbr_era5_grid_coverage/data/lbr_era5_grid_week_2024_08_19.gpkg`
- Bundle description: `supporting_materials/lbr_era5_grid_coverage/README.md`

## LBR crop, cluster, road, and destination panels

The dissertation figure is assembled in
`../itmo-phd-thesis-template-en/Dissertation/chapter4.tex` from three PNGs:

- `images/ch4/lbr_crop_distribution.png`
- `images/ch4/lbr_crop_clusters_nodes.png`
- `images/ch4/lbr_crop_destinations_by_type.png`

Its complete tracked reproduction bundle is:

`../itmo-phd-thesis-template-en/thesis_repro/ch4_lbr_country_inputs/`

That bundle contains:

- `scripts/export_lbr_country_inputs_data.py`
- `scripts/render_lbr_crop_rows.py`
- `scripts/render_lbr_crop_destinations_by_type.py`
- `data/lbr_country_inputs.gpkg` (24 MB)
- `data/lbr_country_inputs_manifest.json`
- all three rendered PNGs under `outputs/`

The GeoPackage preserves the boundary, 45,704 road edges, 97 crop origins, 97
cluster nodes, 4,868 crop preview cells, 21 small-city destinations, 1 large
city, 1 port, and 19 airports. The whole reproduction bundle is tracked in the
dissertation repository at commit `a951dd6`.
