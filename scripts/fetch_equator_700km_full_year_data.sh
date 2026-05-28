#!/usr/bin/env bash
set -u -o pipefail

# Generate and optionally run full-year 2024 raw-data fetch configs for the
# equator sample.
#
# Default is DRY_RUN=1: configs and planned commands are printed, but no fetch
# is executed. To run:
#   DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#
# Useful overrides:
#   ONLY="AGO,BDI,BRN" DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   TIMEOUT_SECONDS=900 DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   SKIP_FLOOD=1 DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   MISSING_ONLY=0 DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   SAMPLE_RADIUS_KM=700 DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   SKIP_WORLDCOVER=0 FETCH_DATASETS=worldcover DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh
#   FETCH_DATASETS=era5 ERA5_VARIABLES=total_precipitation DRY_RUN=0 bash scripts/fetch_equator_700km_full_year_data.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-$ROOT/.venv/bin/python}"
BASE_CONFIG="${BASE_CONFIG:-config/datasets_gabon_2024_03_05_exact.yaml}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
LOG_DIR="$ROOT/logs/fetch_equator_700km_full_year_${RUN_ID}"
CFG_DIR="$ROOT/config/generated/full_year_2024_${RUN_ID}"
CFG_PREFIX="equator_700km_full_year_${RUN_ID}"
STATUS_TSV="$LOG_DIR/status.tsv"

START_DATE="${START_DATE:-2024-01-01}"
END_DATE="${END_DATE:-2024-12-31}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
MAX_RETRIES="${MAX_RETRIES:-4}"
MAX_STATIONS="${MAX_STATIONS:-12}"
FLOOD_MAX_ITEMS_PER_WEEK="${FLOOD_MAX_ITEMS_PER_WEEK:-500}"
BBOX_PAD_DEG="${BBOX_PAD_DEG:-1.0}"
SAMPLE_RADIUS_KM="${SAMPLE_RADIUS_KM:-700}"
DRY_RUN="${DRY_RUN:-1}"
MISSING_ONLY="${MISSING_ONLY:-1}"
SKIP_FLOOD="${SKIP_FLOOD:-0}"
SKIP_WORLDCOVER="${SKIP_WORLDCOVER:-1}"
ONLY="${ONLY:-}"
FETCH_DATASETS="${FETCH_DATASETS:-gadm,road_surface,chirps,era5,landslide_susceptibility,visibility_noaa_isd,flood,gem,liquefaction,flopros}"
ERA5_VARIABLES="${ERA5_VARIABLES:-2m_temperature,skin_temperature,total_precipitation,volumetric_soil_water_layer_1,10m_u_component_of_wind,10m_v_component_of_wind}"

if [[ "$SKIP_WORLDCOVER" == "1" ]]; then
  filtered_fetch_datasets=()
  IFS=',' read -r -a requested_fetch_datasets <<< "$FETCH_DATASETS"
  for dataset_name in "${requested_fetch_datasets[@]}"; do
    dataset_name="$(echo "$dataset_name" | xargs)"
    if [[ -n "$dataset_name" && "$dataset_name" != "worldcover" ]]; then
      filtered_fetch_datasets+=("$dataset_name")
    fi
  done
  (IFS=','; FETCH_DATASETS="${filtered_fetch_datasets[*]}")
fi

mkdir -p "$LOG_DIR" "$CFG_DIR"
printf 'sample_iso\tfetch_iso\tstatus\tstarted_at\tfinished_at\tdatasets\tconfig\tlog\n' > "$STATUS_TSV"

export PYTHONUNBUFFERED=1

IFS=',' read -r -a ONLY_CODES <<< "$ONLY"

should_run_country() {
  local sample_iso="$1"
  local fetch_iso="$2"
  if [[ -z "$ONLY" ]]; then
    return 0
  fi
  local code
  for code in "${ONLY_CODES[@]}"; do
    code="$(echo "$code" | tr '[:lower:]' '[:upper:]' | xargs)"
    if [[ "$code" == "$sample_iso" || "$code" == "$fetch_iso" ]]; then
      return 0
    fi
  done
  return 1
}

has_dataset() {
  local needle="$1"
  [[ ",$FETCH_DATASETS," == *",$needle,"* ]]
}

lower_iso() {
  echo "$1" | tr '[:upper:]' '[:lower:]'
}

