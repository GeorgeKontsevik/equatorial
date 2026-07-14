# LBR ERA5 Coverage And Precipitation Bundle

This is a current protected reproduction bundle for the Liberia figure showing
ERA5 grid coverage and precipitation for the week starting 2024-08-19. Do not
move it to `old/`.

## Files

- `data/lbr_era5_grid_week_2024_08_19.gpkg`
  - `boundary`: Liberia GADM boundary, 1 row.
  - `weekly_grid`: 776 ERA5 cells inside the country, including
    `tp_sum_weekly_mm`.
- `../../scripts/render_lbr_era5_grid_coverage_precip.py`: self-contained
  renderer using only this GeoPackage.
- `../../outputs/astar_accessibility_weekly/paper_lbr_precip_grid/lbr_era5_grid_coverage_precip_2024_08_19.png`:
  preserved source render, 827 × 1683 px.

The data snapshot was exported from `eq.era5_precip_weekly_grid`. Before the
country mask, 2,350 non-null cells were available; 776 cells were inside the
boundary. Median precipitation inside Liberia was `96.1832835` mm/week.

The exact source PNG is preserved from dissertation commit `62c141c`; its
SHA-256 is `7e5bc1f39a45325897a58591c9f98686e085cacae7f95d9701b5ffed3e3daf48`.

Run from the `equatorial` repository root:

```bash
.venv/bin/python scripts/render_lbr_era5_grid_coverage_precip.py
```
