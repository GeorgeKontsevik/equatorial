# Cluster-Connected Crop Accessibility Pipeline

This is the current final pipeline for the equatorial crop accessibility runs.
It supersedes the old `top20` / `component_connected` artifacts.

## Scope

- Data source for crop origins: CROPGRIDS-derived `eq.crop_origin_candidates`.
- Use only CROPGRIDS-derived crop origins for this run.
- Do not run countries without crop candidates, for example `GNQ`.
- Current standard OD scope:
  - all stored crop-cluster terminals per crop, not top-N filtering;
  - 10 nearest small cities, population 5k-100k;
  - 3 nearest large cities, population 100k+;
  - 3 nearest in-country ports;
  - 3 nearest in-country airports.
- Current scenario: `weekly_sum_penalty_v1`.
- Current result scope:
  `cluster_connected_allclusters_10small_3large_3ports_3airports`.

## Key Modeling Rule

Each crop-cluster point is a graph node and starts as its own component.
It is not filtered out by distance to roads and is not snapped directly to the
nearest road as a special one-off rule.

The `cluster_connected` graph is built by taking the raw `road_graph` components
plus crop terminal components and connecting nearest components iteratively.
Synthetic connector links are marked as:

```text
surface_group = unpaved_synthetic_line
```

For precipitation penalty handling, all unpaved-like classes, including
`unpaved_synthetic_line`, are treated as unpaved.

## Build Order

Run countries in ascending road graph size when doing a batch. This keeps early
failures cheap and surfaces scaling problems before large countries.

Exclude from the normal batch:

- `GNQ`: no crop candidates in the current crop table.
- `BRA`, `IDN`: very large graphs with millions of components; they need a
  scalable component-connection strategy before using this same final scope.

## Commands

Use the project venv:

```bash
cd /Users/gk/Code/super-duper-disser/equatorial/scripts
../.venv/bin/python build_cluster_connected_graphs.py --countries ISO
../.venv/bin/python run_weekly_astar_accessibility.py \
  --countries ISO \
  --graph-prefix cluster_connected \
  --top-per-crop 0 \
  --small-city-limit 10 \
  --port-limit 3 \
  --large-city-limit 3 \
  --airport-limit 3 \
  --force-snap \
  --force-od \
  --replace \
  --heartbeat-s 300
```

Render country-level aggregated plots:

```bash
../.venv/bin/python render_weekly_astar_accessibility.py \
  --origin-scope cluster_connected_allclusters_10small_3large_3ports_3airports \
  --countries loaded \
  --min-weeks 53 \
  --split-crops \
  --out-dir ../outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_plots
```

Render heatmaps:

```bash
../.venv/bin/python render_weekly_astar_accessibility_heatmaps.py \
  --origin-scope cluster_connected_allclusters_10small_3large_3ports_3airports \
  --countries loaded \
  --min-weeks 53 \
  --metric delta_minutes \
  --agg median \
  --out-dir ../outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps
```

## Output Directories

Valid current output directories:

```text
equatorial/outputs/astar_accessibility_weekly/cluster_connected_graphs
equatorial/outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_plots
equatorial/outputs/astar_accessibility_weekly/cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps
```

Country-level plots are aggregated across all crops. Crop-specific plots are
under `by_crop/<ISO>/`.

## Verification Queries

Check graph connectivity:

```sql
SELECT count(DISTINCT component)
FROM eq.cluster_connected_components_<iso>;
```

Expected for a completed normal country: `1`.

Check yearly run completeness:

```sql
SELECT country_code,
       count(DISTINCT week_start) AS weeks,
       count(*) AS rows,
       count(*) FILTER (WHERE route_status <> 'ok') AS not_ok
FROM eq.crop_accessibility_weekly_astar
WHERE origin_scope = 'cluster_connected_allclusters_10small_3large_3ports_3airports'
GROUP BY country_code
ORDER BY country_code;
```

Expected for a completed country: `weeks = 53`, `not_ok = 0`.

## Current Completed Countries

Current manifests and route diagnostics contain complete final-scope results
for these 29 countries:

```text
AGO BDI BEN BRN CAF CIV CMR COD COG COL ECU ETH GAB GUY KEN LBR LKA MYS
NGA PER PNG RWA SOM SSD SUR TGO TZA UGA VEN
```

`GNQ` has no crop candidates. `BRA` and `IDN` remain outside the normal batch
because the current component-connection method does not scale to their graph
sizes. Verify live DB coverage before rerunning or extending the country set;
do not use the presence of a PNG alone as completion evidence.
