# DB Country Crop Accessibility Runbook

Goal: reproduce the Brazil DB-first workflow for the next countries: roads in PostGIS, ERA5 weekly precipitation by cell, road-to-cell mapping, cell-level boxplots, crop origins, cities, pgRouting graph, and weekly crop accessibility to nearest cities.

This runbook documents what we actually did for `BRA`. Replace `BRA`, `bra`, and country-specific config filenames for other countries.

## Current DB Assumptions

- Database URL used in this session: `postgresql://gk@127.0.0.1:5432/equatorial`
- Required extensions:
  - `postgis`
  - `pgrouting`
- Country road table pattern:
  - `public.road_surface_<iso_lower>`
  - Example: `public.road_surface_bra`
- ERA5 raw files are local NetCDF files under:
  - `equatorial/data/raw/era5/`
- Country generated configs are under:
  - `equatorial/config/generated/full_year_2024_era5_tp_remaining_20260517_203158/`
- Crop rasters are under:
  - `equatorial/spam_tifs/`
- Cities source:
  - `equatorial/data/raw/cities/global/cities500.zip`

## 0. Install pgRouting Once

For Homebrew PostgreSQL:

```bash
brew install pgrouting
```

Then in the DB:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;
SELECT pgr_version();
```

Expected for this run: `4.0.1`.

## 1. Load Road Surface To PostGIS

If the country road table is missing, use the existing loader:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial
.venv/bin/python scripts/load_road_surface_to_postgis.py \
  --dsn postgresql+psycopg://gk@127.0.0.1:5432/equatorial \
  --iso-list BRA \
  --if-exists replace
```

Verify:

```sql
SELECT count(*) FROM public.road_surface_bra;
SELECT f_table_schema, f_table_name, type, srid
FROM geometry_columns
WHERE f_table_name = 'road_surface_bra';
```

For Brazil we had `8,917,868` road rows.

## 2. Load Weekly ERA5 Precipitation Grid

We did not load hourly ERA5 into Postgres. That would be too large. We loaded weekly ERA5 cell aggregates:

Table:

```sql
eq.era5_precip_weekly_grid
```

Columns:

- `country_code`
- `week_start`
- `cell_id`
- `cell_lon`
- `cell_lat`
- `tp_sum_weekly_mm`
- `tp_mean_hourly_mm`
- `tp_median_hourly_mm`
- `tp_1h_max_mm_per_h`
- generated `geometry`

For Brazil the load produced:

- `10,234,830` rows
- `53` weeks
- `193,110` ERA5 cells per week
- weeks `2024-01-01` through `2024-12-30`

Important implementation details:

- Read only `tp` from each monthly NetCDF.
- Convert ERA5 accumulated precipitation from meters to hourly increments in mm.
- Weekly metrics:
  - sum of hourly increments
  - mean hourly increment
  - median hourly increment
  - max hourly increment
- If a cell is all-NaN for a week, set all metrics to `NULL`, including weekly sum. Do not let `np.nansum()` turn missing data into `0`.

Suggested table DDL:

```sql
CREATE TABLE IF NOT EXISTS eq.era5_precip_weekly_grid (
    country_code text NOT NULL,
    week_start date NOT NULL,
    cell_id text NOT NULL,
    cell_lon double precision NOT NULL,
    cell_lat double precision NOT NULL,
    tp_sum_weekly_mm double precision,
    tp_mean_hourly_mm double precision,
    tp_median_hourly_mm double precision,
    tp_1h_max_mm_per_h double precision,
    geometry geometry(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cell_lon, cell_lat), 4326)) STORED,
    PRIMARY KEY (country_code, week_start, cell_id)
);
CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_country_week_idx
    ON eq.era5_precip_weekly_grid (country_code, week_start);
CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_cell_idx
    ON eq.era5_precip_weekly_grid (country_code, cell_id);
CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_geometry_gist
    ON eq.era5_precip_weekly_grid USING GIST (geometry);
```

Verify:

```sql
SELECT
  count(*) AS rows,
  count(DISTINCT week_start) AS weeks,
  count(DISTINCT cell_id) AS cells,
  min(week_start),
  max(week_start),
  count(*) FILTER (WHERE tp_sum_weekly_mm IS NULL) AS null_sum,
  max(tp_sum_weekly_mm) AS max_sum_mm,
  max(tp_1h_max_mm_per_h) AS max_1h_mm
FROM eq.era5_precip_weekly_grid
WHERE country_code = 'BRA';
```

