#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

import psycopg

DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> int | float | str | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    return bool(
        scalar(
            conn,
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
            """,
            (schema, table),
        )
    )


def run_step(conn: psycopg.Connection, label: str, sql: str) -> float:
    log(f"start {label}")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    elapsed = time.monotonic() - t0
    log(f"done {label} elapsed_s={elapsed:.1f}")
    return elapsed


def ensure_astar_base(conn: psycopg.Connection, force: bool, graph: str) -> str:
    base_table = "road_graph_edges_pgr_bra_bridge_astar_base" if graph == "bridge" else "road_graph_edges_pgr_bra_c5_astar_base"
    source_table = "road_graph_edges_pgr_bra_bridge" if graph == "bridge" else "road_graph_edges_pgr_bra_c5_cell"
    if force or not table_exists(conn, "eq", base_table):
        run_step(
            conn,
            "astar edge base",
            """
            DROP TABLE IF EXISTS eq.__BASE_TABLE__;
            CREATE UNLOGGED TABLE eq.__BASE_TABLE__ AS
            SELECT e.id, e.source, e.target, e.surface_group, e.cost AS base_cost, e.reverse_cost AS base_reverse_cost, m.cell_id,
                   ns.lon AS x1, ns.lat AS y1, nt.lon AS x2, nt.lat AS y2
            FROM eq.__SOURCE_TABLE__ e
            JOIN eq.road_graph_nodes_bra ns ON ns.node_id = e.source
            JOIN eq.road_graph_nodes_bra nt ON nt.node_id = e.target
            LEFT JOIN eq.road_era5_cell_map m ON m.country_code = 'BRA' AND m.road_row_id = e.road_row_id
            WHERE e.cost IS NOT NULL AND e.cost > 0
              AND e.reverse_cost IS NOT NULL AND e.reverse_cost > 0;
            ALTER TABLE eq.__BASE_TABLE__ ADD PRIMARY KEY (id);
            CREATE INDEX __BASE_TABLE___cell_idx ON eq.__BASE_TABLE__ (cell_id);
            CREATE INDEX __BASE_TABLE___source_idx ON eq.__BASE_TABLE__ (source);
            CREATE INDEX __BASE_TABLE___target_idx ON eq.__BASE_TABLE__ (target);
            ANALYZE eq.__BASE_TABLE__;
            """.replace("__BASE_TABLE__", base_table).replace("__SOURCE_TABLE__", source_table),
        )
    else:
        rows = scalar(conn, f"SELECT reltuples::bigint FROM pg_class WHERE oid = 'eq.{base_table}'::regclass")
        log(f"skip astar edge base graph={graph} table={base_table} approx_rows={int(rows or 0):,}")
    return base_table


def ensure_destination_nodes(conn: psycopg.Connection, force: bool) -> None:
    if force or not table_exists(conn, "eq", "city_destination_nodes_5k_100k_bra"):
        run_step(
            conn,
            "snap 5k-100k cities to BRA graph",
            """
            DROP TABLE IF EXISTS eq.city_destination_nodes_5k_100k_bra;
            CREATE UNLOGGED TABLE eq.city_destination_nodes_5k_100k_bra AS
            SELECT c.country_code, c.geoname_id, c.name, c.population, c.lon, c.lat,
                   n.node_id,
                   ST_Distance(c.geometry::geography, n.geometry::geography) AS node_distance_m,
                   c.geometry
            FROM eq.city_destinations_5k_100k c
            CROSS JOIN LATERAL (
                SELECT node_id, geometry
                FROM eq.road_graph_nodes_bra n
                ORDER BY n.geometry <-> c.geometry
                LIMIT 1
            ) n
            WHERE c.country_code = 'BRA';
            ALTER TABLE eq.city_destination_nodes_5k_100k_bra ADD PRIMARY KEY (country_code, geoname_id);
            CREATE INDEX city_destination_nodes_5k_100k_bra_node_idx
              ON eq.city_destination_nodes_5k_100k_bra (node_id);
            CREATE INDEX city_destination_nodes_5k_100k_bra_geom_idx
              ON eq.city_destination_nodes_5k_100k_bra USING GIST (geometry);
            ANALYZE eq.city_destination_nodes_5k_100k_bra;
            """,
        )
    else:
        rows = scalar(conn, "SELECT count(*) FROM eq.city_destination_nodes_5k_100k_bra")
        log(f"skip city nodes rows={int(rows or 0):,}")

    if force or not table_exists(conn, "eq", "port_destination_nodes_bra"):
        run_step(
            conn,
            "snap ports to BRA graph",
            """
            DROP TABLE IF EXISTS eq.port_destination_nodes_bra;
            CREATE UNLOGGED TABLE eq.port_destination_nodes_bra AS
            SELECT p.port_id, p.name, p.natlscale, p.lon, p.lat,
                   n.node_id,
                   ST_Distance(p.geometry::geography, n.geometry::geography) AS node_distance_m,
                   p.geometry
            FROM eq.port_destinations p
            CROSS JOIN LATERAL (
                SELECT node_id, geometry
                FROM eq.road_graph_nodes_bra n
                ORDER BY n.geometry <-> p.geometry
                LIMIT 1
            ) n;
            ALTER TABLE eq.port_destination_nodes_bra ADD PRIMARY KEY (port_id);
            CREATE INDEX port_destination_nodes_bra_node_idx ON eq.port_destination_nodes_bra (node_id);
            CREATE INDEX port_destination_nodes_bra_geom_idx ON eq.port_destination_nodes_bra USING GIST (geometry);
            ANALYZE eq.port_destination_nodes_bra;
            """,
        )
    else:
        rows = scalar(conn, "SELECT count(*) FROM eq.port_destination_nodes_bra")
        log(f"skip port nodes rows={int(rows or 0):,}")


def origin_scope_label(origin_limit: int | None, top_per_crop: int | None) -> str:
    if top_per_crop is not None:
        return f"top{top_per_crop}_per_crop"
    return "all" if origin_limit is None else str(origin_limit)


def build_od(conn: psycopg.Connection, origin_limit: int | None, top_per_crop: int | None, graph: str) -> int:
    limit_clause = "" if origin_limit is None else f"LIMIT {int(origin_limit)}"
    crop_filter = "" if top_per_crop is None else f"WHERE crop_rank <= {int(top_per_crop)}"
    components_table = "road_graph_components_bra_bridge" if graph == "bridge" else "road_graph_components_bra"
    run_step(
        conn,
        "build benchmark OD pairs",
        f"""
        DROP TABLE IF EXISTS eq.bra_astar_benchmark_od;
        CREATE UNLOGGED TABLE eq.bra_astar_benchmark_od AS
        WITH ranked_origins AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                   o.node_id AS origin_node, oc.component, o.geometry,
                   row_number() OVER (
                       PARTITION BY o.crop_code
                       ORDER BY o.harvested_area DESC NULLS LAST, o.candidate_rank
                   ) AS crop_rank
            FROM eq.crop_origin_nodes_bra o
            JOIN eq.__COMPONENTS_TABLE__ oc ON oc.node = o.node_id
            WHERE o.country_code = 'BRA' AND o.node_id IS NOT NULL
        ), origins AS (
            SELECT country_code, crop_code, candidate_rank, harvested_area, origin_node, component, geometry
            FROM ranked_origins
            {crop_filter}
            ORDER BY crop_code, crop_rank
            {limit_clause}
        ), city_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                   'city'::text AS dest_type, c.rank::integer AS dest_rank,
                   c.geoname_id::text AS dest_id, c.name AS dest_name, c.population,
                   o.origin_node, c.node_id AS dest_node,
                   ST_Distance(o.geometry::geography, c.geometry::geography) / 1000.0 AS straight_dist_km
            FROM origins o
            CROSS JOIN LATERAL (
                SELECT c.geoname_id, c.name, c.population, c.node_id, c.geometry,
                       row_number() OVER (ORDER BY o.geometry <-> c.geometry) AS rank
                FROM eq.city_destination_nodes_5k_100k_bra c
                JOIN eq.__COMPONENTS_TABLE__ cc ON cc.node = c.node_id AND cc.component = o.component
                ORDER BY o.geometry <-> c.geometry
                LIMIT 10
            ) c
        ), port_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                   'port'::text AS dest_type, 1::integer AS dest_rank,
                   p.port_id::text AS dest_id, p.name AS dest_name, NULL::bigint AS population,
                   o.origin_node, p.node_id AS dest_node,
                   ST_Distance(o.geometry::geography, p.geometry::geography) / 1000.0 AS straight_dist_km
            FROM origins o
            CROSS JOIN LATERAL (
                SELECT p.port_id, p.name, p.node_id, p.geometry
                FROM eq.port_destination_nodes_bra p
                JOIN eq.__COMPONENTS_TABLE__ pc ON pc.node = p.node_id AND pc.component = o.component
                ORDER BY o.geometry <-> p.geometry
                LIMIT 1
            ) p
        )
        SELECT row_number() OVER () AS od_id, *
        FROM (
            SELECT * FROM city_od
            UNION ALL
            SELECT * FROM port_od
        ) q;
        ALTER TABLE eq.bra_astar_benchmark_od ADD PRIMARY KEY (od_id);
        CREATE INDEX bra_astar_benchmark_od_pair_idx ON eq.bra_astar_benchmark_od (origin_node, dest_node);
        ANALYZE eq.bra_astar_benchmark_od;
        """.replace("__COMPONENTS_TABLE__", components_table),
    )
    return int(scalar(conn, "SELECT count(*) FROM eq.bra_astar_benchmark_od") or 0)


def run_astar(
    conn: psycopg.Connection,
    week_start: str,
    scenario: str,
    origin_limit: int | None,
    top_per_crop: int | None,
    astar_base_table: str,
) -> float:
    origin_limit_label = origin_scope_label(origin_limit, top_per_crop)
    run_step(
        conn,
        "prepare result table",
        """
        CREATE TABLE IF NOT EXISTS eq.bra_astar_benchmark_results (
            run_at timestamptz NOT NULL DEFAULT now(),
            week_start date NOT NULL,
            scenario text NOT NULL,
            origin_limit text NOT NULL,
            country_code text NOT NULL,
            crop_code text NOT NULL,
            candidate_rank integer NOT NULL,
            harvested_area double precision,
            dest_type text NOT NULL,
            dest_rank integer NOT NULL,
            dest_id text NOT NULL,
            dest_name text,
            population bigint,
            origin_node bigint NOT NULL,
            dest_node bigint NOT NULL,
            straight_dist_km double precision,
            travel_time_h double precision
        );
        """,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM eq.bra_astar_benchmark_results
            WHERE week_start = %s AND scenario = %s AND origin_limit = %s
            """,
            (week_start, scenario, origin_limit_label),
        )
    conn.commit()

    edge_sql = f"""
        SELECT e.id, e.source, e.target,
               e.base_cost / GREATEST(
                   CASE
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 150 THEN 0.35
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 100 THEN 0.60
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 50 THEN 0.80
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 200 THEN 0.50
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 100 THEN 0.75
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 50 THEN 0.90
                       ELSE 1.0
                   END,
                   0.05
               ) AS cost,
               e.base_reverse_cost / GREATEST(
                   CASE
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 150 THEN 0.35
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 100 THEN 0.60
                       WHEN e.surface_group = 'unpaved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 50 THEN 0.80
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 200 THEN 0.50
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 100 THEN 0.75
                       WHEN e.surface_group = 'paved' AND COALESCE(g.tp_sum_weekly_mm, 0) >= 50 THEN 0.90
                       ELSE 1.0
                   END,
                   0.05
               ) AS reverse_cost,
               e.x1, e.y1, e.x2, e.y2
        FROM eq.__ASTAR_BASE_TABLE__ e
        LEFT JOIN eq.era5_precip_weekly_grid g
          ON g.country_code = 'BRA'
         AND g.week_start = DATE '{week_start}'
         AND g.cell_id = e.cell_id
    """.replace("__ASTAR_BASE_TABLE__", astar_base_table).replace("\n", " ")
    combinations_sql = "SELECT origin_node AS source, dest_node AS target FROM eq.bra_astar_benchmark_od"

    log(f"start astar week={week_start} scenario={scenario} origin_limit={origin_limit_label}")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eq.bra_astar_benchmark_results (
                week_start, scenario, origin_limit, country_code, crop_code, candidate_rank, harvested_area,
                dest_type, dest_rank, dest_id, dest_name, population,
                origin_node, dest_node, straight_dist_km, travel_time_h
            )
            SELECT %s::date, %s::text, %s::text, od.country_code, od.crop_code, od.candidate_rank, od.harvested_area,
                   od.dest_type, od.dest_rank, od.dest_id, od.dest_name, od.population,
                   od.origin_node, od.dest_node, od.straight_dist_km, r.agg_cost
            FROM pgr_aStarCost(%s, %s, false, 5, 1.0, 1.0) r
            JOIN eq.bra_astar_benchmark_od od
              ON od.origin_node = r.start_vid AND od.dest_node = r.end_vid
            """,
            (week_start, scenario, origin_limit_label, edge_sql, combinations_sql),
        )
    conn.commit()
    elapsed = time.monotonic() - t0
    rows = scalar(
        conn,
        """
        SELECT count(*) FROM eq.bra_astar_benchmark_results
        WHERE week_start = %s AND scenario = %s AND origin_limit = %s
        """,
        (week_start, scenario, origin_limit_label),
    )
    log(f"done astar rows={int(rows or 0):,} elapsed_s={elapsed:.1f}")
    return elapsed


def verify(conn: psycopg.Connection, week_start: str, scenario: str, origin_limit: int | None, top_per_crop: int | None) -> None:
    origin_limit_label = origin_scope_label(origin_limit, top_per_crop)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT crop_code || ':' || candidate_rank::text) AS origins,
                   count(*) FILTER (WHERE travel_time_h IS NULL) AS null_times,
                   min(travel_time_h), percentile_cont(0.5) WITHIN GROUP (ORDER BY travel_time_h), max(travel_time_h)
            FROM eq.bra_astar_benchmark_results
            WHERE week_start = %s AND scenario = %s AND origin_limit = %s
            """,
            (week_start, scenario, origin_limit_label),
        )
        row = cur.fetchone()
    log(
        "verify "
        f"rows={row[0]:,} origins={row[1]:,} null_times={row[2]:,} "
        f"travel_h_min={row[3]:.3f} median={row[4]:.3f} max={row[5]:.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Brazil weekly A* crop-to-city/port routing in PostGIS/pgRouting.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--week-start", default="2024-01-01")
    parser.add_argument("--scenario", default="weekly_sum_default")
    parser.add_argument("--origin-limit", type=int, default=100, help="Use 0 for all origins.")
    parser.add_argument("--top-per-crop", type=int, default=0, help="Use top N origins by harvested_area within each crop_code. 0 disables.")
    parser.add_argument("--graph", choices=["c5", "bridge"], default="bridge")
    parser.add_argument("--force-cache", action="store_true")
    args = parser.parse_args()

    origin_limit = None if args.origin_limit == 0 else args.origin_limit
    top_per_crop = None if args.top_per_crop == 0 else args.top_per_crop
    log(f"connect db={args.db_url} week={args.week_start} origin_scope={origin_scope_label(origin_limit, top_per_crop)}")
    with psycopg.connect(args.db_url) as conn:
        conn.execute("SET application_name = 'bra_astar_weekly_benchmark'")
        conn.execute("SET statement_timeout = 0")
        astar_base_table = ensure_astar_base(conn, args.force_cache, args.graph)
        ensure_destination_nodes(conn, args.force_cache)
        od_rows = build_od(conn, origin_limit, top_per_crop, args.graph)
        log(f"od rows={od_rows:,}")
        elapsed = run_astar(conn, args.week_start, args.scenario, origin_limit, top_per_crop, astar_base_table)
        verify(conn, args.week_start, args.scenario, origin_limit, top_per_crop)
        if origin_limit and top_per_crop is None:
            total_origins = int(scalar(conn, "SELECT count(*) FROM eq.crop_origin_nodes_bra WHERE country_code = 'BRA' AND node_id IS NOT NULL") or 0)
            per_origin = elapsed / max(origin_limit, 1)
            log(f"rough extrapolation total_origins={total_origins:,} per_origin_s={per_origin:.2f} all_origin_s={per_origin * total_origins:.1f}")


if __name__ == "__main__":
    main()
