#!/usr/bin/env bash
set -u -o pipefail

# Run the road-hazard overnight worker for the countries highlighted in
# equator_country_belt_map_700km.png, excluding Brazil.
#
# Usage:
#   cd /path/to/equatorial
#   bash scripts/run_equator_belt_overnight.sh
#
# Useful overrides:
#   ONLY="GAB,COD,IDN" bash scripts/run_equator_belt_overnight.sh
#   SKIP_FETCH=1 bash scripts/run_equator_belt_overnight.sh
#   FETCH_DATASETS="gadm,road_surface,chirps,era5,visibility_noaa_isd" bash scripts/run_equator_belt_overnight.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
BASE_CONFIG="${BASE_CONFIG:-config/datasets_gabon_2024_03_05_exact.yaml}"
DAMAGE_CONFIG="${DAMAGE_CONFIG:-config/road_climate_damage_gabon_2024_03_05.yaml}"
THRESHOLDS_YAML="${THRESHOLDS_YAML:-config/road_hazard_thresholds_exact_mar_may.yaml}"
START_DATE="${START_DATE:-2024-03-01}"
END_DATE="${END_DATE:-2024-05-31}"
STEP_DAYS="${STEP_DAYS:-7}"
CITY_THRESHOLD="${CITY_THRESHOLD:-50000}"
CANDIDATE_TOP_N="${CANDIDATE_TOP_N:-100}"
TOP_N_PER_CROP="${TOP_N_PER_CROP:-3}"
MIN_COMPONENT_NODES="${MIN_COMPONENT_NODES:-100}"
SPAM_DIR="${SPAM_DIR:-spam_tifs}"
SKIP_FETCH="${SKIP_FETCH:-0}"
EXPORT_OVERLAY_PARQUET="${EXPORT_OVERLAY_PARQUET:-0}"
FETCH_DATASETS="${FETCH_DATASETS:-gadm,road_surface,chirps,era5,landslide_susceptibility,visibility_noaa_isd}"
ONLY="${ONLY:-}"

RUN_ID="$(date +%Y%m%d_%H%M%S)"
LOG_DIR="$ROOT/logs/road_hazard_equator_belt_${RUN_ID}"
CFG_DIR="$ROOT/config"
CFG_PREFIX="generated_equator_belt_${RUN_ID}"
mkdir -p "$LOG_DIR" "$CFG_DIR"

export PYTHONUNBUFFERED=1
export MPLCONFIGDIR="${MPLCONFIGDIR:-$ROOT/.mplconfig}"
mkdir -p "$MPLCONFIGDIR"

IFS=',' read -r -a ONLY_CODES <<< "$ONLY"

should_run_country() {
  local iso="$1"
  if [[ -z "$ONLY" ]]; then
    return 0
  fi
  local code
  for code in "${ONLY_CODES[@]}"; do
    code="$(echo "$code" | tr '[:lower:]' '[:upper:]' | xargs)"
    if [[ "$code" == "$iso" ]]; then
      return 0
    fi
  done
  return 1
}

make_country_config() {
  local iso="$1"
  local name="$2"
  local slug="$3"
  local minx="$4"
  local miny="$5"
  local maxx="$6"
  local maxy="$7"
  local out_cfg="$8"

  "$PY" - "$BASE_CONFIG" "$out_cfg" "$iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" <<'PY'
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

from src.data.config import resolve_config

base_config = Path(sys.argv[1])
out_config = Path(sys.argv[2])
iso, name, slug = sys.argv[3], sys.argv[4], sys.argv[5]
minx, miny, maxx, maxy = map(float, sys.argv[6:10])

with base_config.open("r", encoding="utf-8") as handle:
    cfg = yaml.safe_load(handle) or {}

cfg = copy.deepcopy(cfg)
pad = 0.25
cfg["study_area"] = {
    "country_code": iso,
    "country_name": name,
    "slug": slug,
    "bbox": [
        max(-180.0, minx - pad),
        max(-90.0, miny - pad),
        min(180.0, maxx + pad),
        min(90.0, maxy + pad),
    ],
}

# Keep the March-May exact-data setup, but render all templates now. The worker
# passes --country-code internally, which is fine for already-rendered file names.
resolved = resolve_config(cfg)
out_config.parent.mkdir(parents=True, exist_ok=True)
with out_config.open("w", encoding="utf-8") as handle:
    yaml.safe_dump(resolved, handle, sort_keys=False, allow_unicode=True)
PY
}

era5_ready() {
  local slug="$1"
  local month
  for month in 03 04 05; do
    if [[ ! -s "$ROOT/data/raw/era5/era5-land-hourly-${slug}-2024-${month}.nc" ]]; then
      echo "[driver] ERA5 missing: data/raw/era5/era5-land-hourly-${slug}-2024-${month}.nc"
      return 1
    fi
  done
  return 0
}