chirps_complete() {
  "$PY" - "$ROOT" "$START_DATE" "$END_DATE" <<'PY'
from datetime import datetime, timedelta
from pathlib import Path
import sys

root = Path(sys.argv[1])
start = datetime.strptime(sys.argv[2], "%Y-%m-%d").date()
end = datetime.strptime(sys.argv[3], "%Y-%m-%d").date()
day = start
while day <= end:
    path = root / "data" / "raw" / "chirps" / "global" / "daily" / "sat" / str(day.year) / f"chirps-v3.0.sat.{day:%Y.%m.%d}.tif"
    if not path.exists() or path.stat().st_size <= 0:
        sys.exit(1)
    day += timedelta(days=1)
sys.exit(0)
PY
}

era5_complete() {
  local slug="$1"
  local month
  for month in 01 02 03 04 05 06 07 08 09 10 11 12; do
    [[ -s "$ROOT/data/raw/era5/era5-land-hourly-${slug}-2024-${month}.nc" ]] || return 1
  done
  return 0
}

landslide_complete() {
  local slug="$1"
  "$PY" - "$ROOT/data/raw/landslide_susceptibility/global/nasa_landslide_susceptibility_${slug}.tif" <<'PY'
import sys
from pathlib import Path

import rasterio

path = Path(sys.argv[1])
if not path.exists() or path.stat().st_size <= 0:
    sys.exit(1)
try:
    with rasterio.open(path) as dataset:
        if dataset.width <= 0 or dataset.height <= 0:
            sys.exit(1)
except Exception:
    sys.exit(1)
sys.exit(0)
PY
}

visibility_complete() {
  local iso="$1"
  [[ -s "$ROOT/data/raw/visibility_noaa_isd/$iso/stations.csv" ]] || return 1
  compgen -G "$ROOT/data/raw/visibility_noaa_isd/$iso/2024/*.csv" >/dev/null
}

flood_present() {
  # GFM files are not country-keyed in the raw tree; this only avoids a full
  # refetch when the user explicitly wants missing-only and some GFM cache exists.
  compgen -G "$ROOT/data/raw/flood/copernicus_gfm/GFM/2024/*/*.tif" >/dev/null
}

missing_datasets_for_country() {
  local fetch_iso="$1"
  local slug="$2"
  local minx="$3"
  local miny="$4"
  local maxx="$5"
  local maxy="$6"
  local out=()

  if [[ "$MISSING_ONLY" != "1" ]]; then
    local ds
    IFS=',' read -r -a out <<< "$FETCH_DATASETS"
    if [[ "$SKIP_FLOOD" == "1" ]]; then
      local filtered=()
      for ds in "${out[@]}"; do [[ "$ds" != "flood" ]] && filtered+=("$ds"); done
      out=("${filtered[@]}")
    fi
    (IFS=','; echo "${out[*]-}")
    return 0
  fi

  if has_dataset gadm && [[ ! -s "$ROOT/data/raw/gadm/$fetch_iso/gadm41_${fetch_iso}.gpkg" ]]; then
    out+=("gadm")
  fi
  local fetch_iso_lower
  fetch_iso_lower="$(lower_iso "$fetch_iso")"
  if has_dataset road_surface && [[ ! -s "$ROOT/data/raw/road_surface/$fetch_iso/heigit_${fetch_iso_lower}_roadsurface_lines.gpkg" ]]; then
    out+=("road_surface")
  fi
  if has_dataset chirps && ! chirps_complete; then
    out+=("chirps")
  fi
  if has_dataset era5 && ! era5_complete "$slug"; then
    out+=("era5")
  fi
  if has_dataset landslide_susceptibility && ! landslide_complete "$slug"; then
    out+=("landslide_susceptibility")
  fi
  if has_dataset visibility_noaa_isd && ! visibility_complete "$fetch_iso"; then
    out+=("visibility_noaa_isd")
  fi
  if has_dataset flood && [[ "$SKIP_FLOOD" != "1" ]]; then
    out+=("flood")
  fi
  if has_dataset gem && [[ ! -s "$ROOT/data/raw/gem/global/v2023_1_pga_475_rock_3min.tif" ]]; then
    out+=("gem")
  fi
  if has_dataset liquefaction && [[ ! -s "$ROOT/data/raw/liquefaction/global/liquefaction_v1_deg.tif" ]]; then
    out+=("liquefaction")
  fi
  if has_dataset flopros && [[ ! -s "$ROOT/data/raw/flopros/global/original/Scussolini_etal_Suppl_info/FLOPROS_shp_V1/FLOPROS_shp_V1.shp" ]]; then
    out+=("flopros")
  fi

  (IFS=','; echo "${out[*]-}")
}

