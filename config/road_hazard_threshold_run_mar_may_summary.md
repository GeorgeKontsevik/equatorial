# Road Hazard Threshold Run: Gabon, 2024-03-01 to 2024-05-31

Status: completed first exact-input threshold run.

## Inputs

- Dataset config: `config/datasets_gabon_2024_03_05_exact.yaml`
- Period config: `config/road_climate_damage_gabon_2024_03_05.yaml`
- Threshold rules: `config/road_hazard_thresholds_exact_mar_may.yaml`
- Overlay output: `outputs/road_multisource_overlay/GAB/2024-03-01_to_2024-05-31_7d`
- Accessibility output: `outputs/road_weekly_scenarios/GAB/2024-03-01_to_2024-05-31_7d_pop50000`

## Data Checks

- CHIRPS daily: 92/92 files present for 2024-03-01..2024-05-31; all opened as GeoTIFF.
- ERA5-Land hourly: March, April, and May NetCDF files opened with `t2m`, `skt`, `tp`, `swvl1`, `u10`, `v10`.
- Exact overlay factors written:
  - `chirps_24h_max_week_*_mm`
  - `era5_tp_1h_max_week_*_mm_per_h`
  - `era5_crosswind_10m_week_*_max`
  - `landslide_susceptibility`

## Threshold Matching

| Threshold factor | Input column family | Scope / condition | Run status |
|---|---|---|---|
| `era5_tp_1h_max_weekly_mm_per_h` | `era5_tp_1h_max_week_*_mm_per_h` | paved roads | active |
| `era5_tp_1h_max_weekly_mm_per_h` | `era5_tp_1h_max_week_*_mm_per_h` | unpaved roads | active |
| `chirps_24h_max_weekly_mm` | `chirps_24h_max_week_*_mm` | `landslide_susceptibility >= 3` | active, no threshold exceedance |
| `era5_crosswind_10m_weekly_max_m_s` | `era5_crosswind_10m_week_*_max` | all roads | active, no threshold exceedance |
| flood depth | none | standing water depth on road | not activated; no exact depth source |
| pavement surface temperature | none | pavement surface temperature | not activated; ERA5 `skt` is not road pavement temperature |
| visibility / dust | none | visibility in m | not activated; CAMS PM/AOD is not visibility |
| drought matric suction | none | matric suction / expansive clay | not activated; `swvl1` is not suction |

## Trigger Summary

| Factor | Scope | Level | Weeks triggered | Max roads triggered | Total triggered road-weeks |
|---|---:|---:|---:|---:|---:|
| `era5_tp_1h_max_weekly_mm_per_h` | paved | minor | 26 | 23,157 | 150,518 |
| `era5_tp_1h_max_weekly_mm_per_h` | paved | moderate | 6 | 1,142 | 1,803 |
| `era5_tp_1h_max_weekly_mm_per_h` | paved | severe | 0 | 0 | 0 |
| `era5_tp_1h_max_weekly_mm_per_h` | unpaved | minor | 27 | 30,002 | 291,800 |
| `era5_tp_1h_max_weekly_mm_per_h` | unpaved | moderate | 26 | 14,899 | 43,747 |
| `era5_tp_1h_max_weekly_mm_per_h` | unpaved | severe | 0 | 0 | 0 |
| `chirps_24h_max_weekly_mm` | susceptible roads | minor/moderate/severe | 0 | 0 | 0 |
| `era5_crosswind_10m_weekly_max_m_s` | all | minor/moderate/severe | 0 | 0 | 0 |

## Accessibility Result

- Baseline connected fraction: 1.0.
- Scenarios: `unknown_as_paved`, `unknown_as_unpaved`.
- Origins: `outputs/road_weekly_scenarios/GAB/origins_connected_n5_seed42.gpkg`.
- Closed roads: 0 in all weeks.
- Disconnected origin-weeks: 0.
- Maximum delay vs baseline:
  - `unknown_as_paved`: 66.205 minutes.
  - `unknown_as_unpaved`: 81.408 minutes.
- Mean delay across all origin-weeks:
  - `unknown_as_paved`: 15.503 minutes.
  - `unknown_as_unpaved`: 18.249 minutes.

## Notes

- ERA5-Land `tp` is accumulated precipitation. The overlay computes hourly increments from the accumulated sequence and ignores tiny negative numerical jitter so daily accumulation values are not misread as 1-hour rainfall spikes.
- The final `era5_tp_1h_max` overlay has max 46.10 mm/h across the run after this correction.
- No flood-depth threshold was run because the available GFM layer is flood extent, not water depth.
