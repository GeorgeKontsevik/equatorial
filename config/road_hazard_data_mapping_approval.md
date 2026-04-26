# Road Hazard Threshold Data Mapping Approval

Status: pending approval.

This table maps the audited road-hazard anchor values to the data currently present in the latest GAB road overlay:

- overlay: `outputs/road_multisource_overlay/GAB/2024-08-01_to_2024-10-31_7d/roads_with_multisource_overlay.gpkg`
- rows: `48321`
- period: `2024-08-01` to `2024-10-31`, 7-day windows
- surfaces: `unknown=41369`, `unpaved=4438`, `paved=2514`

Machine-readable version: `config/road_hazard_data_mapping_approval.csv`.

## Approval Summary

| Hazard | Current data match | Proposed decision before modelling |
| --- | --- | --- |
| `landslide_rainfall_24h` | blocked | Defer until landslide susceptibility and 24h/event rainfall are available. |
| `extreme_rainfall_paved` | partial temporal mismatch | Do not activate audited mm/h curve from weekly CHIRPS sums without explicit proxy approval. |
| `extreme_rainfall_unpaved` | partial temporal mismatch | Add daily/event rainfall metric or approve a weekly accumulation proxy. |
| `flood_depth` | semantic mismatch | Defer depth curve; current `flood_weekly` is GFM extent, not confirmed water depth. |
| `extreme_heat_pavement` | partial proxy match | Can be approved as diagnostic or speed-only low-risk proxy; current max is below 45 C. |
| `drought_expansive_subgrade` | blocked unit mismatch | Defer until suction/soil transform exists. |
| `dust_visibility` | partial proxy mismatch | Keep diagnostic until visibility or calibrated PM/AOD-to-visibility transform exists. |
| `wind_crosswind` | partial proxy match | Can be approved as operational restriction proxy, not pavement damage. |
| `urban_runoff` | blocked | Defer; route through hydraulic/pluvial depth model rather than universal rainfall thresholds. |

## Candidate Threshold Rows If Approved

These are not active yet. They are the narrow set that can be translated into the current threshold-CSV contract with the least semantic damage:

| factor | minor | moderate | severe | catastrophic | caveat |
| --- | ---: | ---: | ---: | ---: | --- |
| `era5_skt_weekly_max` | 45 | 50 | 65 | 75 | Heat is rutting/damage proxy, not direct speed threshold. Current code treats SKT as speed-only. |
| `era5_wind_speed_weekly_max` | 20 | 30 | 42 | 45 | Operational restriction / treefall proxy only; not pavement damage. |

Not recommended for direct activation yet:

| factor | reason |
| --- | --- |
| `chirps_weekly_mm` | Current values are weekly sums; audited anchors are short-duration intensity or event-depth. |
| `flood_weekly` | Current data are GFM flood extent values; audited anchors are flood depths in meters. |

## Verification Notes

- `chirps_weekly_mm`: `min=0`, `p50=15.38`, `p95=94.12`, `max=179.21 mm/week`.
- `flood_weekly`: all current GAB weekly columns are `0`, with `positive_roads=0`.
- `era5_skt_weekly_max`: `min=25.79 C`, `p50=31.99 C`, `p95=35.30 C`, `max=43.65 C`.
- `era5_wind_speed_weekly_max`: `min=0.93`, `p50=2.88`, `p95=6.94`, `max=8.77 m/s`.
- `cams_duaod550_weekly_max`: `min=0.00136`, `p50=0.00663`, `p95=0.03775`, `max=0.10765`.