make_country_config() {
  local sample_iso="$1"
  local fetch_iso="$2"
  local name="$3"
  local slug="$4"
  local minx="$5"
  local miny="$6"
  local maxx="$7"
  local maxy="$8"
  local out_cfg="$9"

  "$PY" - "$BASE_CONFIG" "$out_cfg" "$sample_iso" "$fetch_iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" "$START_DATE" "$END_DATE" "$TIMEOUT_SECONDS" "$MAX_RETRIES" "$MAX_STATIONS" "$FLOOD_MAX_ITEMS_PER_WEEK" "$BBOX_PAD_DEG" "$FETCH_DATASETS" "$ERA5_VARIABLES" <<'PY'
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

from src.data.config import resolve_config

base_config = Path(sys.argv[1])
out_config = Path(sys.argv[2])
sample_iso, fetch_iso, name, slug = sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
minx, miny, maxx, maxy = map(float, sys.argv[7:11])
start_date, end_date = sys.argv[11], sys.argv[12]
timeout_seconds = int(float(sys.argv[13]))
max_retries = int(sys.argv[14])
max_stations = int(sys.argv[15])
flood_max_items_per_week = int(sys.argv[16])
bbox_pad_deg = float(sys.argv[17])
selected_datasets = {item.strip() for item in sys.argv[18].split(",") if item.strip()}
era5_variables = [item.strip() for item in sys.argv[19].split(",") if item.strip()]
if not era5_variables:
    era5_variables = ["total_precipitation"]

with base_config.open("r", encoding="utf-8") as handle:
    cfg = yaml.safe_load(handle) or {}

cfg = copy.deepcopy(cfg)
bbox = [
    max(-180.0, minx - bbox_pad_deg),
    max(-90.0, miny - bbox_pad_deg),
    min(180.0, maxx + bbox_pad_deg),
    min(90.0, maxy + bbox_pad_deg),
]

cfg.setdefault("global", {})
cfg["global"]["timeout_seconds"] = timeout_seconds
cfg["global"]["max_retries"] = max_retries

cfg["study_area"] = {
    "country_code": fetch_iso,
    "country_name": name,
    "slug": slug,
    "bbox": bbox,
}

datasets = cfg.setdefault("datasets", {})
for key in selected_datasets:
    datasets.setdefault(key, {})
    datasets[key]["enabled"] = True
    datasets[key]["bbox"] = bbox
    datasets[key]["country_code"] = fetch_iso

datasets["gadm"]["country_codes"] = [fetch_iso]
datasets["road_surface"]["country_codes"] = [fetch_iso]

chirps = datasets["chirps"]
chirps.update(
    {
        "version": "v3.0",
        "frequency": "daily",
        "region": "global",
        "daily_variant": "sat",
        "start_date": start_date,
        "end_date": end_date,
        "temporal_resolution": "daily",
    }
)

months = [f"{month:02d}" for month in range(1, 13)]
days = [f"{day:02d}" for day in range(1, 32)]
times = [f"{hour:02d}:00" for hour in range(24)]
era5 = datasets["era5"]
era5["dataset_id"] = "reanalysis-era5-land"
era5["spatial_resolution_raw"] = "0.1 degree grid"
era5["temporal_resolution"] = "hourly"
era5["start_date"] = start_date
era5["end_date"] = end_date
era5["source_files"] = [f"era5-land-hourly-{slug}-2024-{month}.nc" for month in months]
base_request = {
    "product_type": "reanalysis",
    "variable": era5_variables,
    "year": ["2024"],
    "day": days,
    "time": times,
    "data_format": "netcdf",
    "download_format": "unarchived",
    "area": [bbox[3], bbox[0], bbox[1], bbox[2]],
}
era5["requests"] = [
    {
        "target_filename": f"era5-land-hourly-{slug}-2024-{month}.nc",
        "request": {**base_request, "month": [month]},
    }
    for month in months
]
era5.pop("request", None)
era5.pop("target_filename", None)

landslide = datasets["landslide_susceptibility"]
landslide["target_slug"] = slug
landslide.setdefault(
    "source_url",
    "https://gis.earthdata.nasa.gov/gis05/rest/services/Landslides/Global_Landslide_Susceptibility/ImageServer",
)
landslide.setdefault("export_resolution_deg", 0.008333333333333333)

visibility = datasets["visibility_noaa_isd"]
visibility["start_date"] = start_date
visibility["end_date"] = end_date
visibility["max_stations"] = max_stations

flood = datasets["flood"]
flood["enabled"] = True
flood["start_date"] = start_date
flood["end_date"] = end_date
flood["aggregation_period_days"] = 7
flood["max_items_per_week"] = flood_max_items_per_week
flood["asset_key"] = "ensemble_flood_extent"
flood["collection_id"] = "GFM"
flood["product"] = "GFM"
flood["spatial_resolution_raw"] = "20 m Sentinel-1 SAR flood extent, not flood depth"
flood["temporal_resolution"] = "event-based SAR acquisitions"

worldcover = datasets.setdefault("worldcover", {})
worldcover["enabled"] = False
worldcover.setdefault("year", 2021)
worldcover.setdefault("version", "v200")
worldcover.setdefault("layer", "Map")

if "flood_depth" in datasets:
    datasets["flood_depth"]["enabled"] = False

cfg.setdefault("notes", {})
cfg["notes"]["sample_iso"] = sample_iso
if sample_iso != fetch_iso:
    cfg["notes"]["sample_iso_alias"] = f"{sample_iso} from the 700 km country list is fetched as {fetch_iso} for GADM/HeiGIT compatibility."

resolved = resolve_config(cfg)
out_config.parent.mkdir(parents=True, exist_ok=True)
with out_config.open("w", encoding="utf-8") as handle:
    yaml.safe_dump(resolved, handle, sort_keys=False, allow_unicode=True)
PY
}