## 3. Map Roads To ERA5 Cells

Table:

```sql
eq.road_era5_cell_map
```

Purpose: one row per road, assigning `road_row_id` to nearest/snapped ERA5 cell.

For Brazil:

- `8,917,868` rows
- all road rows mapped
- `46,654` ERA5 cells used by roads

Probe point rule used:

- For `LineString`: midpoint.
- For `MultiLineString`: midpoint of longest part.
- This matches the project helper `geometry_probe_point()`.

Suggested table DDL:

```sql
CREATE TABLE IF NOT EXISTS eq.road_era5_cell_map (
    country_code text NOT NULL,
    road_row_id bigint NOT NULL,
    cell_id text NOT NULL,
    cell_lon double precision NOT NULL,
    cell_lat double precision NOT NULL,
    road_probe geometry(Point, 4326),
    cell_geometry geometry(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cell_lon, cell_lat), 4326)) STORED,
    PRIMARY KEY (country_code, road_row_id)
);
CREATE INDEX IF NOT EXISTS road_era5_cell_map_country_cell_idx
    ON eq.road_era5_cell_map (country_code, cell_id);
CREATE INDEX IF NOT EXISTS road_era5_cell_map_probe_gist
    ON eq.road_era5_cell_map USING GIST (road_probe);
```

Verify:

```sql
SELECT count(*) FROM public.road_surface_bra;
SELECT count(*), count(DISTINCT cell_id)
FROM eq.road_era5_cell_map
WHERE country_code = 'BRA';
```

## 4. Build Cell-Level Overlay And Boxplots

Avoid materializing road-week factors for large countries. For Brazil this would be huge. Instead, materialize cell-week overlay.

Tables:

```sql
eq.road_era5_cell_surface_summary
eq.era5_precip_cell_overlay
eq.boxplot_stats_weekly
```

Surface scenarios:

- `actual_unpaved`
- `unknown_as_paved`
- `unknown_as_unpaved`

Surface scopes:

- `all`
- `paved`
- `unpaved`

ERA5 factors:

- `era5_tp_sum_weekly_mm`
- `era5_tp_mean_hourly_mm`
- `era5_tp_median_hourly_mm`
- `era5_tp_1h_max_weekly_mm_per_h`

For Brazil:

- `eq.era5_precip_cell_overlay`: `18,264,701` rows
- `eq.boxplot_stats_weekly_bra`: `1,908` rows
- Formula: `53 weeks x 3 scenarios x 3 scopes x 4 factors`, excluding missing scope/factor combinations as applicable.

Boxplot PNG output created here:

```text
equatorial/outputs/road_weekly_scenarios/BRA/2024_full_year_db_cell_overlay_bra/factor_boxplots_cell/weekly_factor_value_boxplots/
```

Important correction: do not create weekly map PNGs unless explicitly requested. We created and then deleted:

```text
equatorial/outputs/road_weekly_scenarios/BRA/2024_full_year_db_cell_overlay_bra/weekly_cell_maps/
```

## 5. Load City Destinations

Table:

```sql
eq.city_destinations
```

Source:

```text
equatorial/data/raw/cities/global/cities500.zip
```

Rules used:

- GeoNames country code `BR`
- `population >= 50000`

For Brazil:

- `760` cities
- largest cities included São Paulo, Rio de Janeiro, Belo Horizonte, Salvador, Fortaleza, Manaus, Brasília.

DDL:

```sql
CREATE TABLE IF NOT EXISTS eq.city_destinations (
    country_code text NOT NULL,
    geoname_id bigint NOT NULL,
    name text NOT NULL,
    ascii_name text,
    feature_class text,
    feature_code text,
    admin1_code text,
    population bigint,
    lon double precision NOT NULL,
    lat double precision NOT NULL,
    geometry geometry(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
    PRIMARY KEY (country_code, geoname_id)
);
```

## 6. Load Crop Origin Candidates

Table:

```sql
eq.crop_origin_candidates
```

Source:

```text
equatorial/spam_tifs/spam2010V2r0_global_H_*_A.tif
```

