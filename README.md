# equatorial

`equatorial` is now trimmed around the active full-year `2024` / `700 km` crop accessibility workflow.

Current pipeline:

1. Raw data fetch for the equatorial `700 km` country belt
2. Road-level multisource overlay construction
3. Crop-origin candidate build, baseline-connected origin selection, and weekly accessibility calculation

Legacy experiments, old launchers, Bolivia sample artifacts, and one-off research materials were moved under `old/`.

## Active Entry Points

Data fetch:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
bash scripts/fetch_equator_700km_full_year_data.sh
```

Road overlay / preprocessing:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.run_multisource_road_overlay \
  --config config/datasets.yaml \
  --country-code GAB \
  --damage-config config/road_climate_damage_gabon_2024_03_05.yaml
```

Crop-origin candidate build:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.build_spam_crop_top_origins \
  --country-code GAB \
  --top-n 100 \
  --spam-dir spam_tifs \
  --output-dir outputs/road_weekly_scenarios/GAB/origins_spam_top100_by_crop_candidates
```

Baseline-connected crop origin selection:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.select_baseline_connected_crop_origins \
  --country-code GAB \
  --candidate-gpkg outputs/road_weekly_scenarios/GAB/origins_spam_top100_by_crop_candidates/spam_crop_top100_origins.gpkg \
  --overlay-gpkg outputs/road_multisource_overlay/GAB/2024-03-01_to_2024-05-31_7d/roads_with_multisource_overlay.gpkg \
  --output-dir outputs/road_weekly_scenarios/GAB/origins_spam_top3_by_crop_baseline_connected \
  --city-threshold 50000 \
  --top-n-per-crop 3
```

Weekly accessibility calculation:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.run_weekly_accessibility_dijkstra \
  --country-code GAB \
  --start-date 2024-03-01 \
  --end-date 2024-05-31 \
  --step-days 7 \
  --city-threshold 50000 \
  --origins-file outputs/road_weekly_scenarios/GAB/origins_spam_top3_by_crop_baseline_connected/spam_crop_top3_baseline_connected_origins.gpkg \
  --overlay-gpkg outputs/road_multisource_overlay/GAB/2024-03-01_to_2024-05-31_7d/roads_with_multisource_overlay.gpkg \
  --thresholds-yaml config/road_hazard_thresholds_exact_mar_may.yaml
```

Overnight orchestration:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python -m src.data.run_road_hazard_overnight_worker \
  --country-code GAB \
  --config config/datasets_gabon_2024_03_05_exact.yaml \
  --damage-config config/road_climate_damage_gabon_2024_03_05.yaml \
  --thresholds-yaml config/road_hazard_thresholds_exact_mar_may.yaml \
  --start-date 2024-03-01 \
  --end-date 2024-05-31 \
  --step-days 7 \
  --city-threshold 50000 \
  --candidate-top-n 100 \
  --top-n-per-crop 3
```

`run_road_hazard_overnight_worker.py` is intentionally orchestration-only now. It calls the separate step scripts rather than embedding its own crop-origin selection logic.

## Working Layout

- `scripts/fetch_equator_700km_full_year_data.sh`: active full-year fetch launcher
- `src/data/run_multisource_road_overlay.py`: active preprocessing
- `src/data/build_spam_crop_top_origins.py`: active crop-origin candidate builder
- `src/data/select_baseline_connected_crop_origins.py`: active baseline-connected origin selector
- `src/data/run_weekly_accessibility_dijkstra.py`: active weekly accessibility engine
- `src/data/run_road_hazard_overnight_worker.py`: orchestration wrapper over the active steps
- `src/data/plot_weekly_accessibility_results.py`: active weekly result plots
- `src/data/plot_crop_accessibility_results.py`: active crop-level result plots
- `old/`: legacy experiments, old scripts, Bolivia/sample materials, and deprecated analysis helpers

## Notes

- The active fetch belt is `700 km`, not `500 km`.
- The active calculation path is `Dijkstra`, not the old `Pandana` runner.
- Some shared helper functions still live in older modules where active code imports them; those modules are marked accordingly and should not be treated as the preferred execution path.
