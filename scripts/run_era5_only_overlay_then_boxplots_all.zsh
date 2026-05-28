set -euo pipefail
export PYTHONWARNINGS="ignore::FutureWarning"

EQ_ROOT="/Users/gk/Code/super-duper-disser/equatorial"
PY="$EQ_ROOT/.venv/bin/python"
POSTGIS_DSN="${POSTGIS_DSN:-}"

RUN_ID="era5_only_overlay_then_boxes_$(date +%Y%m%d_%H%M%S)"
CFG_DIR="$EQ_ROOT/config/generated/full_year_2024_era5_tp_remaining_20260517_203158"
TMP_ROOT="$EQ_ROOT/config/generated/${RUN_ID}_tmp"
mkdir -p "$TMP_ROOT"
export TMP_ROOT

ERA5_THRESH="$TMP_ROOT/road_hazard_thresholds_era5_only.yaml"
DAMAGE_CFG="$TMP_ROOT/road_climate_damage_2024_full_year.yaml"

"$PY" - <<'PY'
from pathlib import Path
import yaml
import os

root = Path("/Users/gk/Code/super-duper-disser/equatorial")
tmp = Path(os.environ["TMP_ROOT"])
base = root / "config/road_hazard_thresholds_exact_mar_may.yaml"
out = tmp / "road_hazard_thresholds_era5_only.yaml"

doc = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
rules = ((doc.get("road_hazard_thresholds") or {}).get("rules") or [])
doc.setdefault("road_hazard_thresholds", {})["rules"] = [
    r for r in rules if str((r or {}).get("factor", "")).startswith("era5_")
]
out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"[era5-only] thresholds={out}")
PY

"$PY" - <<'PY'
from pathlib import Path
import yaml

base = Path("/Users/gk/Code/super-duper-disser/equatorial/config/road_climate_damage.yaml")
out = Path(__import__("os").environ["TMP_ROOT"]) / "road_climate_damage_2024_full_year.yaml"
doc = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
rcd = doc.setdefault("road_climate_damage", {})
period = rcd.setdefault("analysis_period", {})
period["start_date"] = "2024-01-01"
period["end_date"] = "2024-12-31"
period["aggregation_period_days"] = 7
out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
print(f"[era5-only] damage_config={out}")
PY

ISO_LIST=()
for cfg in "$CFG_DIR"/equator_700km_full_year_era5_tp_remaining_20260517_203158_*_datasets_2024_full_year.yaml; do
  iso="$(basename "$cfg" | sed -E 's/.*_([A-Z]{3})_datasets_2024_full_year\.yaml/\1/')"
  out_cfg="$TMP_ROOT/$(basename "$cfg" .yaml)_era5_only.yaml"

  CFG_IN="$cfg" CFG_OUT="$out_cfg" "$PY" - <<'PY'
from pathlib import Path
import os, yaml

src = Path(os.environ["CFG_IN"])
dst = Path(os.environ["CFG_OUT"])
doc = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
ds = doc.setdefault("datasets", {})

for k in list(ds.keys()):
    ds[k]["enabled"] = (k in {"gadm", "road_surface", "era5"})
if "flood_depth" in ds:
    ds["flood_depth"]["enabled"] = False

era5 = ds.setdefault("era5", {})
era5["enabled"] = True
era5["variables"] = [
    "total_precipitation",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
]

dst.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

  ISO_LIST+=("$iso")
done

for iso in "${ISO_LIST[@]}"; do
  cfg="$TMP_ROOT/equator_700km_full_year_era5_tp_remaining_20260517_203158_${iso}_datasets_2024_full_year_era5_only.yaml"
  overlay_out="$EQ_ROOT/outputs/road_multisource_overlay/$iso/2024-01-01_to_2024-12-31_7d"
  echo "=== OVERLAY $iso ==="
  "$PY" -m src.data.run_multisource_road_overlay \
    --config "$cfg" \
    --country-code "$iso" \
    --damage-config "$DAMAGE_CFG" \
    --road-geometry-mode probe_point \
    --point-batch-size 1000000 \
    --road-chunk-size 1000000 \
    --road-backend postgis \
    --postgis-dsn "$POSTGIS_DSN" \
    --postgis-table "road_surface_${iso:l}" \
    --compact-weekly-logs \
    --era5-precip-only \
    --skip-era5-daily-sum-max \
    --multiscale-road-merge \
    --output-root "$overlay_out"
done

for iso in "${ISO_LIST[@]}"; do
  cfg="$TMP_ROOT/equator_700km_full_year_era5_tp_remaining_20260517_203158_${iso}_datasets_2024_full_year_era5_only.yaml"
  out_root="$EQ_ROOT/outputs/road_weekly_scenarios/$iso/2024_full_year_${RUN_ID}"
  echo "=== BOXPLOTS $iso ==="
  "$PY" -m src.data.run_weekly_factor_boxplots_streaming \
    --config "$cfg" \
    --country-code "$iso" \
    --damage-config "$DAMAGE_CFG" \
    --thresholds-yaml "$ERA5_THRESH" \
    --aggregation-unit cell \
    --road-backend postgis \
    --postgis-dsn "$POSTGIS_DSN" \
    --postgis-table "road_surface_${iso:l}" \
    --chunk-size 2000000 \
    --output-root "$out_root/factor_boxplots_cell"
done

echo "DONE: $RUN_ID"
