# Road Hazard Data Mapping Approval

Status: rebuilt from `config/road_hazard_mapping_rebuilt.xlsx`; NOAA visibility and overlay hooks added for runnable rows that now have data.

Machine-readable sheets:

- Primary mapping: `config/road_hazard_mapping_rebuilt.csv`
- Source key: `config/road_hazard_source_key.csv`
- Validation notes: `config/road_hazard_validation_notes.csv`
- Current approval matrix: `config/road_hazard_data_mapping_approval.csv`
- Original audit sheet: `config/road_hazard_original_audit.csv`

## Validation Notes

| check | status | note |
| --- | --- | --- |
| unit compatibility | pass | Rainfall intensity thresholds are only mm/h. Weekly CHIRPS is demoted to antecedent/context. Flood depth curves require m/cm depth, not binary extent. |
| temporal compatibility | pass | Event/hourly/daily variables separated from weekly/seasonal modifiers. |
| flood semantics | pass | GFM is mapped to binary/likelihood closure proxy; depth fragility requires depth raster or depth-estimation workflow. |
| road type split | pass | Each hazard has paved/unpaved rows with different assumptions and metrics. |
| wind/dust coverage | pass | Wind and dust rows added. Dust uses visibility as primary variable and CAMS PM/AOD only as calibrated proxy. |
| threshold confidence | partial | Hard numeric values are kept only when source-backed or explicitly marked approximation/local calibration. |

## Current Strict Mapping

