# Road Hazard Additional Data Needs

Status: proposed download shortlist for exact threshold matching.

This file lists only data that would make audited thresholds more honest. It
does not include broad proxy layers.

## Already Enough To Derive Exact Factors

| Threshold family | No new download needed | Next action |
| --- | --- | --- |
| Landslide 24h rainfall | CHIRPS daily rasters and NASA landslide susceptibility for Gabon are already present. | Add `chirps_24h_max_weekly_mm` and condition it by `landslide_susceptibility`. |
| Extreme rainfall intensity | ERA5-Land hourly `tp` is already present. | Add reset-aware `era5_tp_1h_max_weekly_mm_per_h`. |
| Crosswind onset | ERA5-Land hourly `u10/v10` is already present. | Add road-bearing projection to produce `era5_crosswind_10m_weekly_max_m_s`. |

## Download Or Build Before Activation

| Priority | Needed factor | Why needed | Candidate source/action |
| --- | --- | --- | --- |
| 1 | `flood_depth_weekly_max_m` | Flood thresholds are in meters of standing water. Current GFM data are flood extent/classification, not depth. | Add a flood-depth/inundation-depth product or local hydrodynamic model output. CEMS flood inundation maps are scenario/return-period maps, useful for hazard depth, not observed weekly depth. |
| 2 | `wind_gust_weekly_max_m_s` | Severe wind thresholds are gust/closure thresholds. Current `u10/v10` are mean 10m wind components. | Download ERA5 single-level `10m_wind_gust_since_previous_post_processing` for the same Gabon period. |
| 3 | `era5_max_total_precip_rate_weekly_mm_per_h` | The rainfall threshold is an intensity threshold. We can derive hourly increments from `tp`, but ERA5 also exposes a max precipitation-rate variable. | Download ERA5 single-level `maximum_total_precipitation_rate_since_previous_post_processing` if we want a source-native intensity factor. |
| 4 | `visibility_weekly_min_m` | Dust thresholds are visibility thresholds in meters. CAMS PM/AOD are not visibility. | Download station visibility where available, or a gridded/reanalysis visibility product with units in meters. Keep CAMS as diagnostic only. |
| 5 | `pavement_surface_temperature_weekly_max_c` | Heat/rutting thresholds are pavement/asphalt surface temperature. ERA5 `skt` is not road pavement temperature. | Build a documented pavement-temperature model from meteorology and road/land-cover inputs, or obtain a pavement-surface product if available. |
| 6 | `matric_suction_weekly_max_kpa` | Drought threshold is matric suction in kPa. ERA5 `swvl1` is volumetric soil water. | Use SoilGrids plus a documented soil-water-retention conversion and an expansive-subgrade mask. |
| 7 | `urban_pluvial_flood_depth_weekly_max_m` | Urban runoff thresholds should become road water depth before applying flood-speed curves. | Build a pluvial/hydraulic model for hub links, then reuse the flood-depth contract. |

## What Not To Download For Direct Thresholding

| Data | Reason |
| --- | --- |
| More GFM flood extent tiles | Helpful for observed flood footprint, but still not water depth in meters. |
| More CAMS PM/AOD only | Helpful for dust context, but still not visibility in meters. |
| More monthly climate aggregates | Thresholds are event/hour/day scale; monthly layers would keep the semantic mismatch. |

## Implementation Notes

1. Keep old broad factors for diagnostics, but do not use them for audited threshold triggers.
2. Add exact-factor names to the overlay so the unit is visible in the column name.
3. Move active thresholding from CSV to a semantic config with units, surface scope, and condition masks.
4. Re-run accessibility only after exact factors and semantic thresholding are in place.
