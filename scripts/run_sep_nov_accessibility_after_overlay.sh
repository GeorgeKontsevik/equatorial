#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)_accessibility}"
LOG_DIR="$ROOT/logs/sep_nov_accessibility_after_overlay_${RUN_ID}"
CFG_DIR="$ROOT/config/generated"
CFG_PREFIX="sep_nov_accessibility_${RUN_ID}"
BASE_PREFIX="${BASE_PREFIX:-}"
DAMAGE_CONFIG="${DAMAGE_CONFIG:-config/road_climate_damage_namibia_2024_sep_nov.yaml}"
THRESHOLDS_YAML="${THRESHOLDS_YAML:-config/road_hazard_thresholds_exact_mar_may.yaml}"
STATUS_TSV="$LOG_DIR/status.tsv"

mkdir -p "$LOG_DIR" "$CFG_DIR"
printf 'iso\tstage\tstatus\tstarted_at\tfinished_at\tconfig\tlog\n' > "$STATUS_TSV"

make_full_config() {
  local iso="$1"
  local base_prefix="$BASE_PREFIX"
  if [[ -z "$base_prefix" ]]; then
    local latest_cfg
    latest_cfg="$(find "$CFG_DIR" -maxdepth 1 -type f -name "fetch_sep_nov_*_${iso}_datasets_2024_09_11.yaml" | sort | tail -1)"
    if [[ -z "$latest_cfg" ]]; then
      printf 'No base fetch config found for %s; set BASE_PREFIX explicitly.\n' "$iso" >&2
      return 1
    fi
    base_prefix="$(basename "$latest_cfg" "_${iso}_datasets_2024_09_11.yaml")"
  fi
  local in_cfg="$CFG_DIR/${base_prefix}_${iso}_datasets_2024_09_11.yaml"
  local out_cfg="$CFG_DIR/${CFG_PREFIX}_${iso}_datasets_2024_09_11_full.yaml"
  "$PY" - "$in_cfg" "$out_cfg" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

import yaml

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
cfg = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
study = cfg.get("study_area") or {}
datasets = cfg.setdefault("datasets", {})
bbox = study.get("bbox")
iso = str(study.get("country_code") or "").upper()

visibility = datasets.setdefault("visibility_noaa_isd", {})
visibility.update(
    {
        "enabled": True,
        "source_url": "https://www.ncei.noaa.gov/pub/data/noaa/",
        "history_url": "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv",
        "spatial_resolution_raw": "station observations",
        "temporal_resolution": "hourly",
        "start_date": "2024-09-01",
        "end_date": "2024-11-30",
        "max_stations": 12,
        "bbox": bbox,
        "country_code": iso,
    }
)

for name in ["gadm", "road_surface", "chirps", "era5", "flood", "landslide_susceptibility", "cams", "gem", "liquefaction", "flopros"]:
    if name in datasets:
        datasets[name]["enabled"] = True
        if bbox is not None:
            datasets[name].setdefault("bbox", bbox)
        if iso:
            datasets[name].setdefault("country_code", iso)

dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(dst)
PY
}

run_stage() {
  local iso="$1"
  local stage="$2"
  local log="$3"
  shift 3
  local started
  local finished

  started="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[sep-nov-access] %s %s start=%s\n' "$iso" "$stage" "$started" | tee -a "$LOG_DIR/driver.log"
  if "$@" > "$log" 2>&1; then
    finished="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[sep-nov-access] %s %s done status=ok finished=%s log=%s\n' "$iso" "$stage" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
    printf '%s\t%s\tok\t%s\t%s\t%s\t%s\n' "$iso" "$stage" "$started" "$finished" "$CURRENT_CFG" "$log" >> "$STATUS_TSV"
    return 0
  fi
  finished="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[sep-nov-access] %s %s done status=failed finished=%s log=%s\n' "$iso" "$stage" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
  tail -80 "$log" >> "$LOG_DIR/driver.log"
  printf '%s\t%s\tfailed\t%s\t%s\t%s\t%s\n' "$iso" "$stage" "$started" "$finished" "$CURRENT_CFG" "$log" >> "$STATUS_TSV"
  return 1
}

wait_for_overlay() {
  local iso="$1"
  local overlay_dir="$ROOT/outputs/road_multisource_overlay/$iso/2024-09-01_to_2024-11-30_7d"
  local overlay_gpkg="$overlay_dir/roads_with_multisource_overlay.gpkg"
  local summary_json="$overlay_dir/summary.json"

  while [[ ! -s "$overlay_gpkg" || ! -s "$summary_json" ]]; do
    printf '[sep-nov-access] %s waiting_for_overlay at=%s\n' "$iso" "$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_DIR/driver.log"
    sleep 300
  done
}

FAILED=()
COUNTRY_LIST="${COUNTRIES:-AGO BDI BRN COG CIV}"
read -r -a COUNTRY_CODES <<< "$COUNTRY_LIST"

for iso in "${COUNTRY_CODES[@]}"; do
  CURRENT_CFG="$(make_full_config "$iso")"
  wait_for_overlay "$iso"

  result_dir="$ROOT/outputs/road_weekly_scenarios/$iso/2024-09-01_to_2024-11-30_7d_crop_connected_visibility_speed_dijkstra"
  if [[ "${FORCE:-0}" != "1" && -s "$result_dir/weekly_median_access_minutes.png" && -s "$result_dir/crop_type_maps/crop_weekly_median_access_minutes.png" ]]; then
    printf '[sep-nov-access] %s accessibility already exists; skipping\n' "$iso" | tee -a "$LOG_DIR/driver.log"
    continue
  fi

  access_log="$LOG_DIR/${iso}_accessibility.log"
  if ! run_stage "$iso" "accessibility_and_plots" "$access_log" "$PY" -m src.data.run_road_hazard_overnight_worker \
    --country-code "$iso" \
    --config "$CURRENT_CFG" \
    --damage-config "$DAMAGE_CONFIG" \
    --thresholds-yaml "$THRESHOLDS_YAML" \
    --start-date 2024-09-01 \
    --end-date 2024-11-30 \
    --step-days 7 \
    --city-threshold 50000 \
    --candidate-top-n 100 \
    --top-n-per-crop 3 \
    --min-component-nodes 100 \
    --spam-dir spam_tifs \
    --skip-overlay; then
    FAILED+=("${iso}:accessibility_and_plots")
  fi
done

if (( ${#FAILED[@]} > 0 )); then
  printf '%s\n' "${FAILED[@]}" > "$LOG_DIR/failed_stages.txt"
  printf '[sep-nov-access] failed stages: %s\n' "${FAILED[*]}" | tee -a "$LOG_DIR/driver.log"
  exit 1
fi

printf '[sep-nov-access] all stages completed\n' | tee -a "$LOG_DIR/driver.log"
