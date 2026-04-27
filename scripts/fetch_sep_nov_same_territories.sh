#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
BASE_CONFIG="${BASE_CONFIG:-config/datasets_namibia_2024_sep_nov.yaml}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
LOG_DIR="$ROOT/logs/fetch_sep_nov_same_territories_${RUN_ID}"
CFG_DIR="$ROOT/config/generated"
CFG_PREFIX="fetch_sep_nov_${RUN_ID}"
FETCH_DATASETS="${FETCH_DATASETS:-gadm,road_surface,chirps,era5,landslide_susceptibility,visibility_noaa_isd}"
STATUS_TSV="$LOG_DIR/status.tsv"

mkdir -p "$LOG_DIR" "$CFG_DIR"
printf 'iso\tstatus\tstarted_at\tfinished_at\tconfig\tlog\n' > "$STATUS_TSV"

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
bbox = [
    max(-180.0, minx - pad),
    max(-90.0, miny - pad),
    min(180.0, maxx + pad),
    min(90.0, maxy + pad),
]
cfg["study_area"] = {
    "country_code": iso,
    "country_name": name,
    "slug": slug,
    "bbox": bbox,
}

datasets = cfg.setdefault("datasets", {})
era5 = datasets.setdefault("era5", {})
era5_request = era5.setdefault("request", {})
era5_request["area"] = [bbox[3], bbox[0], bbox[1], bbox[2]]
if "cams" in datasets:
    cams_request = datasets["cams"].setdefault("request", {})
    cams_request["area"] = [bbox[3], bbox[0], bbox[1], bbox[2]]

resolved = resolve_config(cfg)
out_config.parent.mkdir(parents=True, exist_ok=True)
with out_config.open("w", encoding="utf-8") as handle:
    yaml.safe_dump(resolved, handle, sort_keys=False, allow_unicode=True)
PY
}

run_one_country() {
  local iso="$1"
  local name="$2"
  local slug="$3"
  local minx="$4"
  local miny="$5"
  local maxx="$6"
  local maxy="$7"

  local cfg="$CFG_DIR/${CFG_PREFIX}_${iso}_datasets_2024_09_11.yaml"
  local log="$LOG_DIR/${iso}_fetch.log"
  local started
  local finished

  make_country_config "$iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" "$cfg"
  started="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[fetch-sep-nov] %s start=%s datasets=%s config=%s\n' "$iso" "$started" "$FETCH_DATASETS" "$cfg" | tee -a "$LOG_DIR/driver.log"

  if "$PY" -m src.data.fetch --config "$cfg" --datasets "$FETCH_DATASETS" > "$log" 2>&1; then
    finished="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[fetch-sep-nov] %s done status=ok finished=%s log=%s\n' "$iso" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
    printf '%s\tok\t%s\t%s\t%s\t%s\n' "$iso" "$started" "$finished" "$cfg" "$log" >> "$STATUS_TSV"
    return 0
  fi

  finished="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[fetch-sep-nov] %s done status=failed finished=%s log=%s\n' "$iso" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
  tail -80 "$log" >> "$LOG_DIR/driver.log"
  printf '%s\tfailed\t%s\t%s\t%s\t%s\n' "$iso" "$started" "$finished" "$cfg" "$log" >> "$STATUS_TSV"
  return 1
}

FAILED=()

while IFS='|' read -r iso name slug minx miny maxx maxy; do
  [[ -z "$iso" || "$iso" == \#* ]] && continue
  if ! run_one_country "$iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy"; then
    FAILED+=("$iso")
  fi
done <<'COUNTRIES'
AGO|Angola|angola|11.64|-17.93|24.08|-4.44
BDI|Burundi|burundi|29.02|-4.50|30.83|-2.35
BRN|Brunei|brunei|114.20|4.01|115.45|5.45
COG|Congo|congo|11.09|-5.04|18.45|3.73
CIV|Cote d'Ivoire|cote-divoire|-8.60|4.34|-2.56|10.52
COUNTRIES

if (( ${#FAILED[@]} > 0 )); then
  printf '%s\n' "${FAILED[@]}" > "$LOG_DIR/failed_countries.txt"
  printf '[fetch-sep-nov] failed countries: %s\n' "${FAILED[*]}" | tee -a "$LOG_DIR/driver.log"
  exit 1
fi

printf '[fetch-sep-nov] all countries completed\n' | tee -a "$LOG_DIR/driver.log"
