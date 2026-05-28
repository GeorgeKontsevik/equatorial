#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import psycopg
import pyogrio
import sqlalchemy as sa

from rebuild_noded_road_graph import (
    DEFAULT_DB_URL,
    DEFAULT_SA_URL,
    NON_TRUCK_HIGHWAYS,
    build_edges,
    qident,
)


ROOT = Path(__file__).resolve().parents[1]


def log(message: str) -> None:
    print(message, flush=True)


def default_pbf(iso: str) -> Path:
    mapping = {
        "GUY": ROOT / "data/raw/osm/south-america__guyana/guyana-latest.osm.pbf",
    }
    if iso not in mapping:
        raise ValueError(f"No default PBF path for {iso}; pass --pbf explicitly")
    return mapping[iso]


def read_osm_roads(pbf: Path, iso: str) -> gpd.GeoDataFrame:
    log(f"[read] {iso} pbf={pbf}")
    frame = pyogrio.read_dataframe(
        pbf,
        layer="lines",
        columns=["osm_id", "highway", "other_tags", "geometry"],
    ).to_crs("EPSG:4326")
    frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
    frame["highway"] = frame["highway"].astype("string").str.lower()
    frame = frame[frame["highway"].notna() & ~frame["highway"].isin(NON_TRUCK_HIGHWAYS)].copy()
    frame["road_row_id"] = frame["osm_id"].astype("int64", errors="ignore")
    frame["source_part_id"] = 1
    frame["surface_group"] = "unknown"
    rows = []
    for _, row in frame.iterrows():
        rows.append(
            {
                "road_row_id": int(row["osm_id"]),
                "source_part_id": 1,
                "highway": str(row["highway"]),
                "surface_group": "unknown",
                "geometry": row.geometry,
            }
        )
    roads = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    log(f"[read] {iso} truckable_osm_lines={len(roads):,}")
    return roads


def write_osm_graph(conn: psycopg.Connection, engine: sa.Engine, iso: str, edges_gdf: gpd.GeoDataFrame) -> None:
    suffix = iso.lower()
    prefix = f"osm_road_graph"
    edges = f"{prefix}_edges_{suffix}"
    nodes = f"{prefix}_nodes_{suffix}"
    pgr = f"{prefix}_edges_pgr_{suffix}"
    components = f"{prefix}_components_{suffix}"

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(edges)} CASCADE")
    conn.commit()
    edges_gdf.to_postgis(edges, engine, schema="eq", if_exists="replace", index=False)

    sql = f"""
    ALTER TABLE eq.{qident(edges)} ADD PRIMARY KEY (edge_id);
    CREATE INDEX {edges}_road_idx ON eq.{qident(edges)} (road_row_id);
    CREATE INDEX {edges}_source_idx ON eq.{qident(edges)} (source_node_id);
    CREATE INDEX {edges}_target_idx ON eq.{qident(edges)} (target_node_id);
    CREATE INDEX {edges}_geom_idx ON eq.{qident(edges)} USING GIST (geometry);
    ANALYZE eq.{qident(edges)};

    DROP TABLE IF EXISTS eq.{qident(nodes)} CASCADE;
    CREATE TABLE eq.{qident(nodes)} AS
    WITH raw_nodes AS (
        SELECT source_node_id AS node_key, source_lon AS lon, source_lat AS lat FROM eq.{qident(edges)}
        UNION ALL
        SELECT target_node_id AS node_key, target_lon AS lon, target_lat AS lat FROM eq.{qident(edges)}
    ), grouped AS (
        SELECT node_key, avg(lon) AS lon, avg(lat) AS lat FROM raw_nodes GROUP BY node_key
    )
    SELECT row_number() OVER ()::bigint AS node_id, node_key, lon, lat,
           ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geometry(Point, 4326) AS geometry
    FROM grouped;
    ALTER TABLE eq.{qident(nodes)} ADD PRIMARY KEY (node_id);
    CREATE UNIQUE INDEX {nodes}_key_idx ON eq.{qident(nodes)} (node_key);
    CREATE INDEX {nodes}_geom_gist ON eq.{qident(nodes)} USING GIST (geometry);
    ANALYZE eq.{qident(nodes)};

    DROP TABLE IF EXISTS eq.{qident(pgr)} CASCADE;
    CREATE TABLE eq.{qident(pgr)} AS
    SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
           e.road_row_id, e.part_id, e.highway, e.surface_group, e.base_speed_kmh,
           e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
    FROM eq.{qident(edges)} e
    JOIN eq.{qident(nodes)} ns ON ns.node_key = e.source_node_id
    JOIN eq.{qident(nodes)} nt ON nt.node_key = e.target_node_id
    WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0;
    ALTER TABLE eq.{qident(pgr)} ADD PRIMARY KEY (id);
    CREATE INDEX {pgr}_source_idx ON eq.{qident(pgr)} (source);
    CREATE INDEX {pgr}_target_idx ON eq.{qident(pgr)} (target);
    ANALYZE eq.{qident(pgr)};

    DROP TABLE IF EXISTS eq.{qident(components)} CASCADE;
    CREATE TABLE eq.{qident(components)} AS
    SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{pgr}');
    CREATE INDEX {components}_node_idx ON eq.{qident(components)} (node);
    CREATE INDEX {components}_component_idx ON eq.{qident(components)} (component);
    ANALYZE eq.{qident(components)};
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def component_summary(conn: psycopg.Connection, iso: str) -> None:
    suffix = iso.lower()
    table = f"osm_road_graph_components_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH comp AS (
              SELECT component, count(*)::bigint nodes
              FROM eq.{qident(table)}
              GROUP BY component
            )
            SELECT sum(nodes)::bigint graph_nodes,
                   count(*)::bigint components,
                   max(nodes)::bigint largest_nodes,
                   max(nodes)::float / sum(nodes)::float AS largest_share
            FROM comp
            """
        )
        print(f"[components] {iso} {cur.fetchone()}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a pure OSM noded road graph into eq.osm_road_graph_* tables.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--sa-url", default=DEFAULT_SA_URL)
    parser.add_argument("--country", required=True)
    parser.add_argument("--pbf", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    iso = args.country.upper()
    pbf = args.pbf or default_pbf(iso)
    engine = sa.create_engine(args.sa_url)
    roads = read_osm_roads(pbf, iso)
    edges = build_edges(f"OSM_{iso}", roads)
    with psycopg.connect(args.db_url) as conn:
        write_osm_graph(conn, engine, iso, edges)
        component_summary(conn, iso)


if __name__ == "__main__":
    main()