run_one_country() {
  local iso="$1"
  local name="$2"
  local slug="$3"
  local minx="$4"
  local miny="$5"
  local maxx="$6"
  local maxy="$7"

  if ! should_run_country "$iso"; then
    echo "[driver] skip $iso due ONLY=$ONLY"
    return 0
  fi

  local cfg="$CFG_DIR/${CFG_PREFIX}_${iso}_datasets_2024_03_05_exact.yaml"
  local fetch_log="$LOG_DIR/${iso}_fetch.log"
  local worker_log="$LOG_DIR/${iso}_worker.log"

  echo "[driver] ===== $iso $name ====="
  make_country_config "$iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" "$cfg"
  echo "[driver] config=$cfg"

  if [[ "$SKIP_FETCH" != "1" ]]; then
    echo "[driver] fetch $iso datasets=$FETCH_DATASETS"
    if ! "$PY" -m src.data.fetch \
      --config "$cfg" \
      --datasets "$FETCH_DATASETS" \
      2>&1 | tee "$fetch_log"; then
      echo "[driver] ERROR fetch failed for $iso; see $fetch_log"
      return 1
    fi
  else
    echo "[driver] fetch skipped for $iso"
  fi

  if ! era5_ready "$slug"; then
    echo "[driver] ERROR ERA5 hourly files are not ready for $iso; fetch likely emitted a manual ERA5 record."
    echo "[driver] Configure CDS credentials and rerun fetch, or pre-place the three March-May files under data/raw/era5/."
    return 1
  fi

  local worker_extra_args=()
  if [[ "$EXPORT_OVERLAY_PARQUET" == "1" ]]; then
    worker_extra_args+=(--export-overlay-parquet)
  fi

  echo "[driver] worker $iso"
  if ! "$PY" -m src.data.run_road_hazard_overnight_worker \
    --country-code "$iso" \
    --config "$cfg" \
    --damage-config "$DAMAGE_CONFIG" \
    --thresholds-yaml "$THRESHOLDS_YAML" \
    --start-date "$START_DATE" \
    --end-date "$END_DATE" \
    --step-days "$STEP_DAYS" \
    --city-threshold "$CITY_THRESHOLD" \
    --candidate-top-n "$CANDIDATE_TOP_N" \
    --top-n-per-crop "$TOP_N_PER_CROP" \
    --min-component-nodes "$MIN_COMPONENT_NODES" \
    --spam-dir "$SPAM_DIR" \
    ${worker_extra_args+"${worker_extra_args[@]}"} \
    2>&1 | tee "$worker_log"; then
    echo "[driver] ERROR worker failed for $iso; see $worker_log"
    return 1
  fi

  echo "[driver] done $iso"
  return 0
}

FAILED=()

# ISO3 | name | slug | minx | miny | maxx | maxy
# Brazil is intentionally excluded. France is represented as French Guiana (GUF).
while IFS='|' read -r iso name slug minx miny maxx maxy; do
  [[ -z "$iso" || "$iso" == \#* ]] && continue
  if ! run_one_country "$iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy"; then
    FAILED+=("$iso")
  fi
done <<'COUNTRIES'
AGO|Angola|angola|11.64|-17.93|24.08|-4.44
BEN|Benin|benin|0.77|6.14|3.85|12.24
BRN|Brunei|brunei|114.20|4.01|115.45|5.45
BDI|Burundi|burundi|29.02|-4.50|30.83|-2.35
CMR|Cameroon|cameroon|8.49|1.73|16.01|12.86
CAF|Central African Republic|central-african-republic|14.46|2.27|27.37|11.14
COL|Colombia|colombia|-78.99|-4.30|-66.88|12.44
COG|Congo|congo|11.09|-5.04|18.45|3.73
CIV|Cote d'Ivoire|cote-divoire|-8.60|4.34|-2.56|10.52
COD|Democratic Republic of the Congo|democratic-republic-of-the-congo|12.18|-13.26|31.17|5.26
ECU|Ecuador|ecuador|-80.97|-4.96|-75.23|1.38
GNQ|Equatorial Guinea|equatorial-guinea|9.31|1.01|11.29|2.28
ETH|Ethiopia|ethiopia|32.95|3.42|47.79|14.96
GUF|French Guiana|french-guiana|-54.60|2.05|-51.50|5.90
GAB|Gabon|gabon|8.70|-3.98|14.50|2.33
GHA|Ghana|ghana|-3.24|4.71|1.06|11.10
GUY|Guyana|guyana|-61.41|1.27|-56.54|8.37
IDN|Indonesia|indonesia|95.01|-10.36|141.03|5.48
KEN|Kenya|kenya|33.89|-4.68|41.86|5.51
LBR|Liberia|liberia|-11.51|4.36|-7.54|8.54
MYS|Malaysia|malaysia|100.09|0.77|119.18|6.93
NGA|Nigeria|nigeria|2.69|4.24|14.58|13.87
PNG|Papua New Guinea|papua-new-guinea|141.00|-10.65|156.02|-2.50
PER|Peru|peru|-81.41|-18.35|-68.67|-0.06
PHL|Philippines|philippines|117.17|5.58|126.54|18.51
RWA|Rwanda|rwanda|29.02|-2.92|30.82|-1.13
SSD|South Sudan|south-sudan|24.15|3.51|35.86|12.25
SOM|Somalia|somalia|40.98|-1.68|51.13|12.02
LKA|Sri Lanka|sri-lanka|79.70|5.97|81.79|9.82
SUR|Suriname|suriname|-58.05|1.82|-53.96|6.03
TZA|Tanzania|tanzania|29.34|-11.72|40.32|-0.95
THA|Thailand|thailand|97.38|5.69|105.59|20.42
TGO|Togo|togo|-0.05|5.93|1.87|11.02
UGA|Uganda|uganda|29.58|-1.44|35.04|4.25
VEN|Venezuela|venezuela|-73.35|0.72|-59.76|12.16
COUNTRIES

echo "[driver] logs=$LOG_DIR"
echo "[driver] generated_configs=${CFG_PREFIX}_*_datasets_2024_03_05_exact.yaml in $CFG_DIR"
if (( ${#FAILED[@]} > 0 )); then
  echo "[driver] FAILED countries: ${FAILED[*]}"
  printf "%s\n" "${FAILED[@]}" > "$LOG_DIR/failed_countries.txt"
  exit 1
fi
echo "[driver] all countries completed"