Rules used:

- Clip/mask raster to GADM country boundary.
- Keep positive harvested area cells.
- Select top `100` cells per crop by harvested area.

For Brazil:

- `10` crops
- `1000` candidates total
- crops: `bean`, `cott`, `maiz`, `pota`, `rice`, `sorg`, `soyb`, `sugc`, `sunf`, `whea`

DDL:

```sql
CREATE TABLE IF NOT EXISTS eq.crop_origin_candidates (
    country_code text NOT NULL,
    crop_code text NOT NULL,
    candidate_rank integer NOT NULL,
    harvested_area double precision NOT NULL,
    lon double precision NOT NULL,
    lat double precision NOT NULL,
    source_file text NOT NULL,
    geometry geometry(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
    PRIMARY KEY (country_code, crop_code, candidate_rank)
);
```

## 7. Build pgRouting Road Graph

Tables:

```sql
eq.road_graph_edges_bra
eq.road_graph_nodes_bra
eq.road_graph_edges_pgr_bra
eq.road_graph_components_bra
eq.road_graph_edges_pgr_bra_c5
eq.road_graph_edges_pgr_bra_c5_cell
```

Important caveat: the first graph was endpoint-only. For Brazil it produced many disconnected components:

- `3,433,068` graph components
- largest component: `952,670` nodes

This is analytically important. We selected only crop origins connected to a city component. For Brazil, selected origins all landed in component `5`.

Base edge rules:

- Exclude non-truck highway classes:
  - `footway`
  - `path`
  - `steps`
  - `pedestrian`
  - `cycleway`
  - `bridleway`
  - `living_street`
- Split `MultiLineString` with `ST_Dump`.
- Node key is rounded endpoint coordinate hash.
- Length from `ST_Length(geometry::geography) / 1000.0`.
- Baseline speeds:
  - motorway/link: `90`
  - trunk/link: `80`
  - primary/link: `70`
  - secondary/link: `60`
  - tertiary/link: `50`
  - unclassified/residential: `35`
  - service: `25`
  - track: `20`
  - other: `30`
- Surface multiplier:
  - paved: `1.0`
  - unpaved: `0.75`
  - unknown: `0.85`

For Brazil:

- `eq.road_graph_edges_bra`: `8,299,158` edge-part rows
- `eq.road_graph_nodes_bra`: `9,812,771` nodes
- `eq.road_graph_edges_pgr_bra_c5`: `1,550,980` component-5 edges
- `eq.road_graph_edges_pgr_bra_c5_cell`: same edges with `cell_id` prejoined for weekly penalties

## 8. Snap Origins And Cities To Graph Nodes

Tables:

```sql
eq.crop_origin_nodes_bra
eq.city_destination_nodes_bra
eq.city_destination_components_bra
```

Use nearest graph node by PostGIS KNN:

```sql
ORDER BY n.geometry <-> origin.geometry
LIMIT 1
```

For Brazil:

- crop origins snapped: `1000`
- cities snapped: `760`
- origin snap distance:
  - median about `644 m`
  - p95 about `2.9 km`
  - max about `15 km`
- city snap distance:
  - median about `41 m`
  - p95 about `134 m`
  - max about `1.3 km`

## 9. Select Baseline-Connected Crop Origins

Table:

```sql
eq.crop_origin_selected_bra
```

Rule:

- Join origin node to `eq.road_graph_components_bra`.
- Keep only origins whose component contains at least one selected city.
- Select top `3` per crop by harvested area among connected candidates.

For Brazil:

- selected origins: `26`, not `30`
- selected crops: `9`
- missing crop: `cott`
- all selected origins were in component `5`

This lower-than-expected count is not a failure. It reflects graph connectivity under the endpoint-only topology.

## 10. Baseline Nearest-City Accessibility

Table:

```sql
eq.crop_accessibility_baseline_bra
```

Use `pgr_dijkstraCost` from selected origins to all city nodes in the same component. Store only nearest city per origin.

For Brazil:

- `26` baseline rows
- min travel time about `37 min`
- median about `126 min`
- max about `889 min`

Use component-specific edges for speed:

