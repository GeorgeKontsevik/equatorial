#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
import sqlalchemy as sa
from scipy.spatial import cKDTree
from shapely.geometry import LineString
from shapely.strtree import STRtree

from rebuild_noded_road_graph import DEFAULT_DB_URL, DEFAULT_SA_URL, build_edges, node_key, qident
from run_weekly_astar_accessibility import country_boundary_wkt


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "guy_connectivity_experiment"
ISO = "GUY"
SUFFIX = "guy"
SNAP_CAP_M = 2500.0
MISSING_OSM_BUFFER_M = 20.0
MISSING_OSM_MIN_UNCOVERED_RATIO = 0.20


def log(message: str) -> None:
    print(message, flush=True)


def metric_crs(bounds) -> str:
    minx, miny, maxx, maxy = bounds
    lon0 = (minx + maxx) / 2.0
    lat0 = (miny + maxy) / 2.0
    return f"+proj=aeqd +lat_0={lat0:.8f} +lon_0={lon0:.8f} +datum=WGS84 +units=m +no_defs"


def qlit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_sql_gdf(conn_or_engine, sql: str) -> gpd.GeoDataFrame:
    return gpd.read_postgis(sql, conn_or_engine, geom_col="geometry").to_crs("EPSG:4326")


def load_current_and_osm(engine: sa.Engine) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    current = read_sql_gdf(
        engine,
        """
        SELECT road_row_id, part_id AS source_part_id, highway, surface_group, geometry
        FROM eq.road_graph_edges_guy
        """,
    )
    osm = read_sql_gdf(
        engine,
        """
        SELECT road_row_id, part_id AS source_part_id, highway, geometry
        FROM eq.osm_road_graph_edges_guy
        """,
    )
    osm["surface_group"] = "unpaved_newosm"

    crs = metric_crs(current.total_bounds)
    current_m = current.to_crs(crs)
    osm_m = osm.to_crs(crs)
    tree = STRtree(list(current_m.geometry))
    nearest_distance_m = []
    for geom in osm_m.geometry:
        nearest_idx = tree.nearest(geom)
        if nearest_idx is None:
            nearest_distance_m.append(float("inf"))
        else:
            nearest_distance_m.append(float(geom.distance(current_m.geometry.iloc[int(nearest_idx)])))
    osm_m["nearest_current_m"] = nearest_distance_m
    missing_osm = osm.loc[osm_m["nearest_current_m"].gt(MISSING_OSM_BUFFER_M)].copy()
    log(
        f"[missing-osm] current_edges={len(current):,} osm_edges={len(osm):,} "
        f"missing_osm_edges={len(missing_osm):,}"
    )
    combined = pd.concat([current, missing_osm], ignore_index=True)
    combined = gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")
    return current, missing_osm, combined


def write_graph_tables(conn: psycopg.Connection, engine: sa.Engine, prefix: str, edges_gdf: gpd.GeoDataFrame) -> None:
    edges = f"{prefix}_edges_guy"
    nodes = f"{prefix}_nodes_guy"
    pgr = f"{prefix}_edges_pgr_guy"
    components = f"{prefix}_components_guy"

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(edges)} CASCADE")
    conn.commit()
    edges_gdf.to_postgis(edges, engine, schema="eq", if_exists="replace", index=False)

    sql = f"""
    ALTER TABLE eq.{qident(edges)} ADD PRIMARY KEY (edge_id);
    CREATE INDEX {edges}_geom_idx ON eq.{qident(edges)} USING GIST (geometry);
    CREATE INDEX {edges}_source_idx ON eq.{qident(edges)} (source_node_id);
    CREATE INDEX {edges}_target_idx ON eq.{qident(edges)} (target_node_id);
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
    CREATE INDEX {nodes}_geom_idx ON eq.{qident(nodes)} USING GIST (geometry);
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


def ensure_astar_base(conn: psycopg.Connection, prefix: str) -> None:
    nodes = f"{prefix}_nodes_guy"
    pgr = f"{prefix}_edges_pgr_guy"
    out = f"{prefix}_edges_pgr_guy_astar_base"
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE UNLOGGED TABLE eq.{qident(out)} AS
            SELECT e.id, e.source, e.target, e.surface_group,
                   e.cost AS base_cost, e.reverse_cost AS base_reverse_cost,
                   ns.lon AS x1, ns.lat AS y1,
                   nt.lon AS x2, nt.lat AS y2
            FROM eq.{qident(pgr)} e
            JOIN eq.{qident(nodes)} ns ON ns.node_id = e.source
            JOIN eq.{qident(nodes)} nt ON nt.node_id = e.target;
            CREATE INDEX {out}_id_idx ON eq.{qident(out)} (id);
            CREATE INDEX {out}_source_idx ON eq.{qident(out)} (source);
            CREATE INDEX {out}_target_idx ON eq.{qident(out)} (target);
            ANALYZE eq.{qident(out)};
            """
        )
    conn.commit()


