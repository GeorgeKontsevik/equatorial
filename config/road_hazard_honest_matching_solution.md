# Honest Threshold Matching Solution

Status: proposal for approval.

The solution is to stop matching audited thresholds to broad weekly proxy columns. Instead, the overlay should produce factors whose names, units, and temporal meaning are the same as the threshold variable.

Machine-readable contract: `config/road_hazard_honest_data_contracts.yaml`.

## What Can Be Matched From Current Raw Data

| Threshold family | Honest model factor to add | Source already on disk | Why it matches |
| --- | --- | --- | --- |
| Landslide rainfall | `chirps_24h_max_weekly_mm` + `landslide_susceptibility` condition | CHIRPS daily rasters + NASA landslide susceptibility Gabon raster | Threshold is 24h rainfall on susceptible slopes; CHIRPS gives daily 24h precipitation, susceptibility gives the condition mask. |
| Extreme rainfall intensity | `era5_tp_1h_max_weekly_mm_per_h` | ERA5-Land hourly `tp` | Threshold is mm/h; ERA5 hourly accumulated `tp` can be converted to reset-aware hourly increments, then maxed by week. |
| Wind crosswind onset | `era5_crosswind_10m_weekly_max_m_s` | ERA5-Land hourly `u10` and `v10` + road bearing | Threshold is crosswind velocity; u/v can be projected onto each road link normal. |

These are not active yet because the current threshold engine lacks surface scopes and condition masks.

## What Cannot Be Matched Honestly Yet

| Threshold family | Missing exact variable | Why current data are not enough |
| --- | --- | --- |
| Flood depth | `flood_depth_weekly_max_m` | Current `flood_weekly` is Copernicus GFM flood extent/classification, not depth in meters. |
| Heat / rutting | `pavement_surface_temperature_weekly_max_c` | ERA5 `skt` is model skin temperature, not road pavement/asphalt surface temperature. |
| Drought subgrade cracking | `matric_suction_weekly_max_kpa` | ERA5 `swvl1` is volumetric soil water, not matric suction; also needs expansive-subgrade mask. |
| Dust operations | `visibility_weekly_min_m` | CAMS PM/AOD are aerosol variables, not visibility in meters. |
| Wind gust damage/restriction | `wind_gust_weekly_max_m_s` | Current wind columns are hourly 10m wind speed/components, not gusts. |
| Urban runoff | `urban_pluvial_flood_depth_weekly_max_m` | Rainfall alone is not a transferable urban-runoff threshold; it must become road water depth first. |

## Required Code Change

The active model should move from a plain threshold CSV to a semantic threshold config with:

- `factor`
- `units`
- `direction`
- `thresholds`
- `surface_scope`: `paved`, `unpaved`, `unknown`, or `all`
- optional `condition_factor`, `condition_operator`, `condition_value`
- explicit `effect_type`

The current accessibility runner applies all threshold effects only to effective `unpaved` roads. That has to change before rainfall-on-paved, heat, wind, or flood-depth thresholds can be used honestly.

## Activation Recommendation

First exact activation set after code support:

1. `chirps_24h_max_weekly_mm` for landslide rainfall, only where `landslide_susceptibility` is approved as susceptible.
2. `era5_tp_1h_max_weekly_mm_per_h` for paved/unpaved rainfall intensity, with separate surface scopes.
3. `era5_crosswind_10m_weekly_max_m_s` only for the 20 m/s high-sided-lorry operational threshold.

Keep flood depth, pavement heat, drought, dust visibility, wind gust, and urban runoff inactive until their exact variables exist.
