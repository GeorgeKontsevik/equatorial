#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
LOG_DIR="$ROOT/logs/sep_nov_full_fetch_then_factor_plots_${RUN_ID}"
CFG_DIR="$ROOT/config/generated"
CFG_PREFIX="sep_nov_full_${RUN_ID}"
BASE_PREFIX="${BASE_PREFIX:-}"
DAMAGE_CONFIG="${DAMAGE_CONFIG:-config/road_climate_damage_namibia_2024_sep_nov.yaml}"
THRESHOLDS_YAML="${THRESHOLDS_YAML:-config/road_hazard_thresholds_exact_mar_may.yaml}"
FETCH_DATASETS="${FETCH_DATASETS:-gadm,road_surface,chirps,era5,flood,landslide_susceptibility,cams,gem,liquefaction,flopros,visibility_noaa_isd}"
STATUS_TSV="$LOG_DIR/status.tsv"

mkdir -p "$LOG_DIR" "$CFG_DIR"
printf 'iso\tstage\tstatus\tstarted_at\tfinished_at\tconfig\tlog\n' > "$STATUS_TSV"

load_dotenv() {
  local env_path="$ROOT/.env"
  if [[ ! -f "$env_path" ]]; then
    return 0
  fi
  local xtrace_was_on=0
  case "$-" in
    *x*) xtrace_was_on=1; set +x ;;
  esac
  eval "$("$PY" - "$env_path" <<'PY'
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
key_re = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    match = key_re.match(line)
    if not match:
        continue
    key, value = match.groups()
    value = value.strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}")
PY
)"
  if (( xtrace_was_on )); then
    set -x
  fi
}

check_critical_fetch() {
  local log="$1"
  "$PY" - "$log" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

critical = {"era5", "cams"}
log = Path(sys.argv[1])
manual: set[str] = set()
section = None
for raw_line in log.read_text(encoding="utf-8", errors="replace").splitlines():
    line = raw_line.rstrip()
    if line == "Manual datasets:":
        section = "manual"
        continue
    if line.endswith("datasets:") and line != "Manual datasets:":
        section = None
    if section == "manual" and line.strip().startswith("- "):
        manual.add(line.strip()[2:].strip())
missing = sorted(critical & manual)
if missing:
    print("critical manual datasets after fetch: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
PY
}

load_dotenv

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
  printf '[sep-nov-full] %s %s start=%s\n' "$iso" "$stage" "$started" | tee -a "$LOG_DIR/driver.log"
  if "$@" > "$log" 2>&1; then
    finished="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[sep-nov-full] %s %s done status=ok finished=%s log=%s\n' "$iso" "$stage" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
    printf '%s\t%s\tok\t%s\t%s\t%s\t%s\n' "$iso" "$stage" "$started" "$finished" "$CURRENT_CFG" "$log" >> "$STATUS_TSV"
    return 0
  fi
  finished="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '[sep-nov-full] %s %s done status=failed finished=%s log=%s\n' "$iso" "$stage" "$finished" "$log" | tee -a "$LOG_DIR/driver.log"
  tail -80 "$log" >> "$LOG_DIR/driver.log"
  printf '%s\t%s\tfailed\t%s\t%s\t%s\t%s\n' "$iso" "$stage" "$started" "$finished" "$CURRENT_CFG" "$log" >> "$STATUS_TSV"
  return 1
}

FAILED=()
COUNTRY_LIST="${COUNTRIES:-AGO BDI BRN COG CIV}"
read -r -a COUNTRY_CODES <<< "$COUNTRY_LIST"

for iso in "${COUNTRY_CODES[@]}"; do
  CURRENT_CFG="$(make_full_config "$iso")"
  fetch_log="$LOG_DIR/${iso}_fetch.log"
  if ! run_stage "$iso" "fetch" "$fetch_log" "$PY" -m src.data.fetch --config "$CURRENT_CFG" --country-code "$iso" --datasets "$FETCH_DATASETS"; then
    FAILED+=("${iso}:fetch")
    continue
  fi
  if ! check_critical_fetch "$fetch_log"; then
    printf '[sep-nov-full] %s fetch missing critical downloaded datasets; see %s\n' "$iso" "$fetch_log" | tee -a "$LOG_DIR/driver.log"
    FAILED+=("${iso}:fetch_critical")
    continue
  fi

  overlay_log="$LOG_DIR/${iso}_overlay.log"
  if ! run_stage "$iso" "overlay" "$overlay_log" "$PY" -m src.data.run_multisource_road_overlay --config "$CURRENT_CFG" --country-code "$iso" --damage-config "$DAMAGE_CONFIG"; then
    FAILED+=("${iso}:overlay")
    continue
  fi

  plot_log="$LOG_DIR/${iso}_factor_plots.log"
  overlay_dir="$ROOT/outputs/road_multisource_overlay/$iso/2024-09-01_to_2024-11-30_7d"
  if ! run_stage "$iso" "factor_plots" "$plot_log" "$PY" -m src.data.plot_weekly_factor_threshold_diagnostics --results-dir "$overlay_dir" --overlay-gpkg "$overlay_dir/roads_with_multisource_overlay.gpkg" --thresholds-yaml "$THRESHOLDS_YAML"; then
    FAILED+=("${iso}:factor_plots")
  fi
done

if (( ${#FAILED[@]} > 0 )); then
  printf '%s\n' "${FAILED[@]}" > "$LOG_DIR/failed_stages.txt"
  printf '[sep-nov-full] failed stages: %s\n' "${FAILED[*]}" | tee -a "$LOG_DIR/driver.log"
  exit 1
fi

printf '[sep-nov-full] all stages completed\n' | tee -a "$LOG_DIR/driver.log"
