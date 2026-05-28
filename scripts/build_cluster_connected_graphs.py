#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import psycopg

from build_component_connected_graphs import append_links, create_pgr_components_astar, read_sql_gdf, select_spanning_links
from run_weekly_astar_accessibility import DEFAULT_DB_URL, qident, qliteral, scalar, table_exists


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "cluster_connected_graphs"
PREFIX = "cluster_connected"
BASE_PREFIX = "road_graph"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def country_queue(conn: psycopg.Connection, requested: str) -> list[str]:
    if requested.lower() != "auto":
        return [x.strip().upper() for x in requested.split(",") if x.strip()]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT upper(substring(tablename from '^road_graph_edges_([a-z]{3})$'))
            FROM pg_tables
            WHERE schemaname = 'eq'
              AND tablename ~ '^road_graph_edges_[a-z]{3}$'
            ORDER BY 1
            """
        )
        return [row[0] for row in cur.fetchall()]


def copy_base(conn: psycopg.Connection, iso: str) -> None:
    suffix = iso.lower()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DROP TABLE IF EXISTS eq.{qident(f'{PREFIX}_edges_{suffix}')} CASCADE;
            CREATE TABLE eq.{qident(f'{PREFIX}_edges_{suffix}')} AS TABLE eq.{qident(f'{BASE_PREFIX}_edges_{suffix}')};
            ALTER TABLE eq.{qident(f'{PREFIX}_edges_{suffix}')} ADD PRIMARY KEY (edge_id);
            CREATE INDEX {PREFIX}_edges_{suffix}_geom_idx ON eq.{qident(f'{PREFIX}_edges_{suffix}')} USING GIST (geometry);

            DROP TABLE IF EXISTS eq.{qident(f'{PREFIX}_nodes_{suffix}')} CASCADE;
            CREATE TABLE eq.{qident(f'{PREFIX}_nodes_{suffix}')} AS TABLE eq.{qident(f'{BASE_PREFIX}_nodes_{suffix}')};
            ALTER TABLE eq.{qident(f'{PREFIX}_nodes_{suffix}')} ADD PRIMARY KEY (node_id);
            CREATE UNIQUE INDEX {PREFIX}_nodes_{suffix}_key_idx ON eq.{qident(f'{PREFIX}_nodes_{suffix}')} (node_key);
            CREATE INDEX {PREFIX}_nodes_{suffix}_geom_idx ON eq.{qident(f'{PREFIX}_nodes_{suffix}')} USING GIST (geometry);
            ANALYZE eq.{qident(f'{PREFIX}_edges_{suffix}')};
            ANALYZE eq.{qident(f'{PREFIX}_nodes_{suffix}')};
            """
        )
    conn.commit()


