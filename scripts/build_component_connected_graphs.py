#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from run_weekly_astar_accessibility import DEFAULT_DB_URL, qident, qliteral, scalar, table_exists


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "component_connected_graphs"
CONNECTOR_SPEED_KMH = 15.0
SURFACE_GROUP = "unpaved_synthetic_line"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_sql_gdf(conn, sql: str) -> gpd.GeoDataFrame:
    frame = gpd.read_postgis(sql, conn, geom_col="geometry")
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame.to_crs("EPSG:4326")


def metric_crs(bounds) -> str:
    minx, miny, maxx, maxy = bounds
    lon0 = (minx + maxx) / 2.0
    lat0 = (miny + maxy) / 2.0
    return f"+proj=aeqd +lat_0={lat0:.8f} +lon_0={lon0:.8f} +datum=WGS84 +units=m +no_defs"


def country_queue(conn: psycopg.Connection, requested: str) -> list[str]:
    if requested.lower() != "auto":
        return [x.strip().upper() for x in requested.split(",") if x.strip()]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH edges AS (
                SELECT upper(substring(relname from '^road_graph_edges_([a-z]{3})$')) iso
                FROM pg_class
                WHERE relnamespace = 'eq'::regnamespace
                  AND relname ~ '^road_graph_edges_[a-z]{3}$'
            ), crops AS (
                SELECT country_code iso, count(*) n
                FROM eq.crop_origin_candidates
                GROUP BY country_code
            )
            SELECT e.iso
            FROM edges e
            JOIN crops c USING (iso)
            WHERE c.n > 0
            ORDER BY e.iso
            """
        )
        return [row[0] for row in cur.fetchall()]


def create_pgr_components_astar(conn: psycopg.Connection, iso: str, prefix: str) -> None:
    suffix = iso.lower()
    nodes = f"{prefix}_nodes_{suffix}"
    edges = f"{prefix}_edges_{suffix}"
    pgr = f"{prefix}_edges_pgr_{suffix}"
    comps = f"{prefix}_components_{suffix}"
    astar = f"{prefix}_edges_pgr_{suffix}_astar_base"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DROP TABLE IF EXISTS eq.{qident(pgr)} CASCADE;
            CREATE UNLOGGED TABLE eq.{qident(pgr)} AS
            SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
                   e.road_row_id, e.part_id, e.highway, e.surface_group,
                   e.base_speed_kmh::double precision AS base_speed_kmh,
                   e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
            FROM eq.{qident(edges)} e
            JOIN eq.{qident(nodes)} ns ON ns.node_key = e.source_node_id
            JOIN eq.{qident(nodes)} nt ON nt.node_key = e.target_node_id
            WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0;
            ALTER TABLE eq.{qident(pgr)} ADD PRIMARY KEY (id);
            CREATE INDEX {pgr}_source_idx ON eq.{qident(pgr)} (source);
            CREATE INDEX {pgr}_target_idx ON eq.{qident(pgr)} (target);
            ANALYZE eq.{qident(pgr)};

            DROP TABLE IF EXISTS eq.{qident(comps)} CASCADE;
            CREATE UNLOGGED TABLE eq.{qident(comps)} AS
            SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{pgr}');
            CREATE INDEX {comps}_node_idx ON eq.{qident(comps)} (node);
            CREATE INDEX {comps}_component_idx ON eq.{qident(comps)} (component);
            ANALYZE eq.{qident(comps)};

            DROP TABLE IF EXISTS eq.{qident(astar)} CASCADE;
            CREATE UNLOGGED TABLE eq.{qident(astar)} AS
            SELECT e.id, e.source, e.target, e.surface_group,
                   e.cost AS base_cost, e.reverse_cost AS base_reverse_cost,
                   m.cell_id,
                   ns.lon AS x1, ns.lat AS y1,
                   nt.lon AS x2, nt.lat AS y2
            FROM eq.{qident(pgr)} e
            JOIN eq.{qident(nodes)} ns ON ns.node_id = e.source
            JOIN eq.{qident(nodes)} nt ON nt.node_id = e.target
            LEFT JOIN eq.road_era5_cell_map m
              ON m.country_code = {qliteral(iso)}
             AND m.road_row_id = e.road_row_id;
            ALTER TABLE eq.{qident(astar)} ADD PRIMARY KEY (id);
            CREATE INDEX {astar}_source_idx ON eq.{qident(astar)} (source);
            CREATE INDEX {astar}_target_idx ON eq.{qident(astar)} (target);
            CREATE INDEX {astar}_cell_idx ON eq.{qident(astar)} (cell_id);
            ANALYZE eq.{qident(astar)};
            """
        )
    conn.commit()