```sql
SELECT *
FROM pgr_dijkstraCost(
  'SELECT id, source, target, cost, reverse_cost FROM eq.road_graph_edges_pgr_bra_c5',
  ARRAY(SELECT DISTINCT node_id FROM eq.crop_origin_selected_bra WHERE component = 5),
  ARRAY(SELECT DISTINCT node_id FROM eq.city_destination_components_bra WHERE component = 5),
  false
);
```

## 11. Weekly Rain Penalty Function

Function:

```sql
eq.rain_speed_penalty_fraction(surface_group, tp_1h_max_mm_per_h, tp_1h_percentile)
```

Rules implemented from `road_hazard_thresholds_exact_mar_may.yaml`:

Paved operational rainfall:

- `< 6.35 mm/h`: `0`
- `6.35..20`: interpolate `0.07..0.20`
- `20..30`: interpolate `0.20..0.35`
- `>=30`: `0.35`

Unpaved erosion proxy:

- use weekly local percentile of `tp_1h_max_mm_per_h`
- `<75`: `0`
- `75..90`: interpolate `0.05..0.15`
- `90..99`: interpolate `0.15..0.35`
- `>=99`: `0.35`

Unknown handling is scenario-dependent:

- `actual_unpaved`: unknown stays unknown, no rainfall speed penalty
- `unknown_as_paved`: unknown treated as paved
- `unknown_as_unpaved`: unknown treated as unpaved

Percentile table:

```sql
eq.era5_precip_weekly_percentile_bra
```

For Brazil:

- `2,446,904` rows

## 12. Weekly Crop Accessibility

Table:

```sql
eq.crop_accessibility_weekly_bra
```

Target complete size for Brazil:

```text
53 weeks x 3 scenarios x 26 origins = 4,134 rows
```

Partial state when stopped:

- `468` rows
- `6` weeks
- `3` scenarios
- weeks `2024-01-01` through `2024-02-05`

Before a full rerun:

```sql
DELETE FROM eq.crop_accessibility_weekly_bra
WHERE country_code = 'BRA';
```

Then rerun `pgr_dijkstraCost` week by week and scenario by scenario.

Performance observed:

- initial query with join to `road_era5_cell_map`: about `13 s` per week/scenario
- optimized query using `eq.road_graph_edges_pgr_bra_c5_cell`: about `10-12 s` per week/scenario
- full Brazil run estimate: roughly `25-35 min`

The main cost is `pgr_dijkstraCost` over `1.55M` component edges, not the SQL join.

## 13. Verification Checklist

After each country, inspect real outputs:

```sql
SELECT count(*) FROM public.road_surface_bra;
SELECT count(*), count(DISTINCT week_start), count(DISTINCT cell_id)
FROM eq.era5_precip_weekly_grid
WHERE country_code = 'BRA';
SELECT count(*), count(DISTINCT cell_id)
FROM eq.road_era5_cell_map
WHERE country_code = 'BRA';
SELECT count(*), count(DISTINCT week_start), count(DISTINCT scenario)
FROM eq.crop_accessibility_weekly_bra
WHERE country_code = 'BRA';
```

Check graph fragmentation:

```sql
SELECT count(DISTINCT component) AS graph_components,
       max(component_size) AS largest_component_nodes
FROM (
  SELECT component, count(*) AS component_size
  FROM eq.road_graph_components_bra
  GROUP BY component
) x;
```

Check selected crop origins:

```sql
SELECT crop_code, count(*) AS selected
FROM eq.crop_origin_selected_bra
GROUP BY crop_code
ORDER BY crop_code;
```

Check weekly accessibility sanity:

```sql
SELECT count(*) AS rows,
       count(DISTINCT week_start) AS weeks,
       count(DISTINCT scenario) AS scenarios,
       min(delay_min) AS min_delay,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY delay_min) AS median_delay,
       max(delay_min) AS max_delay
FROM eq.crop_accessibility_weekly_bra
WHERE country_code = 'BRA';
```

## 14. Naming For Next Countries

Use generic shared tables where reasonable:

- `eq.era5_precip_weekly_grid`
- `eq.road_era5_cell_map`
- `eq.era5_precip_cell_overlay`
- `eq.city_destinations`
- `eq.crop_origin_candidates`

Use country-specific graph/accessibility tables unless refactored into partitioned parents:

