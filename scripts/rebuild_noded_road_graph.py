#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import math
import re
import time
from pathlib import Path

import geopandas as gpd
import pandas as pd
import psycopg
import sqlalchemy as sa
from shapely.geometry import GeometryCollection, LineString, MultiLineString
from shapely.ops import unary_union
from shapely.strtree import STRtree


DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SA_URL = "postgresql+psycopg://gk@127.0.0.1:5432/equatorial"
NON_TRUCK_HIGHWAYS = {"footway", "path", "steps", "pedestrian", "cycleway", "bridleway", "living_street"}


def qident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return f'"{value}"'


def log(message: str) -> None:
    print(message, flush=True)


def iter_lines(geometry):
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        if len(geometry.coords) >= 2:
            yield geometry
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            yield from iter_lines(part)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            yield from iter_lines(part)


def node_key(x: float, y: float) -> str:
    # Match the existing graph contract: stable hash of 5-decimal lon/lat.
    raw = f"{round(float(x), 5)}:{round(float(y), 5)}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def surface_group(row: pd.Series) -> str:
    for col in ["surface", "pred_label", "osm_surface_class", "combined_surface_osm_priority", "combined_surface_DL_priority"]:
        value = row.get(col)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        value = str(value).lower()
        if value in {"paved", "unpaved"}:
            return value
    return "unknown"


def base_speed_kmh(highway: str, surface: str) -> float:
    highway = str(highway or "").lower()
    if highway in {"motorway", "motorway_link"}:
        speed = 90.0
    elif highway in {"trunk", "trunk_link"}:
        speed = 80.0
    elif highway in {"primary", "primary_link"}:
        speed = 70.0
    elif highway in {"secondary", "secondary_link"}:
        speed = 60.0
    elif highway in {"tertiary", "tertiary_link"}:
        speed = 50.0
    elif highway in {"unclassified", "residential"}:
        speed = 35.0
    elif highway == "service":
        speed = 25.0
    elif highway == "track":
        speed = 20.0
    else:
        speed = 30.0
    if surface in {"unpaved", "unpaved_newosm", "unpaved_synthetic_line"}:
        return speed * 0.75
    if surface == "unknown":
        return speed * 0.85
    return speed


def read_roads(engine: sa.Engine, iso: str) -> gpd.GeoDataFrame:
    table = f"road_surface_{iso.lower()}"
    sql = f"""
        SELECT id AS road_row_id, highway, surface, pred_label, osm_surface_class,
               combined_surface_osm_priority, "combined_surface_DL_priority", geometry
        FROM public.{qident(table)}
        WHERE geometry IS NOT NULL
          AND NOT ST_IsEmpty(geometry)
          AND coalesce(lower(highway::text), '') <> ''
          AND coalesce(lower(highway::text), '') NOT IN ({",".join(repr(x) for x in sorted(NON_TRUCK_HIGHWAYS))})
    """
    roads = gpd.read_postgis(sql, engine, geom_col="geometry").to_crs("EPSG:4326")
    rows = []
    for _, row in roads.iterrows():
        for part_id, line in enumerate(iter_lines(row.geometry) or [], start=1):
            rows.append(
                {
                    "road_row_id": int(row["road_row_id"]) if row["road_row_id"] is not None else None,
                    "source_part_id": part_id,
                    "highway": str(row["highway"]).lower() if row["highway"] is not None else "",
                    "surface_group": surface_group(row),
                    "geometry": line,
                }
            )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def build_edges(iso: str, roads: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    t0 = time.time()
    log(f"[noding] {iso} input_lines={len(roads):,}")
    noded_lines = list(iter_lines(unary_union(list(roads.geometry))) or [])
    log(f"[noding] {iso} noded_lines={len(noded_lines):,} elapsed_s={time.time() - t0:.1f}")

    tree = STRtree(list(roads.geometry))
    records = []
    misses = 0
    for edge_id, line in enumerate(noded_lines, start=1):
        if line.is_empty or len(line.coords) < 2:
            continue
        match_idx = None
        idxs = tree.query(line, predicate="covered_by")
        if len(idxs):
            match_idx = int(idxs[0])
        else:
            misses += 1
            nearest = tree.nearest(line)
            if nearest is not None:
                match_idx = int(nearest)
        if match_idx is None:
            continue
        attr = roads.iloc[match_idx]
        x1, y1 = line.coords[0]
        x2, y2 = line.coords[-1]
        length_km = float(gpd.GeoSeries([line], crs="EPSG:4326").to_crs("EPSG:3857").length.iloc[0] / 1000.0)
        if length_km <= 0:
            continue
        speed = base_speed_kmh(attr.highway, attr.surface_group)
        records.append(
            {
                "edge_id": edge_id,
                "country_code": iso,
                "road_row_id": int(attr.road_row_id) if pd.notna(attr.road_row_id) else None,
                "part_id": int(edge_id),
                "source_node_id": node_key(x1, y1),
                "target_node_id": node_key(x2, y2),
                "source_lon": float(x1),
                "source_lat": float(y1),
                "target_lon": float(x2),
                "target_lat": float(y2),
                "highway": attr.highway,
                "surface_group": attr.surface_group,
                "base_speed_kmh": float(speed),
                "length_km": length_km,
                "base_time_h": length_km / speed,
                "geometry": line,
            }
        )
    log(f"[attribute] {iso} rows={len(records):,} misses_nearest_fallback={misses:,}")
    return gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")


def write_graph(conn: psycopg.Connection, engine: sa.Engine, iso: str, edges_gdf: gpd.GeoDataFrame) -> None:
    suffix = iso.lower()
    edges = f"road_graph_edges_{suffix}"
    nodes = f"road_graph_nodes_{suffix}"
    pgr = f"road_graph_edges_pgr_{suffix}"
    components = f"road_graph_components_{suffix}"

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
    CREATE INDEX {pgr}_road_idx ON eq.{qident(pgr)} (road_row_id);
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild road_graph_* tables with intersection noding in Python/Shapely.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--sa-url", default=DEFAULT_SA_URL)
    parser.add_argument("--countries", required=True, help="Comma-separated ISO3 list")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    countries = [part.strip().upper() for part in args.countries.split(",") if part.strip()]
    engine = sa.create_engine(args.sa_url)
    with psycopg.connect(args.db_url) as conn:
        for iso in countries:
            t0 = time.time()
            roads = read_roads(engine, iso)
            edges = build_edges(iso, roads)
            write_graph(conn, engine, iso, edges)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    WITH sizes AS (
                      SELECT component, count(*) nodes
                      FROM eq.{qident(f"road_graph_components_{iso.lower()}")}
                      GROUP BY component
                    )
                    SELECT count(*), max(nodes) FROM sizes
                    """
                )
                comps, largest = cur.fetchone()
            log(f"[done] {iso} components={int(comps or 0):,} largest={int(largest or 0):,} elapsed_s={time.time() - t0:.1f}")


if __name__ == "__main__":
    main()
