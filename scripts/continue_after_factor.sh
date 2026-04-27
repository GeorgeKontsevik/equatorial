#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WAIT_PID="${1:-}"
FACTOR_RUN_ID="${FACTOR_RUN_ID:-}"
if [[ -z "$FACTOR_RUN_ID" ]]; then
  printf 'Set FACTOR_RUN_ID to the equator-belt run id to continue after.\n' >&2
  exit 2
fi
LOG_DIR="${LOG_DIR:-$ROOT/logs/road_hazard_equator_belt_${FACTOR_RUN_ID}}"
SUPERVISOR_LOG="$LOG_DIR/continue_after_factor_supervisor.log"
COUNTRIES=(AGO BDI BRN COG CIV)

mkdir -p "$LOG_DIR"

if [[ -n "$WAIT_PID" ]]; then
  printf '[continue-supervisor] waiting_for_factor_pid=%s start=%s\n' "$WAIT_PID" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$SUPERVISOR_LOG"
  while kill -0 "$WAIT_PID" 2>/dev/null; do
    sleep 60
  done
fi
printf '[continue-supervisor] factor_done=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$SUPERVISOR_LOG"

for iso in "${COUNTRIES[@]}"; do
  log="$LOG_DIR/${iso}_continue_after_factor.log"
  printf '[continue-supervisor] %s worker_start=%s\n' "$iso" "$(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$SUPERVISOR_LOG"
    .venv/bin/python -m src.data.run_road_hazard_overnight_worker \
    --country-code "$iso" \
    --config "config/generated_equator_belt_${FACTOR_RUN_ID}_${iso}_datasets_2024_03_05_exact.yaml" \
    --damage-config config/road_climate_damage_gabon_2024_03_05.yaml \
    --thresholds-yaml config/road_hazard_thresholds_exact_mar_may.yaml \
    --start-date 2024-03-01 \
    --end-date 2024-05-31 \
    --step-days 7 \
    --city-threshold 50000 \
    --candidate-top-n 100 \
    --top-n-per-crop 3 \
    --min-component-nodes 100 \
    --spam-dir spam_tifs \
    --skip-overlay \
    > "$log" 2>&1
  rc=$?
  printf '[continue-supervisor] %s worker_done rc=%s at=%s log=%s\n' "$iso" "$rc" "$(date '+%Y-%m-%d %H:%M:%S')" "$log" | tee -a "$SUPERVISOR_LOG"
  if [[ "$rc" -ne 0 ]]; then
    tail -80 "$log" >> "$SUPERVISOR_LOG"
  fi
done

printf '[continue-supervisor] all_done=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$SUPERVISOR_LOG"