class DSU:
    def __init__(self, values: list[int]) -> None:
        self.parent = {v: v for v in values}
        self.size = {v: 1 for v in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def candidate_links(nodes_m: gpd.GeoDataFrame, k: int) -> pd.DataFrame:
    coords = np.column_stack([nodes_m.geometry.x.to_numpy(), nodes_m.geometry.y.to_numpy()])
    tree = cKDTree(coords)
    k_eff = min(k + 1, len(nodes_m))
    distances, indexes = tree.query(coords, k=k_eff)
    if k_eff == 1:
        distances = distances[:, None]
        indexes = indexes[:, None]
    rows = []
    best_pair: dict[tuple[int, int], dict] = {}
    comps = nodes_m.component.to_numpy()
    node_ids = nodes_m.node_id.to_numpy()
    lons = nodes_m.lon.to_numpy()
    lats = nodes_m.lat.to_numpy()
    keys = nodes_m.node_key.to_numpy()
    for i in range(len(nodes_m)):
        comp_a = int(comps[i])
        for dist_m, j in zip(distances[i][1:], indexes[i][1:], strict=True):
            comp_b = int(comps[int(j)])
            if comp_a == comp_b:
                continue
            pair = (comp_a, comp_b) if comp_a < comp_b else (comp_b, comp_a)
            item = {
                "source_component": comp_a,
                "target_component": comp_b,
                "source_node": int(node_ids[i]),
                "target_node": int(node_ids[int(j)]),
                "source_key": str(keys[i]),
                "target_key": str(keys[int(j)]),
                "source_lon": float(lons[i]),
                "source_lat": float(lats[i]),
                "target_lon": float(lons[int(j)]),
                "target_lat": float(lats[int(j)]),
                "length_m": float(dist_m),
            }
            old = best_pair.get(pair)
            if old is None or item["length_m"] < old["length_m"]:
                best_pair[pair] = item
    rows.extend(best_pair.values())
    return pd.DataFrame(rows)


def select_spanning_links(nodes: gpd.GeoDataFrame, k_values: list[int]) -> tuple[gpd.GeoDataFrame, dict]:
    crs = metric_crs(nodes.total_bounds)
    nodes_m = nodes.to_crs(crs)
    components = sorted(int(c) for c in nodes_m.component.unique())
    if len(components) <= 1:
        empty = gpd.GeoDataFrame(columns=["length_m", "geometry"], geometry="geometry", crs="EPSG:4326")
        return empty, {"initial_components": len(components), "final_components": len(components), "candidate_rows": 0, "k_used": 0}

    selected_rows = []
    all_candidates = pd.DataFrame()
    k_used = 0
    dsu = DSU(components)
    for k in k_values:
        k_used = k
        log(f"candidate nearest-node links k={k}")
        candidates = candidate_links(nodes_m, k)
        if candidates.empty:
            continue
        all_candidates = (
            pd.concat([all_candidates, candidates], ignore_index=True)
            .sort_values("length_m")
            .drop_duplicates(["source_component", "target_component"], keep="first")
        )
        dsu = DSU(components)
        selected_rows = []
        for row in all_candidates.sort_values("length_m").itertuples(index=False):
            if dsu.union(int(row.source_component), int(row.target_component)):
                selected_rows.append(row._asdict())
                if len(selected_rows) == len(components) - 1:
                    break
        final_roots = {dsu.find(c) for c in components}
        if len(final_roots) == 1:
            break

    # k-nearest component candidates can still leave disconnected islands when a
    # remote component's nearest nodes are all inside its own component. Finish
    # with exact nearest links between the remaining DSU super-components.
    while len({dsu.find(c) for c in components}) > 1:
        roots = sorted({dsu.find(c) for c in components})
        connected_root = roots[0]
        connected_components = {c for c in components if dsu.find(c) == connected_root}
        remaining_roots = roots[1:]
        connected_nodes = nodes_m[nodes_m.component.isin(connected_components)]
        tree = cKDTree(np.column_stack([connected_nodes.geometry.x.to_numpy(), connected_nodes.geometry.y.to_numpy()]))
        best = None
        for root in remaining_roots:
            root_components = {c for c in components if dsu.find(c) == root}
            root_nodes = nodes_m[nodes_m.component.isin(root_components)]
            distances, indexes = tree.query(
                np.column_stack([root_nodes.geometry.x.to_numpy(), root_nodes.geometry.y.to_numpy()]),
                k=1,
            )
            pos = int(np.argmin(distances))
            src = connected_nodes.iloc[int(indexes[pos])]
            dst = root_nodes.iloc[pos]
            item = {
                "source_component": int(src.component),
                "target_component": int(dst.component),
                "source_node": int(src.node_id),
                "target_node": int(dst.node_id),
                "source_key": str(src.node_key),
                "target_key": str(dst.node_key),
                "source_lon": float(src.lon),
                "source_lat": float(src.lat),
                "target_lon": float(dst.lon),
                "target_lat": float(dst.lat),
                "length_m": float(distances[pos]),
            }
            if best is None or item["length_m"] < best["length_m"]:
                best = item
        if best is None:
            break
        selected_rows.append(best)
        dsu.union(int(best["source_component"]), int(best["target_component"]))

    links = pd.DataFrame(selected_rows)
    if links.empty:
        links_gdf = gpd.GeoDataFrame(columns=["length_m", "geometry"], geometry="geometry", crs="EPSG:4326")
    else:
        links["geometry"] = [
            LineString([(r.source_lon, r.source_lat), (r.target_lon, r.target_lat)])
            for r in links.itertuples(index=False)
        ]
        links_gdf = gpd.GeoDataFrame(links, geometry="geometry", crs="EPSG:4326")
    final_components = len({dsu.find(c) for c in components})
    stats = {
        "initial_components": int(len(components)),
        "final_components": int(final_components),
        "candidate_rows": int(len(all_candidates)),
        "k_used": int(k_used),
    }
    return links_gdf, stats


def copy_base_graph(conn: psycopg.Connection, iso: str, prefix: str) -> None:
    suffix = iso.lower()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DROP TABLE IF EXISTS eq.{qident(f'{prefix}_edges_{suffix}')} CASCADE;
            CREATE TABLE eq.{qident(f'{prefix}_edges_{suffix}')} AS TABLE eq.{qident(f'road_graph_edges_{suffix}')};
            ALTER TABLE eq.{qident(f'{prefix}_edges_{suffix}')} ADD PRIMARY KEY (edge_id);
            CREATE INDEX {prefix}_edges_{suffix}_geom_idx ON eq.{qident(f'{prefix}_edges_{suffix}')} USING GIST (geometry);

            DROP TABLE IF EXISTS eq.{qident(f'{prefix}_nodes_{suffix}')} CASCADE;
            CREATE TABLE eq.{qident(f'{prefix}_nodes_{suffix}')} AS TABLE eq.{qident(f'road_graph_nodes_{suffix}')};
            ALTER TABLE eq.{qident(f'{prefix}_nodes_{suffix}')} ADD PRIMARY KEY (node_id);
            CREATE UNIQUE INDEX {prefix}_nodes_{suffix}_key_idx ON eq.{qident(f'{prefix}_nodes_{suffix}')} (node_key);
            CREATE INDEX {prefix}_nodes_{suffix}_geom_idx ON eq.{qident(f'{prefix}_nodes_{suffix}')} USING GIST (geometry);
            ANALYZE eq.{qident(f'{prefix}_edges_{suffix}')};
            ANALYZE eq.{qident(f'{prefix}_nodes_{suffix}')};
            """
        )
    conn.commit()


def append_links(conn: psycopg.Connection, iso: str, prefix: str, links: gpd.GeoDataFrame) -> None:
    if links.empty:
        return
    suffix = iso.lower()
    edges = f"{prefix}_edges_{suffix}"
    max_edge_id = int(scalar(conn, f"SELECT max(edge_id) FROM eq.{qident(edges)}") or 0)
    with conn.cursor() as cur:
        for i, row in enumerate(links.itertuples(index=False), start=1):
            edge_id = max_edge_id + i
            length_km = float(row.length_m) / 1000.0
            x1, y1 = row.geometry.coords[0]
            x2, y2 = row.geometry.coords[-1]
            cur.execute(
                f"""
                INSERT INTO eq.{qident(edges)} (
                    edge_id, country_code, road_row_id, part_id, source_node_id, target_node_id,
                    source_lon, source_lat, target_lon, target_lat,
                    highway, surface_group, base_speed_kmh, length_km, base_time_h, geometry
                )
                VALUES (
                    %s, %s, NULL, %s, %s, %s,
                    %s, %s, %s, %s,
                    'component_connector', %s, %s, %s, %s,
                    ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)), 4326)
                )
                """,
                (
                    edge_id,
                    iso,
                    edge_id,
                    row.source_key,
                    row.target_key,
                    x1,
                    y1,
                    x2,
                    y2,
                    SURFACE_GROUP,
                    CONNECTOR_SPEED_KMH,
                    length_km,
                    length_km / CONNECTOR_SPEED_KMH,
                    x1,
                    y1,
                    x2,
                    y2,
                ),
            )
    conn.commit()


def component_summary(conn: psycopg.Connection, iso: str, prefix: str) -> dict:
    suffix = iso.lower()
    comps = f"{prefix}_components_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH comp AS (
                SELECT component, count(*)::bigint nodes
                FROM eq.{qident(comps)}
                GROUP BY component
            )
            SELECT sum(nodes)::bigint, count(*)::bigint, max(nodes)::bigint,
                   max(nodes)::float / sum(nodes)::float
            FROM comp
            """
        )
        nodes, components, largest, share = cur.fetchone()
    return {"nodes": nodes, "components": components, "largest_nodes": largest, "largest_share": share}