def component_summary(conn: psycopg.Connection, prefix: str) -> dict[str, float | int]:
    components = f"{prefix}_components_guy"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH comp AS (
                SELECT component, count(*)::bigint nodes
                FROM eq.{qident(components)}
                GROUP BY component
            )
            SELECT sum(nodes)::bigint, count(*)::bigint, max(nodes)::bigint,
                   max(nodes)::float / sum(nodes)::float
            FROM comp
            """
        )
        nodes, comps, largest, share = cur.fetchone()
    return {"nodes": nodes, "components": comps, "largest_nodes": largest, "largest_share": share}


def load_terminals(conn: psycopg.Connection) -> gpd.GeoDataFrame:
    frames = []
    frames.append(
        read_sql_gdf(
            conn,
            """
            SELECT 'crop'::text AS terminal_type, crop_code || ':' || candidate_rank AS terminal_id,
                   crop_code AS label, geometry
            FROM eq.crop_origin_candidates
            WHERE country_code = 'GUY'
            """,
        )
    )
    frames.append(
        read_sql_gdf(
            conn,
            """
            SELECT 'city_5_100k'::text AS terminal_type, geoname_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.city_destinations_5k_100k
            WHERE country_code = 'GUY'
            """,
        )
    )
    frames.append(
        read_sql_gdf(
            conn,
            """
            SELECT 'city_100k_plus'::text AS terminal_type, geoname_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.city_destinations
            WHERE country_code = 'GUY' AND population >= 100000
            """,
        )
    )
    frames.append(
        read_sql_gdf(
            conn,
            f"""
            SELECT 'port'::text AS terminal_type, port_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.port_destinations p
            WHERE ST_Intersects(
                p.geometry,
                ST_GeomFromText({qlit(country_boundary_wkt('GUY'))}, 4326)
            )
            """,
        )
    )
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326")


def snap_terminals(conn: psycopg.Connection, prefix: str) -> gpd.GeoDataFrame:
    terminals = load_terminals(conn)
    nodes = read_sql_gdf(conn, f"SELECT node_id, geometry FROM eq.{qident(f'{prefix}_nodes_guy')}")
    comps = pd.read_sql_query(f"SELECT node AS node_id, component FROM eq.{qident(f'{prefix}_components_guy')}", conn)
    crs = metric_crs(terminals.total_bounds)
    term_m = terminals.to_crs(crs)
    nodes_m = nodes.to_crs(crs)
    tree = cKDTree(list(zip(nodes_m.geometry.x, nodes_m.geometry.y)))
    dist, idx = tree.query(list(zip(term_m.geometry.x, term_m.geometry.y)), k=1)
    snapped = terminals.copy()
    snapped["node_distance_m"] = dist
    snapped["node_id"] = [int(nodes.iloc[i].node_id) if d <= SNAP_CAP_M else None for d, i in zip(dist, idx, strict=True)]
    snapped = snapped.merge(comps, on="node_id", how="left")
    return snapped


def build_terminal_od(snapped: gpd.GeoDataFrame) -> dict[str, int]:
    crops = snapped[(snapped.terminal_type == "crop") & snapped.node_id.notna()].copy()
    out = {}
    for dest_type in ["port", "city_5_100k", "city_100k_plus"]:
        dests = snapped[(snapped.terminal_type == dest_type) & snapped.node_id.notna()].copy()
        od = len(crops) * len(dests)
        if od:
            same = 0
            for comp, group in crops.groupby("component"):
                same += len(group) * int((dests.component == comp).sum())
        else:
            same = 0
        out[f"{dest_type}_od"] = int(od)
        out[f"{dest_type}_same_component"] = int(same)
    return out


def add_synthetic_links(conn: psycopg.Connection, engine: sa.Engine, source_prefix: str, out_prefix: str, snapped: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    terminal_components = sorted(int(c) for c in snapped.component.dropna().unique())
    if len(terminal_components) <= 1:
        return gpd.GeoDataFrame(columns=["source", "target", "length_m", "geometry"], geometry="geometry", crs="EPSG:4326")

    comp_list = ",".join(str(c) for c in terminal_components)
    nodes = read_sql_gdf(
        conn,
        f"""
        SELECT n.node_id, c.component, n.lon, n.lat, n.geometry
        FROM eq.{qident(f'{source_prefix}_nodes_guy')} n
        JOIN eq.{qident(f'{source_prefix}_components_guy')} c ON c.node = n.node_id
        WHERE c.component IN ({comp_list})
        """,
    )
    crs = metric_crs(nodes.total_bounds)
    nodes_m = nodes.to_crs(crs)
    comp_counts = snapped.component.value_counts()
    start_comp = int(comp_counts.index[0])
    connected = {start_comp}
    remaining = set(terminal_components) - connected
    links = []
    while remaining:
        connected_nodes = nodes_m[nodes_m.component.isin(connected)].copy()
        tree = cKDTree(list(zip(connected_nodes.geometry.x, connected_nodes.geometry.y)))
        best = None
        for comp in sorted(remaining):
            comp_nodes = nodes_m[nodes_m.component.eq(comp)].copy()
            distances, indexes = tree.query(list(zip(comp_nodes.geometry.x, comp_nodes.geometry.y)), k=1)
            pos = int(distances.argmin())
            dist_m = float(distances[pos])
            src_row = connected_nodes.iloc[int(indexes[pos])]
            dst_row = comp_nodes.iloc[pos]
            if best is None or dist_m < best["length_m"]:
                best = {
                    "source": int(src_row.node_id),
                    "target": int(dst_row.node_id),
                    "source_component": int(src_row.component),
                    "target_component": int(dst_row.component),
                    "length_m": dist_m,
                    "geometry": LineString([(float(src_row.lon), float(src_row.lat)), (float(dst_row.lon), float(dst_row.lat))]),
                }
        links.append(best)
        connected.add(best["target_component"])
        remaining.remove(best["target_component"])

    links_gdf = gpd.GeoDataFrame(links, geometry="geometry", crs="EPSG:4326")

    # Copy source graph tables and append synthetic pgr edges using existing endpoint nodes.
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_edges_guy')} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(f'{out_prefix}_edges_guy')} AS TABLE eq.{qident(f'{source_prefix}_edges_guy')}")
        max_edge_id = int(pd.read_sql_query(f"SELECT max(edge_id) AS m FROM eq.{qident(f'{out_prefix}_edges_guy')}", conn).iloc[0].m or 0)
        for i, row in enumerate(links_gdf.itertuples(index=False), start=1):
            edge_id = max_edge_id + i
            length_km = float(row.length_m) / 1000.0
            base_speed = 15.0
            x1, y1 = row.geometry.coords[0]
            x2, y2 = row.geometry.coords[-1]
            cur.execute(
                f"""
                INSERT INTO eq.{qident(f'{out_prefix}_edges_guy')} (
                    edge_id, country_code, road_row_id, part_id, source_node_id, target_node_id,
                    source_lon, source_lat, target_lon, target_lat,
                    highway, surface_group, base_speed_kmh, length_km, base_time_h, geometry
                )
                SELECT %s, 'GUY', NULL, %s, ns.node_key, nt.node_key,
                       %s, %s, %s, %s,
                       'synthetic_connector', 'unpaved_synthetic_line', %s, %s, %s,
                       ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)), 4326)
                FROM eq.{qident(f'{source_prefix}_nodes_guy')} ns
                JOIN eq.{qident(f'{source_prefix}_nodes_guy')} nt ON nt.node_id = %s
                WHERE ns.node_id = %s
                """,
                (edge_id, edge_id, x1, y1, x2, y2, base_speed, length_km, length_km / base_speed, x1, y1, x2, y2, row.target, row.source),
            )
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_nodes_guy')} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(f'{out_prefix}_nodes_guy')} AS TABLE eq.{qident(f'{source_prefix}_nodes_guy')}")
        cur.execute(f"ALTER TABLE eq.{qident(f'{out_prefix}_nodes_guy')} ADD PRIMARY KEY (node_id)")
        cur.execute(f"CREATE INDEX {out_prefix}_nodes_guy_geom_idx ON eq.{qident(f'{out_prefix}_nodes_guy')} USING GIST (geometry)")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_edges_pgr_guy')} CASCADE")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(f'{out_prefix}_edges_pgr_guy')} AS
            SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
                   e.road_row_id, e.part_id, e.highway, e.surface_group, e.base_speed_kmh,
                   e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
            FROM eq.{qident(f'{out_prefix}_edges_guy')} e
            JOIN eq.{qident(f'{out_prefix}_nodes_guy')} ns ON ns.node_key = e.source_node_id
            JOIN eq.{qident(f'{out_prefix}_nodes_guy')} nt ON nt.node_key = e.target_node_id
            WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0
            """
        )
        cur.execute(f"ALTER TABLE eq.{qident(f'{out_prefix}_edges_pgr_guy')} ADD PRIMARY KEY (id)")
        cur.execute(f"CREATE INDEX {out_prefix}_edges_pgr_guy_source_idx ON eq.{qident(f'{out_prefix}_edges_pgr_guy')} (source)")
        cur.execute(f"CREATE INDEX {out_prefix}_edges_pgr_guy_target_idx ON eq.{qident(f'{out_prefix}_edges_pgr_guy')} (target)")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_components_guy')} CASCADE")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(f'{out_prefix}_components_guy')} AS
            SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{out_prefix}_edges_pgr_guy')
            """
        )
        cur.execute(f"CREATE INDEX {out_prefix}_components_guy_node_idx ON eq.{qident(f'{out_prefix}_components_guy')} (node)")
        cur.execute(f"CREATE INDEX {out_prefix}_components_guy_component_idx ON eq.{qident(f'{out_prefix}_components_guy')} (component)")
    conn.commit()
    ensure_astar_base(conn, out_prefix)
    return links_gdf


def add_terminal_connectors(conn: psycopg.Connection, source_prefix: str, out_prefix: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    terminals = load_terminals(conn).reset_index(drop=True)
    nodes = read_sql_gdf(conn, f"SELECT node_id, node_key, lon, lat, geometry FROM eq.{qident(f'{source_prefix}_nodes_guy')}")
    crs = metric_crs(terminals.total_bounds)
    terminals_m = terminals.to_crs(crs)
    nodes_m = nodes.to_crs(crs)
    tree = cKDTree(list(zip(nodes_m.geometry.x, nodes_m.geometry.y)))
    distances, indexes = tree.query(list(zip(terminals_m.geometry.x, terminals_m.geometry.y)), k=1)

    connectors = []
    terminal_node_rows = []
    max_node_id = int(nodes.node_id.max() or 0)
    for i, (terminal, dist_m, nearest_idx) in enumerate(zip(terminals.itertuples(index=False), distances, indexes, strict=True), start=1):
        road = nodes.iloc[int(nearest_idx)]
        terminal_node_id = max_node_id + i
        x1, y1 = float(terminal.geometry.x), float(terminal.geometry.y)
        x2, y2 = float(road.lon), float(road.lat)
        terminal_key = f"terminal:{terminal.terminal_type}:{terminal.terminal_id}"
        terminal_node_rows.append(
            {
                "node_id": terminal_node_id,
                "node_key": terminal_key,
                "lon": x1,
                "lat": y1,
                "geometry": terminal.geometry,
                "terminal_type": terminal.terminal_type,
                "terminal_id": terminal.terminal_id,
            }
        )
        connectors.append(
            {
                "terminal_type": terminal.terminal_type,
                "terminal_id": terminal.terminal_id,
                "terminal_node_id": terminal_node_id,
                "road_node_id": int(road.node_id),
                "length_m": float(dist_m),
                "geometry": LineString([(x1, y1), (x2, y2)]),
            }
        )
    connectors_gdf = gpd.GeoDataFrame(connectors, geometry="geometry", crs="EPSG:4326")
    terminal_nodes = gpd.GeoDataFrame(terminal_node_rows, geometry="geometry", crs="EPSG:4326")

    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_edges_guy')} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(f'{out_prefix}_edges_guy')} AS TABLE eq.{qident(f'{source_prefix}_edges_guy')}")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_nodes_guy')} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(f'{out_prefix}_nodes_guy')} AS TABLE eq.{qident(f'{source_prefix}_nodes_guy')}")
        max_edge_id = int(pd.read_sql_query(f"SELECT max(edge_id) AS m FROM eq.{qident(f'{out_prefix}_edges_guy')}", conn).iloc[0].m or 0)
        for row in terminal_nodes.itertuples(index=False):
            cur.execute(
                f"""
                INSERT INTO eq.{qident(f'{out_prefix}_nodes_guy')} (node_id, node_key, lon, lat, geometry)
                VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                """,
                (int(row.node_id), row.node_key, float(row.lon), float(row.lat), float(row.lon), float(row.lat)),
            )
        for i, row in enumerate(connectors_gdf.itertuples(index=False), start=1):
            edge_id = max_edge_id + i
            length_km = float(row.length_m) / 1000.0
            base_speed = 15.0
            x1, y1 = row.geometry.coords[0]
            x2, y2 = row.geometry.coords[-1]
            cur.execute(
                f"""
                INSERT INTO eq.{qident(f'{out_prefix}_edges_guy')} (
                    edge_id, country_code, road_row_id, part_id, source_node_id, target_node_id,
                    source_lon, source_lat, target_lon, target_lat,
                    highway, surface_group, base_speed_kmh, length_km, base_time_h, geometry
                )
                SELECT %s, 'GUY', NULL, %s, tn.node_key, rn.node_key,
                       %s, %s, %s, %s,
                       'terminal_connector', 'unpaved_synthetic_line', %s, %s, %s,
                       ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)), 4326)
                FROM eq.{qident(f'{out_prefix}_nodes_guy')} tn
                JOIN eq.{qident(f'{out_prefix}_nodes_guy')} rn ON rn.node_id = %s
                WHERE tn.node_id = %s
                """,
                (
                    edge_id,
                    edge_id,
                    x1,
                    y1,
                    x2,
                    y2,
                    base_speed,
                    length_km,
                    length_km / base_speed,
                    x1,
                    y1,
                    x2,
                    y2,
                    int(row.road_node_id),
                    int(row.terminal_node_id),
                ),
            )
        cur.execute(f"ALTER TABLE eq.{qident(f'{out_prefix}_nodes_guy')} ADD PRIMARY KEY (node_id)")
        cur.execute(f"CREATE UNIQUE INDEX {out_prefix}_nodes_guy_key_idx ON eq.{qident(f'{out_prefix}_nodes_guy')} (node_key)")
        cur.execute(f"CREATE INDEX {out_prefix}_nodes_guy_geom_idx ON eq.{qident(f'{out_prefix}_nodes_guy')} USING GIST (geometry)")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_edges_pgr_guy')} CASCADE")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(f'{out_prefix}_edges_pgr_guy')} AS
            SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
                   e.road_row_id, e.part_id, e.highway, e.surface_group, e.base_speed_kmh,
                   e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
            FROM eq.{qident(f'{out_prefix}_edges_guy')} e
            JOIN eq.{qident(f'{out_prefix}_nodes_guy')} ns ON ns.node_key = e.source_node_id
            JOIN eq.{qident(f'{out_prefix}_nodes_guy')} nt ON nt.node_key = e.target_node_id
            WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0
            """
        )
        cur.execute(f"ALTER TABLE eq.{qident(f'{out_prefix}_edges_pgr_guy')} ADD PRIMARY KEY (id)")
        cur.execute(f"CREATE INDEX {out_prefix}_edges_pgr_guy_source_idx ON eq.{qident(f'{out_prefix}_edges_pgr_guy')} (source)")
        cur.execute(f"CREATE INDEX {out_prefix}_edges_pgr_guy_target_idx ON eq.{qident(f'{out_prefix}_edges_pgr_guy')} (target)")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(f'{out_prefix}_components_guy')} CASCADE")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(f'{out_prefix}_components_guy')} AS
            SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{out_prefix}_edges_pgr_guy')
            """
        )
        cur.execute(f"CREATE INDEX {out_prefix}_components_guy_node_idx ON eq.{qident(f'{out_prefix}_components_guy')} (node)")
        cur.execute(f"CREATE INDEX {out_prefix}_components_guy_component_idx ON eq.{qident(f'{out_prefix}_components_guy')} (component)")
    conn.commit()
    ensure_astar_base(conn, out_prefix)
    return connectors_gdf, terminal_nodes


def draw_stage_map(conn: psycopg.Connection, prefix: str, stage: str, snapped: gpd.GeoDataFrame, extra_edges: gpd.GeoDataFrame | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    boundary = gpd.read_file(ROOT / "data/raw/gadm/GUY/gadm41_GUY.gpkg", layer="ADM_ADM_0").to_crs("EPSG:4326")
    edges = read_sql_gdf(conn, f"SELECT surface_group, geometry FROM eq.{qident(f'{prefix}_edges_guy')}")
    fig, ax = plt.subplots(figsize=(12, 15))
    boundary.boundary.plot(ax=ax, color="#222222", linewidth=0.9)
    for surface, color, width, alpha in [
        ("paved", "#009e73", 0.55, 0.82),
        ("unpaved", "#d55e00", 0.55, 0.78),
        ("unknown", "#8c8c8c", 0.38, 0.48),
        ("unpaved_newosm", "#e7298a", 0.85, 0.92),
        ("unpaved_synthetic_line", "#54278f", 2.2, 0.98),
    ]:
        sub = edges[edges.surface_group.eq(surface)]
        if not sub.empty:
            sub.plot(ax=ax, color=color, linewidth=width, alpha=alpha, label=surface)
    if extra_edges is not None and not extra_edges.empty:
        extra_edges.plot(ax=ax, color="#6a3d9a", linewidth=1.4, alpha=0.95, label="synthetic links")
    for terminal_type, marker, color in [
        ("crop", "x", "#666666"),
        ("city_5_100k", "o", "#377eb8"),
        ("city_100k_plus", "^", "#08519c"),
        ("port", "s", "#e41a1c"),
    ]:
        sub = snapped[snapped.terminal_type.eq(terminal_type)]
        if not sub.empty:
            ax.scatter(sub.geometry.x, sub.geometry.y, s=28, marker=marker, color=color, label=terminal_type, zorder=6)
    minx, miny, maxx, maxy = boundary.total_bounds
    ax.set_xlim(minx - 0.25, maxx + 0.25)
    ax.set_ylim(miny - 0.25, maxy + 0.25)
    ax.grid(True, color="#dddddd", linewidth=0.4, alpha=0.55)
    ax.set_title(f"GUY connectivity experiment: {stage}")
    ax.legend(loc="lower left", fontsize=8, frameon=True)
    path = OUT_DIR / f"GUY_{stage}.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    log(f"[map] {path}")
    return path


def main() -> None:
    engine = sa.create_engine(DEFAULT_SA_URL)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    metrics = {}
    with psycopg.connect(DEFAULT_DB_URL) as conn:
        # Stage 0: current graph.
        current_snapped = snap_terminals(conn, "road_graph")
        metrics["current"] = component_summary(conn, "road_graph") | build_terminal_od(current_snapped)
        draw_stage_map(conn, "road_graph", "01_current", current_snapped)

        # Stage 1: current graph plus OSM roads absent from current geometry.
        _, missing_osm, combined = load_current_and_osm(engine)
        metrics["missing_osm_edges"] = int(len(missing_osm))
        hybrid_edges = build_edges("GUY_HYBRID_OSM", combined)
        write_graph_tables(conn, engine, "guy_hybrid_osm", hybrid_edges)
        ensure_astar_base(conn, "guy_hybrid_osm")
        hybrid_snapped = snap_terminals(conn, "guy_hybrid_osm")
        metrics["hybrid_osm"] = component_summary(conn, "guy_hybrid_osm") | build_terminal_od(hybrid_snapped)
        draw_stage_map(conn, "guy_hybrid_osm", "02_hybrid_osm", hybrid_snapped)

        # Stage 2: synthetic straight links connecting terminal-bearing components.
        links = add_synthetic_links(conn, engine, "guy_hybrid_osm", "guy_hybrid_connected", hybrid_snapped)
        connected_snapped = snap_terminals(conn, "guy_hybrid_connected")
        metrics["synthetic_links"] = {
            "count": int(len(links)),
            "max_length_m": float(links.length_m.max()) if not links.empty else 0.0,
            "p95_length_m": float(links.length_m.quantile(0.95)) if not links.empty else 0.0,
            "over_2500m": int((links.length_m > 2500).sum()) if not links.empty else 0,
        }
        metrics["hybrid_connected"] = component_summary(conn, "guy_hybrid_connected") | build_terminal_od(connected_snapped)
        draw_stage_map(conn, "guy_hybrid_connected", "03_hybrid_connected", connected_snapped, links)

        # Stage 3: put every crop/city/port point into the graph, then reconnect
        # terminal-bearing components so every terminal OD can route on-graph.
        terminal_connectors, _ = add_terminal_connectors(conn, "guy_hybrid_osm", "guy_terminal_connectors")
        terminal_connector_snapped = snap_terminals(conn, "guy_terminal_connectors")
        metrics["terminal_connectors"] = {
            "count": int(len(terminal_connectors)),
            "max_length_m": float(terminal_connectors.length_m.max()) if not terminal_connectors.empty else 0.0,
            "p95_length_m": float(terminal_connectors.length_m.quantile(0.95)) if not terminal_connectors.empty else 0.0,
            "over_2500m": int((terminal_connectors.length_m > 2500).sum()) if not terminal_connectors.empty else 0,
        }
        metrics["terminal_connectors_graph"] = component_summary(conn, "guy_terminal_connectors") | build_terminal_od(
            terminal_connector_snapped
        )
        draw_stage_map(
            conn,
            "guy_terminal_connectors",
            "04_terminal_connectors",
            terminal_connector_snapped,
            terminal_connectors,
        )

        terminal_links = add_synthetic_links(
            conn,
            engine,
            "guy_terminal_connectors",
            "guy_terminal_connected",
            terminal_connector_snapped,
        )
        terminal_connected_snapped = snap_terminals(conn, "guy_terminal_connected")
        metrics["terminal_component_links"] = {
            "count": int(len(terminal_links)),
            "max_length_m": float(terminal_links.length_m.max()) if not terminal_links.empty else 0.0,
            "p95_length_m": float(terminal_links.length_m.quantile(0.95)) if not terminal_links.empty else 0.0,
            "over_2500m": int((terminal_links.length_m > 2500).sum()) if not terminal_links.empty else 0,
        }
        metrics["terminal_connected"] = component_summary(conn, "guy_terminal_connected") | build_terminal_od(
            terminal_connected_snapped
        )
        draw_stage_map(
            conn,
            "guy_terminal_connected",
            "05_terminal_connected",
            terminal_connected_snapped,
            terminal_links,
        )

    (OUT_DIR / "GUY_connectivity_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    log(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
