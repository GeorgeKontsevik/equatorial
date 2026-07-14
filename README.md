# equatorial

Research pipeline for measuring weekly climate-related changes in crop
accessibility across countries within roughly 700 km of the equator.

## Current result

The current dissertation experiment uses:

- full-year 2024 data with 53 weekly states;
- CROPGRIDS crop clusters for avocado, banana, mango, pineapple, and plantain;
- road-surface networks stored in PostgreSQL/PostGIS;
- ERA5 weekly precipitation and surface-dependent speed penalties;
- `cluster_connected` graphs;
- A* routes from every stored crop cluster to 10 small cities, 3 large cities,
  3 ports, and 3 airports;
- result scope
  `cluster_connected_allclusters_10small_3large_3ports_3airports`;
- scenario `weekly_sum_penalty_v1`.

The exact build and verification commands are in
[CLUSTER_CONNECTED_ACCESSIBILITY_PIPELINE.md](CLUSTER_CONNECTED_ACCESSIBILITY_PIPELINE.md).
For a short handoff, read [NEXT_AGENT_NOTES.md](NEXT_AGENT_NOTES.md).

## Repository map

| Path | Purpose |
| --- | --- |
| `src/data/` | Config-driven raw-data fetch and multisource road-overlay library |
| `scripts/` | Current service, graph, accessibility, analysis, and figure entry points |
| `sql/` | SQL supporting current DB inputs and road-cell statistics |
| `config/` | Active dataset and road-hazard data contracts |
| `data/` | Local raw data and metadata; do not clean automatically |
| `outputs/` | Current experiment outputs and their manifests |
| `tests/` | Tests for the active fetch/overlay layer |
| `supporting_materials/` | Relevant cross-project schemes and their provenance |
| `old/` | Superseded material; do not inspect unless explicitly requested |
| `artifacts/` | Archived documents, tables, and images; do not inspect unless requested |

See [scripts/README.md](scripts/README.md), [config/README.md](config/README.md),
and [outputs/README.md](outputs/README.md) for directory-level maps.

## Environment

The established local environment is `.venv` and the database used by the
workflow is normally:

```text
postgresql://gk@127.0.0.1:5432/equatorial
```

PostgreSQL requires PostGIS and pgRouting. Install the Python project with:

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

Do not delete or recreate an existing environment, database, raw-data tree, or
generated output as cleanup.

## Main workflow

Fetch/generate full-year country configs:

```bash
bash scripts/fetch_equator_700km_full_year_data.sh
```

Build a cluster-connected country graph:

```bash
.venv/bin/python scripts/build_cluster_connected_graphs.py --countries LBR
```

Run the final accessibility scope:

```bash
.venv/bin/python scripts/run_weekly_astar_accessibility.py \
  --countries LBR \
  --graph-prefix cluster_connected \
  --top-per-crop 0 \
  --small-city-limit 10 \
  --large-city-limit 3 \
  --port-limit 3 \
  --airport-limit 3 \
  --force-snap \
  --force-od \
  --replace
```

Render final heatmaps:

```bash
.venv/bin/python scripts/render_weekly_astar_accessibility_heatmaps.py \
  --countries LBR \
  --min-weeks 53 \
  --metric delta_minutes \
  --agg median
```

## Verification

Run the checks available in the existing environment:

```bash
make check
```

A successful command is not sufficient evidence. Inspect the relevant manifest,
row counts, 53-week coverage, route statuses, and final PNGs. The known test
status is recorded in [BACKLOG.md](BACKLOG.md).

## Scope constraints

- Do not use historical crop-origin or top-N result scopes for the current result.
- `GNQ` has no current crop candidates.
- `BRA` and `IDN` are excluded from the normal cluster-connection batch because
  the current connection strategy does not scale to their component counts.
- Synthetic graph links are connectivity devices, not observed roads.
- Weekly precipitation penalties are a scenario model, not a calibrated physical
  road-damage model.

## Citation

Kontsevik, G. (2026). *equatorial* [Computer software].
https://github.com/GeorgeKontsevik/equatorial