| hazard_type | road_type | runnable_factor | surface_scope | current_use | status | source_reference |
| --- | --- | --- | --- | --- | --- | --- |
| extreme_rainfall_operational | paved | era5_tp_1h_max_weekly_mm_per_h | paved | operational speed/capacity penalty only; not structural failure | runnable_with_hourly_intensity_factor | S2_CHIRPS_daily: https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY; S8_Tsapakis_2013_weather_travel_time: https://www.sciencedirect.com/science/article/pii/S0966692312002694; S9_Chung_2012_weather_capacity: https://www.sciencedirect.com/science/article/abs/pii/S0967070X11001144 |
| extreme_rainfall_erosion | unpaved |  | unpaved | inactive until local percentile/calibration factor exists | inactive_no_universal_threshold | S2_CHIRPS_daily: https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm; S12_FAO_protective_measures: https://www.fao.org/4/T0099E/T0099e01.htm; S15_Unpaved_road_erosion_2024: https://bioone.org/journals/air-soil-and-water-research/volume-17/issue-1/11786221241272396/Erosion-Mechanisms-in-Unpaved-Roads--Effects-of-Slope-Rainfall/10.1177/11786221241272396.full |
| flood_depth | paved | flood_depth_weekly_max_m | paved | inactive until actual flood-depth raster/model exists | inactive_needs_depth_factor | S1_Koks_2019_Nature: https://www.nature.com/articles/s41467-019-10442-3; S1b_Koks_2019_supplement: https://static-content.springer.com/esm/art%3A10.1038%2Fs41467-019-10442-3/MediaObjects/41467_2019_10442_MOESM1_ESM.pdf; S4_Pregnolato_2017_flood_depth_disruption: https://www.sciencedirect.com/science/article/pii/S1361920916308367; S5_Kramer_2016_inundated_roads: https://deltaexpertise.nl/images/f/f2/Kramer_2016_Safety_criteria_for_the_trafficability_of_inundated_roads_in_urban.pdf |
| flood_depth | unpaved | flood_depth_weekly_max_m | unpaved | inactive until actual flood-depth raster/model exists | inactive_needs_depth_factor | S1_Koks_2019_Nature: https://www.nature.com/articles/s41467-019-10442-3; S1b_Koks_2019_supplement: https://static-content.springer.com/esm/art%3A10.1038%2Fs41467-019-10442-3/MediaObjects/41467_2019_10442_MOESM1_ESM.pdf; S4_Pregnolato_2017_flood_depth_disruption: https://www.sciencedirect.com/science/article/pii/S1361920916308367; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm |
| flood_extent_binary_proxy | paved | flood_weekly | paved | binary operational closure proxy only; no depth damage | runnable_binary_proxy_if_gfm_present | S6_Copernicus_GFM_manual: https://extwiki.eodc.eu/gfm_assets/gfm4.0_pum_2025.pdf; S7_Betterle_2024_flood_depth_from_extent: https://nhess.copernicus.org/articles/24/2817/2024/ |
| flood_extent_binary_proxy | unpaved | flood_weekly | unpaved | binary operational closure proxy plus recovery/degradation; no depth damage | runnable_binary_proxy_if_gfm_present | S6_Copernicus_GFM_manual: https://extwiki.eodc.eu/gfm_assets/gfm4.0_pum_2025.pdf; S7_Betterle_2024_flood_depth_from_extent: https://nhess.copernicus.org/articles/24/2817/2024/; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm |
| heat_pavement | paved | pavement_surface_temperature_weekly_max_c | paved | diagnostic proxy from ERA5 skin temperature; not active traffic-speed threshold without pavement model validation | diagnostic_proxy_factor_buildable | S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land; S10_FHWA_pavement_resilience: https://www.fhwa.dot.gov/pavement/concrete/pubs/hif23006.pdf |
| heat_dryness | unpaved |  | unpaved | inactive until local T/soil-moisture/wind/traffic index exists | inactive_needs_local_index | S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land; S10_FHWA_pavement_resilience: https://www.fhwa.dot.gov/pavement/concrete/pubs/hif23006.pdf; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm |
| wind_crosswind | paved | era5_crosswind_10m_weekly_max_m_s | paved | operational restriction only; no pavement damage | runnable_speed_only_crosswind_proxy | S1_Koks_2019_Nature: https://www.nature.com/articles/s41467-019-10442-3; S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land |
| wind_crosswind | unpaved | era5_crosswind_10m_weekly_max_m_s | unpaved | operational restriction only; dust handled by dust/visibility row | runnable_speed_only_crosswind_proxy | S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land; S13_UNEP_WMO_dust_assessment: https://wesr.unep.org/media/docs/assessments/global_assessment_of_sand_and_dust_stormsx.pdf; S14_UNDRR_dust_sandstorm_limits: https://www.undrr.org/understanding-disaster-risk/terminology/hips/mh0201 |
| dust_visibility | paved | visibility_weekly_min_m | paved | inactive until visibility factor exists; CAMS PM/AOD not enough | runnable_if_visibility_factor_present | S13_UNEP_WMO_dust_assessment: https://wesr.unep.org/media/docs/assessments/global_assessment_of_sand_and_dust_stormsx.pdf; S14_UNDRR_dust_sandstorm_limits: https://www.undrr.org/understanding-disaster-risk/terminology/hips/mh0201 |
| dust_visibility_surface_wear | unpaved | visibility_weekly_min_m | unpaved | inactive until visibility/calibrated dust factor exists | runnable_if_visibility_factor_present | S13_UNEP_WMO_dust_assessment: https://wesr.unep.org/media/docs/assessments/global_assessment_of_sand_and_dust_stormsx.pdf; S14_UNDRR_dust_sandstorm_limits: https://www.undrr.org/understanding-disaster-risk/terminology/hips/mh0201; S12_FAO_protective_measures: https://www.fao.org/4/T0099E/T0099e01.htm |
| soil_moisture_subgrade | paved | soil_moisture_weekly_local_percentile | paved | diagnostic local percentile from ERA5 swvl1; activate only after local calibration/baseline choice | diagnostic_local_percentile_factor_buildable | S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land; S10_FHWA_pavement_resilience: https://www.fhwa.dot.gov/pavement/concrete/pubs/hif23006.pdf; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm |
| soil_moisture_surface_condition | unpaved | soil_moisture_weekly_local_percentile | unpaved | diagnostic local percentile from ERA5 swvl1; activate only after local calibration/baseline choice | diagnostic_local_percentile_factor_buildable | S2_CHIRPS_daily: https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY; S3_ERA5_Land: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land; S11_FAO_drainage_design: https://www.fao.org/4/t0099e/t0099e04.htm; S12_FAO_protective_measures: https://www.fao.org/4/T0099E/T0099e01.htm |