run_one_country() {
  local sample_iso="$1"
  local fetch_iso="$2"
  local name="$3"
  local slug="$4"
  local minx="$5"
  local miny="$6"
  local maxx="$7"
  local maxy="$8"
  local min_radius="${9:-500}"

  if ! should_run_country "$sample_iso" "$fetch_iso"; then
    echo "[full-year-fetch] skip $sample_iso/$fetch_iso due ONLY=$ONLY"
    return 0
  fi
  if (( min_radius > SAMPLE_RADIUS_KM )); then
    echo "[full-year-fetch] skip $sample_iso/$fetch_iso min_radius=${min_radius}km for SAMPLE_RADIUS_KM=$SAMPLE_RADIUS_KM"
    return 0
  fi

  local cfg="$CFG_DIR/${CFG_PREFIX}_${fetch_iso}_datasets_2024_full_year.yaml"
  local log="$LOG_DIR/${fetch_iso}_fetch.log"
  local datasets_csv
  local started
  local finished

  make_country_config "$sample_iso" "$fetch_iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" "$cfg"
  if ! datasets_csv="$(missing_datasets_for_country "$fetch_iso" "$slug" "$minx" "$miny" "$maxx" "$maxy")"; then
    echo "[full-year-fetch] ERROR missing-data check failed for $sample_iso/$fetch_iso"
    return 1
  fi

  if [[ -z "$datasets_csv" ]]; then
    echo "[full-year-fetch] $sample_iso/$fetch_iso already complete for selected datasets; config=$cfg"
    printf '%s\t%s\talready_complete\t\t\t\t%s\t\n' "$sample_iso" "$fetch_iso" "$cfg" >> "$STATUS_TSV"
    return 0
  fi

  started="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[full-year-fetch] $sample_iso/$fetch_iso datasets=$datasets_csv config=$cfg log=$log"

  if [[ "$DRY_RUN" == "1" ]]; then
    printf '%s\t%s\tdry_run\t%s\t\t%s\t%s\t%s\n' "$sample_iso" "$fetch_iso" "$started" "$datasets_csv" "$cfg" "$log" >> "$STATUS_TSV"
    return 0
  fi

  if "$PY" -m src.data.fetch --config "$cfg" --datasets "$datasets_csv" > "$log" 2>&1; then
    finished="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s\t%s\tok\t%s\t%s\t%s\t%s\t%s\n' "$sample_iso" "$fetch_iso" "$started" "$finished" "$datasets_csv" "$cfg" "$log" >> "$STATUS_TSV"
    return 0
  fi

  finished="$(date '+%Y-%m-%d %H:%M:%S')"
  printf '%s\t%s\tfailed\t%s\t%s\t%s\t%s\t%s\n' "$sample_iso" "$fetch_iso" "$started" "$finished" "$datasets_csv" "$cfg" "$log" >> "$STATUS_TSV"
  tail -80 "$log" || true
  return 1
}

FAILED=()

