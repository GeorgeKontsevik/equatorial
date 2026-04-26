# Road Hazard Threshold Run: Gabon, 2024-03-01 to 2024-05-31

Status: superseded by `config/road_hazard_mapping_rebuilt.xlsx`.

The previous March-May run used an older road-hazard threshold matrix. The
active file name `config/road_hazard_thresholds_exact_mar_may.yaml` has now
been rebuilt from the new workbook, so the older trigger counts should not be
read as results from the current mapping.

Current authoritative inputs:

- Workbook: `config/road_hazard_mapping_rebuilt.xlsx`
- Primary mapping CSV: `config/road_hazard_mapping_rebuilt.csv`
- Source key CSV: `config/road_hazard_source_key.csv`
- Strict matrix: `config/weekly_hazard_thresholds_strict.csv`
- Runnable subset: `config/road_hazard_thresholds_exact_mar_may.yaml`

Current active runnable rows:

| Hazard | Surface | Factor | Use |
| --- | --- | --- | --- |
| `extreme_rainfall_operational` | paved | `era5_tp_1h_max_weekly_mm_per_h` | operational speed/capacity penalty only |
| `flood_extent_binary_proxy` | paved | `flood_weekly` | binary operational closure proxy, not depth damage |
| `flood_extent_binary_proxy` | unpaved | `flood_weekly` | binary operational closure proxy plus recovery/degradation note, not depth damage |
| `wind_crosswind` | paved | `era5_crosswind_10m_weekly_max_m_s` | operational speed restriction only |
| `wind_crosswind` | unpaved | `era5_crosswind_10m_weekly_max_m_s` | operational speed restriction only; dust handled by dust/visibility row |
| `dust_visibility` | paved | `visibility_weekly_min_m` | observed visibility speed/closure rule from NOAA Global Hourly station VIS |
| `dust_visibility_surface_wear` | unpaved | `visibility_weekly_min_m` | observed visibility operational rule; surface wear remains contextual/calibration-dependent |

Data completion status:

- NOAA visibility station data were fetched for Gabon March-May 2024 and written under `data/raw/visibility_noaa_isd/GAB/`.
- The March overlay was regenerated and now contains `visibility_week_*_min_m`, `pavement_surface_temperature_week_*_max_c`, and `soil_moisture_week_*_local_percentile`.
- `flood_depth_week_*_max_m` columns are present but empty because no true flood-depth raster in meters has been supplied.

No new accessibility run has been executed after this data completion pass.
