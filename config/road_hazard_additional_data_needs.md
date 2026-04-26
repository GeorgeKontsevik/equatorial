# Road Hazard Additional Data Needs

Status: updated after adding NOAA visibility ingestion and new overlay hooks.

NOAA Global Hourly visibility has been fetched for Gabon March-May 2024 and `visibility_week_*_min_m` is now an overlay factor. Flood depth remains manual because extent is not depth.

## Download Or Build Before Activation

| hazard_type | road_type | runnable_factor | units | current_use | status |
| --- | --- | --- | --- | --- | --- |
| extreme_rainfall_erosion | unpaved |  | local percentile event rainfall index | inactive until local percentile/calibration factor exists | inactive_no_universal_threshold |
| flood_depth | paved | flood_depth_weekly_max_m | m | inactive until actual flood-depth raster/model exists | inactive_needs_depth_factor |
| flood_depth | unpaved | flood_depth_weekly_max_m | m | inactive until actual flood-depth raster/model exists | inactive_needs_depth_factor |
| heat_dryness | unpaved |  | local percentile dryness index | inactive until local T/soil-moisture/wind/traffic index exists | inactive_needs_local_index |

## Diagnostic Only

| hazard_type | road_type | runnable_factor | units | current_use | status |
| --- | --- | --- | --- | --- | --- |
| heat_pavement | paved | pavement_surface_temperature_weekly_max_c | celsius | diagnostic proxy from ERA5 skin temperature; not active traffic-speed threshold without pavement model validation | diagnostic_proxy_factor_buildable |
| soil_moisture_subgrade | paved | soil_moisture_weekly_local_percentile | local percentile soil moisture | diagnostic local percentile from ERA5 swvl1; activate only after local calibration/baseline choice | diagnostic_local_percentile_factor_buildable |
| soil_moisture_surface_condition | unpaved | soil_moisture_weekly_local_percentile | local percentile soil moisture | diagnostic local percentile from ERA5 swvl1; activate only after local calibration/baseline choice | diagnostic_local_percentile_factor_buildable |