# Columns:
# sample_iso | fetch_iso | country_name | slug | minx | miny | maxx | maxy | min_radius_km
#
# Notes:
# - The 700 km list labels South Sudan as SDS; GADM and HeiGIT use SSD.
# - FRA is included because the list intersects France through French Guiana;
#   bbox below is French Guiana's bbox, while the country code remains FRA.
while IFS='|' read -r sample_iso fetch_iso name slug minx miny maxx maxy min_radius; do
  [[ -z "$sample_iso" || "$sample_iso" == \#* ]] && continue
  if ! run_one_country "$sample_iso" "$fetch_iso" "$name" "$slug" "$minx" "$miny" "$maxx" "$maxy" "$min_radius"; then
    FAILED+=("$sample_iso/$fetch_iso")
  fi
done <<'COUNTRIES'
# Processed in run era5_tp_remaining_20260517_203158 or already complete there.
#BRA|BRA|Brazil|brazil|-73.99|-33.75|-28.84|5.27|500
#COD|COD|Democratic Republic of the Congo|democratic-republic-of-the-congo|12.18|-13.26|31.17|5.26|500
#IDN|IDN|Indonesia|indonesia|95.01|-10.36|141.03|5.48|500
#PER|PER|Peru|peru|-81.41|-18.35|-68.67|-0.06|500
#AGO|AGO|Angola|angola|11.64|-17.93|24.08|-4.44|500
#COL|COL|Colombia|colombia|-78.99|-4.30|-66.88|12.44|500
#ETH|ETH|Ethiopia|ethiopia|32.95|3.42|47.79|14.96|500
#TZA|TZA|United Republic of Tanzania|tanzania|29.34|-11.72|40.32|-0.95|500
VEN|VEN|Venezuela|venezuela|-73.35|0.72|-59.76|12.16|500
NGA|NGA|Nigeria|nigeria|2.69|4.24|14.58|13.87|500
FRA|FRA|France|france|-54.60|2.05|-51.50|5.90|500
SDS|SSD|South Sudan|south-sudan|24.15|3.51|35.86|12.25|500
CAF|CAF|Central African Republic|central-african-republic|14.46|2.27|27.37|11.14|500
KEN|KEN|Kenya|kenya|33.89|-4.68|41.86|5.51|500
SOM|SOM|Somalia|somalia|40.98|-1.68|51.13|12.02|500
PNG|PNG|Papua New Guinea|papua-new-guinea|141.00|-10.65|156.02|-2.50|500
CMR|CMR|Cameroon|cameroon|8.49|1.73|16.01|12.86|500
COG|COG|Republic of the Congo|congo|11.09|-5.04|18.45|3.73|500
MYS|MYS|Malaysia|malaysia|100.09|0.77|119.18|6.93|500
CIV|CIV|Ivory Coast|cote-divoire|-8.60|4.34|-2.56|10.52|500
GAB|GAB|Gabon|gabon|8.70|-3.98|14.50|2.33|500
ECU|ECU|Ecuador|ecuador|-80.97|-4.96|-75.23|1.38|500
UGA|UGA|Uganda|uganda|29.58|-1.44|35.04|4.25|500
GUY|GUY|Guyana|guyana|-61.41|1.27|-56.54|8.37|500
SUR|SUR|Suriname|suriname|-58.05|1.82|-53.96|6.03|500
LBR|LBR|Liberia|liberia|-11.51|4.36|-7.54|8.54|500
GNQ|GNQ|Equatorial Guinea|equatorial-guinea|9.31|1.01|11.29|2.28|500
BDI|BDI|Burundi|burundi|29.02|-4.50|30.83|-2.35|500
RWA|RWA|Rwanda|rwanda|29.02|-2.92|30.82|-1.13|500
BRN|BRN|Brunei|brunei|114.20|4.01|115.45|5.45|500
THA|THA|Thailand|thailand|97.38|5.69|105.59|20.42|700
PHL|PHL|Philippines|philippines|117.17|5.58|126.54|18.51|700
GHA|GHA|Ghana|ghana|-3.24|4.71|1.06|11.10|700
BEN|BEN|Benin|benin|0.77|6.14|3.85|12.24|700
LKA|LKA|Sri Lanka|sri-lanka|79.70|5.97|81.79|9.82|700
TGO|TGO|Togo|togo|-0.05|5.93|1.87|11.02|700
COUNTRIES

echo "[full-year-fetch] status=$STATUS_TSV"
echo "[full-year-fetch] configs=$CFG_DIR"
echo "[full-year-fetch] logs=$LOG_DIR"

if (( ${#FAILED[@]} > 0 )); then
  printf '%s\n' "${FAILED[@]}" > "$LOG_DIR/failed_countries.txt"
  echo "[full-year-fetch] failed countries: ${FAILED[*]}"
  exit 1
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[full-year-fetch] dry run only; set DRY_RUN=0 to fetch."
else
  echo "[full-year-fetch] completed."
fi