- `eq.road_graph_edges_<iso_lower>`
- `eq.road_graph_nodes_<iso_lower>`
- `eq.road_graph_edges_pgr_<iso_lower>`
- `eq.road_graph_components_<iso_lower>`
- `eq.crop_origin_selected_<iso_lower>`
- `eq.crop_accessibility_baseline_<iso_lower>`
- `eq.crop_accessibility_weekly_<iso_lower>`

Reason: graph tables are large, expensive, and easier to rebuild/drop per country.

## 15. Current Yearly A* Weekly Accessibility Run

Active runner:

```bash
.venv/bin/python scripts/run_weekly_astar_accessibility.py \
  --countries auto \
  --top-per-crop 5 \
  --heartbeat-s 60 \
  --scenario weekly_sum_penalty_v1
```

Output table:

```text
eq.crop_accessibility_weekly_astar
```

Scenario semantics for `weekly_sum_penalty_v1`:

- ERA5 input: `eq.era5_precip_weekly_grid.tp_sum_weekly_mm`.
- Penalty lookup table: `eq.weekly_rain_speed_penalty_rules`.
- Origin scope: `top5_per_crop`, meaning top 5 crop origin nodes by `harvested_area` within each `crop_code`.
- Destinations: up to 10 nearest reachable 5k-100k cities plus nearest reachable port, selected inside the bridge graph component.
- Road surface handling:
  - `surface_group = 'paved'` uses paved rainfall penalties.
  - `surface_group = 'unpaved'` uses unpaved rainfall penalties.
  - `surface_group = 'unknown'`, `NULL`, and any other non-paved value are treated as unpaved.
  - `surface_group = 'synthetic_connector'` has no rainfall penalty; multiplier stays `1.0`.
- In plain terms: this run is `unknown_as_unpaved_weekly_sum_penalty_v1`, but the stored scenario name is currently `weekly_sum_penalty_v1`.

Weekly rain speed multipliers:

| road_type | weekly rain mm | multiplier | effect |
| --- | ---: | ---: | --- |
| paved | 0-50 | 1.00 | no penalty |
| paved | 50-100 | 0.90 | minor speed reduction |
| paved | 100-200 | 0.75 | slowed |
| paved | 200-300 | 0.40 | flood/damage risk |
| paved | >=300 | 0.05 | effectively closed |
| unpaved | 0-50 | 1.00 | no penalty |
| unpaved | 50-100 | 0.70 | slowed |
| unpaved | 100-150 | 0.45 | strongly slowed |
| unpaved | 150-250 | 0.20 | severe degradation / low reliability |
| unpaved | >=250 | 0.05 | effectively closed |

Rendering current accessibility impact heatmaps:

```bash
.venv/bin/python scripts/render_weekly_astar_accessibility_heatmaps.py \
  --metric delta_minutes \
  --cap-minutes 240 \
  --min-weeks 1
```

Rendered PNGs:

```text
outputs/astar_accessibility_weekly/weekly_sum_penalty_v1_top5_per_crop_delta_minutes_heatmaps
```

Heatmap metric:

- `delta_minutes = (weekly travel_time_h - baseline travel_time_h) * 60`.
- Baseline is the best available week for the same origin-destination pair within the current yearly run.
- This is a travel-time deviation, not a path-length deviation.
- Path-length deviation would require storing full `pgr_aStar` paths and summing edge `length_km`; the current run uses `pgr_aStarCost`, which returns only route cost.

## 16. Known Caveats

- The active crop/accessibility scripts referenced in `README.md` are currently absent from `src/data`; this workflow was built DB-first instead of using those deleted scripts.
- Endpoint-only topology fragments Brazil heavily. This is the biggest analytical risk. For production, consider a DB-native topology improvement step before final accessibility claims.
- We used top-100 SPAM harvested-area cells per crop, then top-3 connected origins per crop. Some crops may have fewer than 3 connected origins.
- Threshold implementation currently covers ERA5 rainfall rules only:
  - paved operational rainfall
  - unpaved erosion percentile proxy
- No flood, heat, wind, visibility, or soil-moisture routing penalties were applied in the DB weekly accessibility run.
- Do not call a run correct just because SQL completed. Inspect counts, selected origins, graph components, travel-time ranges, and delay distributions.
