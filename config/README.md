# Configuration Map

## Active data collection

- `datasets.yaml`: base dataset contract.
- `datasets_gabon_2024_03_05_exact.yaml`: established template used by the
  full-year config generator.
- `generated/full_year_2024_era5_tp_remaining_20260517_203158/`: generated
  country configs used for the current data collection.
- `data/metadata/manual_steps/`: manual acquisition notes.

## Active road-hazard contracts

- `road_hazard_mapping_rebuilt.xlsx`: authoritative mapping workbook.
- `road_hazard_mapping_rebuilt.csv`: primary mapping export.
- `road_hazard_source_key.csv`: source references.
- `road_hazard_validation_notes.csv`: validation results.
- `road_hazard_original_audit.csv`: source audit retained with the mapping.
- `weekly_hazard_thresholds_strict.csv`: strict machine-readable matrix.
- `road_hazard_honest_data_contracts.yaml`: usable factor contracts.
- `road_hazard_impact_curves.yaml`: impact-curve representation.
- `road_hazard_thresholds_exact_mar_may.yaml`: runnable/diagnostic rule export.
- `road_hazard_data_mapping_approval.*`,
  `road_hazard_honest_matching_solution.md`, and
  `road_hazard_additional_data_needs.md`: interpretation and limitations.

`road_climate_damage.yaml` is the default configuration consumed by the active
multisource overlay script. Country-specific and superseded threshold
experiments are archived under `old/config/`.