def draw_map(conn: psycopg.Connection, iso: str, prefix: str, links: gpd.GeoDataFrame) -> Path:
    suffix = iso.lower()
    boundary = gpd.read_file(ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg", layer="ADM_ADM_0").to_crs("EPSG:4326")
    edges = read_sql_gdf(conn, f"SELECT surface_group, geometry FROM eq.{qident(f'{prefix}_edges_{suffix}')}")
    fig, ax = plt.subplots(figsize=(12, 15))
    boundary.boundary.plot(ax=ax, color="#222222", linewidth=0.9)
    for surface, color, width, alpha in [
        ("paved", "#009e73", 0.45, 0.78),
        ("unpaved", "#d55e00", 0.48, 0.76),
        ("unknown", "#8c8c8c", 0.32, 0.42),
        (SURFACE_GROUP, "#54278f", 1.4, 0.92),
    ]:
        sub = edges[edges.surface_group.eq(surface)]
        if not sub.empty:
            sub.plot(ax=ax, color=color, linewidth=width, alpha=alpha, label=surface)
    if not links.empty:
        short = links[links.length_m <= 2500]
        mid = links[(links.length_m > 2500) & (links.length_m <= 10000)]
        long = links[links.length_m > 10000]
        if not short.empty:
            short.plot(ax=ax, color="#31a354", linewidth=1.3, alpha=0.95, label="new <=2.5km")
        if not mid.empty:
            mid.plot(ax=ax, color="#ff7f00", linewidth=1.3, alpha=0.95, label="new 2.5-10km")
        if not long.empty:
            long.plot(ax=ax, color="#de2d26", linewidth=1.6, alpha=0.95, label="new >10km")
    minx, miny, maxx, maxy = boundary.total_bounds
    ax.set_xlim(minx - 0.25, maxx + 0.25)
    ax.set_ylim(miny - 0.25, maxy + 0.25)
    ax.grid(True, color="#dddddd", linewidth=0.4, alpha=0.55)
    ax.set_title(f"{iso} whole-road-graph component connection")
    ax.legend(loc="lower left", fontsize=7, frameon=True)
    out = OUT_DIR / iso / f"{iso}_component_connected_graph.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170)
    plt.close(fig)
    return out


def run_country(conn: psycopg.Connection, iso: str, args: argparse.Namespace) -> dict:
    suffix = iso.lower()
    prefix = "component_connected"
    missing = [t for t in [f"road_graph_edges_{suffix}", f"road_graph_nodes_{suffix}", f"road_graph_components_{suffix}"] if not table_exists(conn, "eq", t)]
    if missing:
        return {"country_code": iso, "skipped": True, "reason": f"missing {missing}"}
    log(f"{iso} copy base")
    copy_base_graph(conn, iso, prefix)
    nodes = read_sql_gdf(
        conn,
        f"""
        SELECT n.node_id, n.node_key, n.lon, n.lat, n.geometry, c.component
        FROM eq.{qident(f'road_graph_nodes_{suffix}')} n
        JOIN eq.{qident(f'road_graph_components_{suffix}')} c ON c.node = n.node_id
        """
    )
    comp_counts = nodes.groupby("component").size()
    log(f"{iso} nodes={len(nodes):,} components={len(comp_counts):,}")
    links, select_stats = select_spanning_links(nodes, [int(x) for x in args.k_values.split(",")])
    append_links(conn, iso, prefix, links)
    create_pgr_components_astar(conn, iso, prefix)
    summary = component_summary(conn, iso, prefix)
    path = draw_map(conn, iso, prefix, links)
    metrics = {
        "country_code": iso,
        **select_stats,
        "after": summary,
        "links": {
            "count": int(len(links)),
            "max_length_m": float(links.length_m.max()) if not links.empty else 0.0,
            "p95_length_m": float(links.length_m.quantile(0.95)) if not links.empty else 0.0,
            "over_2500m": int((links.length_m > 2500).sum()) if not links.empty else 0,
            "over_10000m": int((links.length_m > 10000).sum()) if not links.empty else 0,
        },
        "map": str(path),
    }
    out = OUT_DIR / iso / f"{iso}_component_connected_metrics.json"
    out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    log(
        f"{iso} connected components {select_stats['initial_components']} -> {summary['components']} "
        f"links={len(links):,} >2.5km={metrics['links']['over_2500m']:,} >10km={metrics['links']['over_10000m']:,}"
    )
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="GUY")
    parser.add_argument("--exclude", default="BRA,IDN")
    parser.add_argument("--max-countries", type=int, default=0)
    parser.add_argument("--k-values", default="1,3,5,10,20")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exclude = {x.strip().upper() for x in args.exclude.split(",") if x.strip()}
    summaries = []
    with psycopg.connect(args.db_url) as conn:
        countries = [iso for iso in country_queue(conn, args.countries) if iso not in exclude]
        if args.max_countries:
            countries = countries[: args.max_countries]
        for iso in countries:
            try:
                summaries.append(run_country(conn, iso, args))
            except Exception as exc:
                conn.rollback()
                log(f"{iso} ERROR {type(exc).__name__}: {exc}")
                summaries.append({"country_code": iso, "error": f"{type(exc).__name__}: {exc}"})
    (OUT_DIR / "component_connected_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    pd.DataFrame(summaries).to_csv(OUT_DIR / "component_connected_summary.csv", index=False)
    log(f"done countries={len(summaries)} out={OUT_DIR}")


if __name__ == "__main__":
    main()
