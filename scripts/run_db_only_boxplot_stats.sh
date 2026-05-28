#!/usr/bin/env bash
set -euo pipefail

DB_URL="${DB_URL:-postgresql://gk@127.0.0.1:5432/equatorial}"
COUNTRY_CODE="${COUNTRY_CODE:?set COUNTRY_CODE}"
START_DATE="${START_DATE:-2024-01-01}"
END_DATE="${END_DATE:-2024-12-31}"
ROOT="/Users/gk/Code/super-duper-disser/equatorial"
FULL_REFRESH="${FULL_REFRESH:-0}"
BOX_UNIT="${BOX_UNIT:-cell}"   # cell | road
CELL_DEG="${CELL_DEG:-0.1}"    # ERA5 grid step in degrees
PREPARE_DB="${PREPARE_DB:-0}"  # 1 = run schema/partition DDL before compute
if [[ "$BOX_UNIT" == "cell" ]]; then
  BOX_SQL="$ROOT/sql/02_compute_boxplot_stats_cells.sql"
else
  BOX_SQL="$ROOT/sql/02_compute_boxplot_stats.sql"
fi

if [[ "$PREPARE_DB" == "1" ]]; then
  psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$ROOT/sql/01_db_only_schema.sql"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -f "$ROOT/sql/03_optimize_country_partitions.sql"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -c "SELECT eq.ensure_list_partition('eq','road_weekly_factors','${COUNTRY_CODE}');"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -c "SELECT eq.ensure_list_partition('eq','boxplot_stats_weekly','${COUNTRY_CODE}');"
fi
psql "$DB_URL" -v ON_ERROR_STOP=1 \
  -v country_code="$COUNTRY_CODE" \
  -v cell_deg="$CELL_DEG" \
  -f "$ROOT/sql/05_build_road_cell_map.sql"

if [[ "$FULL_REFRESH" == "1" ]]; then
  echo "[boxplot] clear existing rows for country=${COUNTRY_CODE}"
  psql "$DB_URL" -v ON_ERROR_STOP=1 -c "DELETE FROM eq.boxplot_stats_weekly WHERE country_code='${COUNTRY_CODE}';"
fi

WEEKS_STR="$(
python3 - "$START_DATE" "$END_DATE" <<'PY'
from datetime import date, timedelta
import sys
s = date.fromisoformat(sys.argv[1])
e = date.fromisoformat(sys.argv[2])
d = s
while d <= e:
    print(d.isoformat())
    d += timedelta(days=7)
PY
)"
ws=""
while IFS= read -r ws || [[ -n "${ws:-}" ]]; do
  [[ -z "${ws:-}" ]] && continue
  if [[ "$FULL_REFRESH" != "1" ]]; then
    existing_rows="$(
      psql "$DB_URL" -Atc \
      "SELECT count(*) FROM eq.boxplot_stats_weekly WHERE country_code='${COUNTRY_CODE}' AND week_start='${ws:-}'::date;"
    )"
    if [[ "${existing_rows:-0}" -ge 18 ]]; then
      echo "[boxplot] country=${COUNTRY_CODE} week skip ${ws:-} already_rows=${existing_rows}"
      continue
    fi
  fi
  echo "[boxplot] country=${COUNTRY_CODE} week start ${ws:-}"
  psql "$DB_URL" -v ON_ERROR_STOP=1 \
    -v country_code="$COUNTRY_CODE" \
    -v start_date="${ws:-}" \
    -v end_date="${ws:-}" \
    -f "$BOX_SQL" >/dev/null
  echo "[boxplot] country=${COUNTRY_CODE} week done ${ws:-}"
done < <(printf '%s\n' "$WEEKS_STR")

psql "$DB_URL" -v ON_ERROR_STOP=1 -c "
SELECT country_code, min(week_start) AS min_week, max(week_start) AS max_week, count(*) AS rows
FROM eq.boxplot_stats_weekly
WHERE country_code = '$COUNTRY_CODE'
GROUP BY country_code;"
