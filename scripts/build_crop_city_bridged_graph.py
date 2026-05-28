#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import time

import psycopg

DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SPEED_KMH = 30.0
TOP_N_CONNECTED = 3


def log(message: str) -> None:
    print(message, flush=True)


def qident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"'


def qiso_literal(iso: str) -> str:
    if not re.fullmatch(r"[A-Z]{3}", iso):
        raise ValueError(f"Unsafe ISO code: {iso}")
    return f"'{iso}'"


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        return cur.fetchone()[0] is not None


def scalar(conn: psycopg.Connection, sql: str) -> int:
    with conn.cursor() as cur:
        cur.execute(sql)
        value = cur.fetchone()[0]
    return int(value or 0)


def loaded_countries(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code
            FROM eq.boxplot_stats_weekly
            GROUP BY country_code
            HAVING count(*)=1908 AND count(DISTINCT week_start)=53
            ORDER BY country_code
            """
        )
        return [row[0] for row in cur.fetchall()]


def zero_selected_countries(conn: psycopg.Connection, candidates: list[str]) -> list[str]:
    out: list[str] = []
    for iso in candidates:
        selected = f"crop_origin_selected_{iso.lower()}"
        if not table_exists(conn, "eq", selected):
            continue
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(selected)}")
        if rows == 0:
            out.append(iso)
    return out


def build_country(conn: psycopg.Connection, iso: str, speed_kmh: float, force: bool) -> None:
    suffix = iso.lower()
    iso_sql = qiso_literal(iso)
    pgr = f"road_graph_edges_pgr_{suffix}"
    nodes = f"road_graph_nodes_{suffix}"
    components = f"road_graph_components_{suffix}"
    origin_nodes = f"crop_origin_nodes_{suffix}"
    city_nodes = f"city_destination_nodes_{suffix}"
    city_components = f"city_destination_components_{suffix}"
    connectors = f"road_graph_connectors_{suffix}"
    bridge = f"road_graph_edges_pgr_{suffix}_bridge"
    bridge_components = f"road_graph_components_{suffix}_bridge"
    bridge_city_components = f"city_destination_components_{suffix}_bridge"
    selected = f"crop_origin_selected_{suffix}_bridge"

    required = [pgr, nodes, components, origin_nodes, city_nodes, city_components]
    missing = [table for table in required if not table_exists(conn, "eq", table)]
    if missing:
        log(f"[skip] {iso} missing tables={','.join(missing)}")
        return
    if table_exists(conn, "eq", selected) and not force:
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(selected)}")
        if rows > 0:
            log(f"[skip] {iso} bridge selected exists rows={rows:,}")
            return

    t0 = time.time()
    old_selected_table = f"crop_origin_selected_{suffix}"
    old_selected = scalar(conn, f"SELECT count(*) FROM eq.{qident(old_selected_table)}") if table_exists(conn, "eq", old_selected_table) else 0
    origin_rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(origin_nodes)}")
    city_rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(city_nodes)}")
    log(f"[start] {iso} bridge origin_nodes={origin_rows:,} city_nodes={city_rows:,} old_selected={old_selected:,}")

    sql = f"""
    DROP TABLE IF EXISTS eq.{qident(connectors)};
    CREATE TABLE eq.{qident(connectors)} AS
    WITH city_node_geoms AS (
        SELECT DISTINCT c.node_id, n.geometry
        FROM eq.{qident(city_nodes)} c
        JOIN eq.{qident(nodes)} n ON n.node_id = c.node_id
        WHERE c.country_code = {iso_sql}
    ), city_components_raw AS (
        SELECT DISTINCT component FROM eq.{qident(city_components)}
    ), origin_nodes_raw AS (
        SELECT DISTINCT o.node_id, oc.component, n.geometry
        FROM eq.{qident(origin_nodes)} o
        JOIN eq.{qident(components)} oc ON oc.node = o.node_id
        JOIN eq.{qident(nodes)} n ON n.node_id = o.node_id
        LEFT JOIN city_components_raw cc ON cc.component = oc.component
        WHERE o.country_code = {iso_sql}
          AND cc.component IS NULL
    ), nearest AS (
        SELECT o.node_id AS source,
               o.component AS source_component,
               c.node_id AS target,
               ST_Distance(o.geometry::geography, c.geometry::geography) AS length_m,
               ST_MakeLine(o.geometry, c.geometry)::geometry(LineString, 4326) AS geometry
        FROM origin_nodes_raw o
        CROSS JOIN LATERAL (
            SELECT c.node_id, c.geometry
            FROM city_node_geoms c
            ORDER BY o.geometry <-> c.geometry
            LIMIT 1
        ) c
    )
    SELECT row_number() OVER ()::bigint AS connector_id,
           {iso_sql}::text AS country_code,
           source, target, source_component,
           length_m / 1000.0 AS length_km,
           {float(speed_kmh)}::double precision AS base_speed_kmh,
           (length_m / 1000.0) / {float(speed_kmh)}::double precision AS cost,
           'crop_city_component_connector'::text AS connector_type,
           geometry
    FROM nearest
    WHERE length_m > 0;
    ALTER TABLE eq.{qident(connectors)} ADD PRIMARY KEY (connector_id);
    CREATE INDEX {connectors}_source_idx ON eq.{qident(connectors)} (source);
    CREATE INDEX {connectors}_target_idx ON eq.{qident(connectors)} (target);
    CREATE INDEX {connectors}_geom_gist ON eq.{qident(connectors)} USING GIST (geometry);
    ANALYZE eq.{qident(connectors)};

    DROP TABLE IF EXISTS eq.{qident(bridge)};
    CREATE TABLE eq.{qident(bridge)} AS
    SELECT id, source, target, road_row_id, part_id, highway, surface_group,
           base_speed_kmh::double precision AS base_speed_kmh, length_km, cost, reverse_cost
    FROM eq.{qident(pgr)}
    UNION ALL
    SELECT (SELECT coalesce(max(id), 0) FROM eq.{qident(pgr)}) + connector_id AS id,
           source, target,
           NULL::bigint AS road_row_id,
           NULL::integer AS part_id,
           'synthetic_connector'::text AS highway,
           'synthetic_connector'::text AS surface_group,
           base_speed_kmh,
           length_km,
           cost,
           cost AS reverse_cost
    FROM eq.{qident(connectors)};
    ALTER TABLE eq.{qident(bridge)} ADD PRIMARY KEY (id);
    CREATE INDEX {bridge}_source_idx ON eq.{qident(bridge)} (source);
    CREATE INDEX {bridge}_target_idx ON eq.{qident(bridge)} (target);
    CREATE INDEX {bridge}_road_idx ON eq.{qident(bridge)} (road_row_id);
    ANALYZE eq.{qident(bridge)};

    DROP TABLE IF EXISTS eq.{qident(bridge_components)};
    CREATE TABLE eq.{qident(bridge_components)} AS
    SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{bridge}');
    CREATE INDEX {bridge_components}_node_idx ON eq.{qident(bridge_components)} (node);
    CREATE INDEX {bridge_components}_component_idx ON eq.{qident(bridge_components)} (component);
    ANALYZE eq.{qident(bridge_components)};

    DROP TABLE IF EXISTS eq.{qident(bridge_city_components)};
    CREATE TABLE eq.{qident(bridge_city_components)} AS
    SELECT c.*, cc.component
    FROM eq.{qident(city_nodes)} c
    JOIN eq.{qident(bridge_components)} cc ON cc.node = c.node_id;
    ALTER TABLE eq.{qident(bridge_city_components)} ADD PRIMARY KEY (country_code, geoname_id);
    CREATE INDEX {bridge_city_components}_component_idx ON eq.{qident(bridge_city_components)} (component);
    CREATE INDEX {bridge_city_components}_node_idx ON eq.{qident(bridge_city_components)} (node_id);
    ANALYZE eq.{qident(bridge_city_components)};

    DROP TABLE IF EXISTS eq.{qident(selected)};
    CREATE TABLE eq.{qident(selected)} AS
    WITH city_components AS (
        SELECT DISTINCT component FROM eq.{qident(bridge_city_components)}
    ), origin_components AS (
        SELECT o.*, oc.component, (city_components.component IS NOT NULL) AS connected_to_city,
               CASE WHEN city_components.component IS NOT NULL THEN
                   row_number() OVER (
                       PARTITION BY o.crop_code, (city_components.component IS NOT NULL)
                       ORDER BY o.harvested_area DESC, o.candidate_rank
                   )
               END AS connected_rank
        FROM eq.{qident(origin_nodes)} o
        JOIN eq.{qident(bridge_components)} oc ON oc.node = o.node_id
        LEFT JOIN city_components ON city_components.component = oc.component
        WHERE o.country_code = {iso_sql}
    )
    SELECT country_code, crop_code, candidate_rank, connected_rank AS selected_rank,
           harvested_area, lon, lat, node_id, component, node_distance_m, geometry
    FROM origin_components
    WHERE connected_to_city AND connected_rank <= {TOP_N_CONNECTED};
    ALTER TABLE eq.{qident(selected)} ADD PRIMARY KEY (country_code, crop_code, selected_rank);
    CREATE INDEX {selected}_node_idx ON eq.{qident(selected)} (node_id);
    CREATE INDEX {selected}_component_idx ON eq.{qident(selected)} (component);
    ANALYZE eq.{qident(selected)};
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()

    connectors_count = scalar(conn, f"SELECT count(*) FROM eq.{qident(connectors)}")
    new_selected = scalar(conn, f"SELECT count(*) FROM eq.{qident(selected)}")
    max_km = 0.0
    p50_km = 0.0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY length_km), 0),
                   coalesce(max(length_km), 0)
            FROM eq.{qident(connectors)}
            """
        )
        p50_km, max_km = (float(x or 0) for x in cur.fetchone())
    log(
        f"[done] {iso} connectors={connectors_count:,} selected_bridge={new_selected:,} "
        f"connector_p50_km={p50_km:.2f} connector_max_km={max_km:.2f} elapsed_s={time.time() - t0:.1f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create crop-city synthetic connector edges and bridged pgRouting tables.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="zero", help="Comma-separated ISO3 list, loaded, or zero.")
    parser.add_argument("--speed-kmh", type=float, default=DEFAULT_SPEED_KMH)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "loaded":
            countries = loaded_countries(conn)
        elif args.countries.strip().lower() == "zero":
            countries = zero_selected_countries(conn, loaded_countries(conn))
        else:
            countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()]
        log(f"[bridge] countries={','.join(countries)} speed_kmh={args.speed_kmh:g} force={args.force}")
        for iso in countries:
            build_country(conn, iso, args.speed_kmh, args.force)
    log("[bridge] complete")


if __name__ == "__main__":
    main()