def append_crop_terminals(conn: psycopg.Connection, iso: str) -> tuple[gpd.GeoDataFrame, dict[str, float | int]]:
    suffix = iso.lower()
    nodes_table = f"{PREFIX}_nodes_{suffix}"
    out_origins = f"{PREFIX}_crop_origin_nodes_{suffix}"
    crops = read_sql_gdf(
        conn,
        f"""
        SELECT country_code, crop_code, candidate_rank, harvested_area, lon, lat,
               cluster_cell_count, representative_cell_harvested_area, cluster_share,
               representative_distance_m, metric_crs, geometry
        FROM eq.crop_origin_candidates
        WHERE country_code = {qliteral(iso)}
        """,
    )
    if crops.empty:
        empty = gpd.GeoDataFrame(
            columns=["node_id", "node_key", "lon", "lat", "geometry", "component"],
            geometry="geometry",
            crs="EPSG:4326",
        )
        return empty, {"terminals": 0}

    max_node_id = int(scalar(conn, f"SELECT max(node_id) FROM eq.{qident(nodes_table)}") or 0)
    max_component = int(scalar(conn, f"SELECT max(component) FROM eq.{qident(f'{BASE_PREFIX}_components_{suffix}')}") or 0)
    origin_rows = []
    terminal_rows = []
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out_origins)} CASCADE")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(out_origins)} (
                country_code text,
                crop_code text,
                candidate_rank integer,
                harvested_area double precision,
                lon double precision,
                lat double precision,
                cluster_cell_count integer,
                representative_cell_harvested_area double precision,
                cluster_share double precision,
                representative_distance_m double precision,
                metric_crs text,
                node_id bigint,
                node_distance_m double precision,
                road_node_id bigint,
                geometry geometry(Point, 4326),
                PRIMARY KEY (country_code, crop_code, candidate_rank)
            )
            """
        )
        for i, crop in enumerate(crops.itertuples(index=False), start=1):
            terminal_node_id = max_node_id + i
            terminal_component = max_component + i
            terminal_key = f"crop_terminal:{iso}:{crop.crop_code}:{int(crop.candidate_rank)}"
            x1, y1 = float(crop.lon), float(crop.lat)
            cur.execute(
                f"""
                INSERT INTO eq.{qident(nodes_table)} (node_id, node_key, lon, lat, geometry)
                VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                """,
                (terminal_node_id, terminal_key, x1, y1, x1, y1),
            )
            origin_rows.append(
                (
                    crop.country_code,
                    crop.crop_code,
                    int(crop.candidate_rank),
                    None if pd.isna(crop.harvested_area) else float(crop.harvested_area),
                    x1,
                    y1,
                    None if pd.isna(crop.cluster_cell_count) else int(crop.cluster_cell_count),
                    None if pd.isna(crop.representative_cell_harvested_area) else float(crop.representative_cell_harvested_area),
                    None if pd.isna(crop.cluster_share) else float(crop.cluster_share),
                    None if pd.isna(crop.representative_distance_m) else float(crop.representative_distance_m),
                    crop.metric_crs,
                    terminal_node_id,
                    0.0,
                    None,
                    x1,
                    y1,
                )
            )
            terminal_rows.append(
                {
                    "node_id": terminal_node_id,
                    "node_key": terminal_key,
                    "lon": x1,
                    "lat": y1,
                    "geometry": crop.geometry,
                    "component": terminal_component,
                }
            )
        cur.executemany(
            f"""
            INSERT INTO eq.{qident(out_origins)} (
                country_code, crop_code, candidate_rank, harvested_area, lon, lat,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                representative_distance_m, metric_crs, node_id, node_distance_m,
                road_node_id, geometry
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            """,
            origin_rows,
        )
        cur.execute(f"CREATE INDEX {out_origins}_node_idx ON eq.{qident(out_origins)} (node_id)")
        cur.execute(f"CREATE INDEX {out_origins}_geom_idx ON eq.{qident(out_origins)} USING GIST (geometry)")
        cur.execute(f"ANALYZE eq.{qident(out_origins)}")
        cur.execute(f"ANALYZE eq.{qident(nodes_table)}")
    conn.commit()
    terminals = gpd.GeoDataFrame(terminal_rows, geometry="geometry", crs="EPSG:4326")
    return terminals, {"terminals": int(len(terminals))}


def run_country(conn: psycopg.Connection, iso: str) -> dict:
    suffix = iso.lower()
    missing = [
        table
        for table in [f"{BASE_PREFIX}_edges_{suffix}", f"{BASE_PREFIX}_nodes_{suffix}", f"{BASE_PREFIX}_components_{suffix}"]
        if not table_exists(conn, "eq", table)
    ]
    if missing:
        return {"country_code": iso, "skipped": True, "reason": f"missing {missing}"}
    log(f"{iso} copy {BASE_PREFIX} -> {PREFIX}")
    copy_base(conn, iso)
    crop_nodes, metrics = append_crop_terminals(conn, iso)
    road_nodes = read_sql_gdf(
        conn,
        f"""
        SELECT n.node_id, n.node_key, n.lon, n.lat, n.geometry, c.component
        FROM eq.{qident(f'{BASE_PREFIX}_nodes_{suffix}')} n
        JOIN eq.{qident(f'{BASE_PREFIX}_components_{suffix}')} c ON c.node = n.node_id
        """,
    )
    all_nodes = pd.concat([road_nodes, crop_nodes], ignore_index=True)
    all_nodes = gpd.GeoDataFrame(all_nodes, geometry="geometry", crs="EPSG:4326")
    log(f"{iso} graph components+crop terminals={all_nodes.component.nunique():,} crop_terminals={metrics['terminals']:,}")
    links, select_stats = select_spanning_links(all_nodes, [2, 4, 8, 16, 32, 64])
    if not links.empty:
        # Coincident crop terminals still need a positive routing cost; pgRouting
        # drops zero-cost edges in the A* base-table build.
        links.loc[links.length_m <= 0, "length_m"] = 0.001
    append_links(conn, iso, PREFIX, links)
    create_pgr_components_astar(conn, iso, PREFIX)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(DISTINCT component) FROM eq.{qident(f'{PREFIX}_components_{suffix}')}")
        components = int(cur.fetchone()[0])
        cur.execute(f"SELECT count(*) FROM eq.{qident(f'{PREFIX}_edges_pgr_{suffix}')}")
        pgr_edges = int(cur.fetchone()[0])
    crop_link_mask = (
        links.source_key.astype(str).str.startswith("crop_terminal:")
        | links.target_key.astype(str).str.startswith("crop_terminal:")
        if not links.empty
        else pd.Series(dtype=bool)
    )
    crop_links = links[crop_link_mask] if not links.empty else links
    out = {
        "country_code": iso,
        **metrics,
        **select_stats,
        "components": components,
        "pgr_edges": pgr_edges,
        "links": {
            "count": int(len(links)),
            "max_length_m": float(links.length_m.max()) if not links.empty else 0.0,
            "p95_length_m": float(links.length_m.quantile(0.95)) if not links.empty else 0.0,
            "over_2500m": int((links.length_m > 2500).sum()) if not links.empty else 0,
            "over_10000m": int((links.length_m > 10000).sum()) if not links.empty else 0,
        },
        "crop_terminal_links": {
            "count": int(len(crop_links)),
            "max_length_m": float(crop_links.length_m.max()) if not crop_links.empty else 0.0,
            "p95_length_m": float(crop_links.length_m.quantile(0.95)) if not crop_links.empty else 0.0,
            "over_2500m": int((crop_links.length_m > 2500).sum()) if not crop_links.empty else 0,
        },
    }
    path = OUT_DIR / f"{iso}_cluster_connected_metrics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log(
        f"{iso} connected components {select_stats['initial_components']} -> {components} "
        f"links={len(links):,} crop_link_p95={out['crop_terminal_links']['p95_length_m']:.0f}m"
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="auto")
    parser.add_argument("--max-countries", type=int, default=0)
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries = []
    with psycopg.connect(args.db_url) as conn:
        conn.execute("SET statement_timeout = 0")
        countries = country_queue(conn, args.countries)
        if args.max_countries:
            countries = countries[: args.max_countries]
        for iso in countries:
            try:
                summaries.append(run_country(conn, iso))
            except Exception as exc:
                conn.rollback()
                log(f"{iso} ERROR {type(exc).__name__}: {exc}")
                summaries.append({"country_code": iso, "error": f"{type(exc).__name__}: {exc}"})
    (OUT_DIR / "cluster_connected_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    pd.DataFrame(summaries).to_csv(OUT_DIR / "cluster_connected_summary.csv", index=False)
    log(f"done countries={len(summaries)} out={OUT_DIR}")


if __name__ == "__main__":
    main()
