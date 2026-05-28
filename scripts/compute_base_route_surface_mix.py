#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "base_route_surface_mix"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def qident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return f'"{value}"'


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> object | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    return bool(scalar(conn, "SELECT to_regclass(%s)", (f"{schema}.{table}",)))


def short_graph_tag(graph_prefix: str) -> str:
    if graph_prefix == "road_graph":
        return "rg"
    if graph_prefix == "component_connected":
        return "cc"
    if graph_prefix == "cluster_connected":
        return "clc"
    return re.sub(r"[^A-Za-z0-9_]", "_", graph_prefix)[:12]


def origin_scope(args: argparse.Namespace) -> str:
    origin_prefix = "allclusters" if args.top_per_crop <= 0 else f"top{args.top_per_crop}_per_crop"
    scope = f"{origin_prefix}_{args.small_city_limit}small_{args.large_city_limit}large_{args.port_limit}ports"
    if args.airport_limit > 0:
        scope += f"_{args.airport_limit}airports"
    if args.graph_prefix != "road_graph":
        scope = f"{args.graph_prefix}_{scope}"
    return scope


def od_table_name(iso: str, args: argparse.Namespace) -> str:
    suffix = iso.lower()
    limit_tag = f"{args.small_city_limit}s_{args.large_city_limit}l_{args.port_limit}p"
    if args.airport_limit > 0:
        limit_tag += f"_{args.airport_limit}a"
    table = f"crop_access_astar_od_{short_graph_tag(args.graph_prefix)}_{suffix}_{limit_tag}"
    if args.top_per_crop <= 0:
        table += "_allclusters"
    return table


def astar_base_name(iso: str, graph_prefix: str) -> str:
    return f"{graph_prefix}_edges_pgr_{iso.lower()}_astar_base"


def edge_table_name(iso: str, graph_prefix: str) -> str:
    return f"{graph_prefix}_edges_pgr_{iso.lower()}"


def countries_from_results(conn: psycopg.Connection, scope: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code
            FROM eq.crop_accessibility_weekly_astar
            WHERE origin_scope = %s
            GROUP BY country_code
            ORDER BY country_code
            """,
            (scope,),
        )
        return [row[0] for row in cur.fetchall()]


def parse_countries(conn: psycopg.Connection, requested: str, scope: str) -> list[str]:
    if requested.lower() == "auto":
        return countries_from_results(conn, scope)
    return [item.strip().upper() for item in requested.split(",") if item.strip()]


def ensure_output_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eq.crop_accessibility_base_route_surface_mix (
                run_at timestamptz NOT NULL DEFAULT now(),
                country_code text NOT NULL,
                graph_prefix text NOT NULL,
                origin_scope text NOT NULL,
                measurement text NOT NULL DEFAULT 'base_cost_no_rain',
                crop_code text NOT NULL,
                candidate_rank integer NOT NULL,
                crop_rank integer,
                harvested_area double precision,
                cluster_cell_count integer,
                representative_cell_harvested_area double precision,
                cluster_share double precision,
                dest_type text NOT NULL,
                dest_rank integer NOT NULL,
                dest_id text NOT NULL,
                dest_name text,
                population bigint,
                origin_node bigint NOT NULL,
                dest_node bigint NOT NULL,
                straight_dist_km double precision,
                route_status text NOT NULL,
                total_edge_count integer NOT NULL,
                total_length_km double precision NOT NULL,
                total_travel_time_h double precision NOT NULL,
                surface_group text NOT NULL,
                surface_edge_count integer NOT NULL,
                surface_edge_pct double precision NOT NULL,
                surface_length_km double precision NOT NULL,
                surface_length_pct double precision NOT NULL,
                surface_travel_time_h double precision NOT NULL,
                surface_travel_time_pct double precision NOT NULL,
                PRIMARY KEY (
                    country_code, graph_prefix, origin_scope, measurement,
                    crop_code, candidate_rank, dest_type, dest_rank, dest_id, surface_group
                )
            );
            CREATE INDEX IF NOT EXISTS crop_accessibility_base_route_surface_mix_country_idx
                ON eq.crop_accessibility_base_route_surface_mix (country_code, dest_type, crop_code);
            CREATE INDEX IF NOT EXISTS crop_accessibility_base_route_surface_mix_surface_idx
                ON eq.crop_accessibility_base_route_surface_mix (surface_group);
            """
        )
    conn.commit()


def compute_country(conn: psycopg.Connection, iso: str, args: argparse.Namespace, scope: str) -> dict[str, object]:
    od_table = od_table_name(iso, args)
    astar_base = astar_base_name(iso, args.graph_prefix)
    edge_table = edge_table_name(iso, args.graph_prefix)
    missing = [name for name in (od_table, astar_base, edge_table) if not table_exists(conn, "eq", name)]
    if missing:
        log(f"skip {iso} missing={','.join(missing)}")
        return {"country_code": iso, "status": "skipped", "missing": missing}

    od_rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(od_table)}") or 0)
    if od_rows == 0:
        log(f"skip {iso} od_rows=0")
        return {"country_code": iso, "status": "skipped", "od_rows": 0}

    t0 = time.monotonic()
    edge_sql = f"""
        SELECT id, source, target,
               base_cost AS cost,
               base_reverse_cost AS reverse_cost,
               x1, y1, x2, y2
        FROM eq.{qident(astar_base)}
    """.replace("\n", " ")

    if args.replace:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM eq.crop_accessibility_base_route_surface_mix
                WHERE country_code = %s AND graph_prefix = %s AND origin_scope = %s AND measurement = %s
                """,
                (iso, args.graph_prefix, scope, args.measurement),
            )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_base_surface_pairs")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_base_surface_pairs ON COMMIT DROP AS
            SELECT od_id, country_code, crop_code, candidate_rank, crop_rank, harvested_area,
                   cluster_cell_count, representative_cell_harvested_area, cluster_share,
                   dest_type, dest_rank, dest_id, dest_name, population,
                   origin_node, dest_node, straight_dist_km
            FROM eq.{qident(od_table)}
            WHERE country_code = %s
            """,
            (iso,),
        )
        cur.execute("CREATE INDEX tmp_base_surface_pairs_pair_idx ON tmp_base_surface_pairs (origin_node, dest_node)")
        cur.execute("CREATE INDEX tmp_base_surface_pairs_od_idx ON tmp_base_surface_pairs (od_id)")
        cur.execute("DROP TABLE IF EXISTS tmp_base_surface_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_base_surface_steps ON COMMIT DROP AS
            SELECT *
            FROM pgr_aStar(
                %s,
                'SELECT od_id AS id, origin_node AS source, dest_node AS target FROM tmp_base_surface_pairs',
                false,
                5,
                1.0,
                1.0
            )
            WHERE edge <> -1
            """,
            (edge_sql,),
        )
        cur.execute("CREATE INDEX tmp_base_surface_steps_route_idx ON tmp_base_surface_steps (start_vid, end_vid)")
        cur.execute("CREATE INDEX tmp_base_surface_steps_edge_idx ON tmp_base_surface_steps (edge)")
        cur.execute("DROP TABLE IF EXISTS tmp_base_surface_route_totals")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_base_surface_route_totals ON COMMIT DROP AS
            SELECT p.od_id, p.origin_node, p.dest_node,
                   count(s.edge)::integer AS total_edge_count,
                   COALESCE(sum(e.length_km), 0)::double precision AS total_length_km,
                   COALESCE(sum(s.cost), 0)::double precision AS total_travel_time_h
            FROM tmp_base_surface_pairs p
            LEFT JOIN tmp_base_surface_steps s
              ON s.start_vid = p.origin_node AND s.end_vid = p.dest_node
            LEFT JOIN eq.{qident(edge_table)} e ON e.id = s.edge
            GROUP BY p.od_id, p.origin_node, p.dest_node
            """
        )
        cur.execute("CREATE INDEX tmp_base_surface_route_totals_od_idx ON tmp_base_surface_route_totals (od_id)")
        cur.execute("DROP TABLE IF EXISTS tmp_base_surface_route_mix")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_base_surface_route_mix ON COMMIT DROP AS
            SELECT p.od_id, COALESCE(e.surface_group, 'missing_surface')::text AS surface_group,
                   count(s.edge)::integer AS surface_edge_count,
                   COALESCE(sum(e.length_km), 0)::double precision AS surface_length_km,
                   COALESCE(sum(s.cost), 0)::double precision AS surface_travel_time_h
            FROM tmp_base_surface_pairs p
            JOIN tmp_base_surface_steps s
              ON s.start_vid = p.origin_node AND s.end_vid = p.dest_node
            JOIN eq.{qident(edge_table)} e ON e.id = s.edge
            GROUP BY p.od_id, COALESCE(e.surface_group, 'missing_surface')
            """
        )
        cur.execute("CREATE INDEX tmp_base_surface_route_mix_od_idx ON tmp_base_surface_route_mix (od_id)")
        cur.execute(
            """
            INSERT INTO eq.crop_accessibility_base_route_surface_mix (
                country_code, graph_prefix, origin_scope, measurement,
                crop_code, candidate_rank, crop_rank, harvested_area,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                dest_type, dest_rank, dest_id, dest_name, population,
                origin_node, dest_node, straight_dist_km, route_status,
                total_edge_count, total_length_km, total_travel_time_h,
                surface_group, surface_edge_count, surface_edge_pct,
                surface_length_km, surface_length_pct,
                surface_travel_time_h, surface_travel_time_pct
            )
            SELECT p.country_code, %s, %s, %s,
                   p.crop_code, p.candidate_rank, p.crop_rank, p.harvested_area,
                   p.cluster_cell_count, p.representative_cell_harvested_area, p.cluster_share,
                   p.dest_type, p.dest_rank, p.dest_id, p.dest_name, p.population,
                   p.origin_node, p.dest_node, p.straight_dist_km,
                   CASE WHEN t.total_edge_count > 0 THEN 'ok' ELSE 'unreachable' END,
                   t.total_edge_count, t.total_length_km, t.total_travel_time_h,
                   m.surface_group, m.surface_edge_count,
                   m.surface_edge_count::double precision / NULLIF(t.total_edge_count, 0),
                   m.surface_length_km,
                   m.surface_length_km / NULLIF(t.total_length_km, 0),
                   m.surface_travel_time_h,
                   m.surface_travel_time_h / NULLIF(t.total_travel_time_h, 0)
            FROM tmp_base_surface_pairs p
            JOIN tmp_base_surface_route_totals t ON t.od_id = p.od_id
            JOIN tmp_base_surface_route_mix m ON m.od_id = p.od_id
            ON CONFLICT (
                country_code, graph_prefix, origin_scope, measurement,
                crop_code, candidate_rank, dest_type, dest_rank, dest_id, surface_group
            ) DO UPDATE SET
                run_at = now(),
                crop_rank = EXCLUDED.crop_rank,
                harvested_area = EXCLUDED.harvested_area,
                cluster_cell_count = EXCLUDED.cluster_cell_count,
                representative_cell_harvested_area = EXCLUDED.representative_cell_harvested_area,
                cluster_share = EXCLUDED.cluster_share,
                dest_name = EXCLUDED.dest_name,
                population = EXCLUDED.population,
                origin_node = EXCLUDED.origin_node,
                dest_node = EXCLUDED.dest_node,
                straight_dist_km = EXCLUDED.straight_dist_km,
                route_status = EXCLUDED.route_status,
                total_edge_count = EXCLUDED.total_edge_count,
                total_length_km = EXCLUDED.total_length_km,
                total_travel_time_h = EXCLUDED.total_travel_time_h,
                surface_edge_count = EXCLUDED.surface_edge_count,
                surface_edge_pct = EXCLUDED.surface_edge_pct,
                surface_length_km = EXCLUDED.surface_length_km,
                surface_length_pct = EXCLUDED.surface_length_pct,
                surface_travel_time_h = EXCLUDED.surface_travel_time_h,
                surface_travel_time_pct = EXCLUDED.surface_travel_time_pct
            """,
            (args.graph_prefix, scope, args.measurement),
        )
        cur.execute(
            """
            SELECT count(DISTINCT od_id) FILTER (WHERE total_edge_count > 0),
                   count(DISTINCT od_id),
                   COALESCE(sum(total_edge_count), 0),
                   COALESCE(sum(total_length_km), 0)
            FROM tmp_base_surface_route_totals
            """
        )
        reachable_od, total_od, route_edges, route_length_km = cur.fetchone()
    conn.commit()

    rows = int(
        scalar(
            conn,
            """
            SELECT count(*)
            FROM eq.crop_accessibility_base_route_surface_mix
            WHERE country_code = %s AND graph_prefix = %s AND origin_scope = %s AND measurement = %s
            """,
            (iso, args.graph_prefix, scope, args.measurement),
        )
        or 0
    )
    elapsed = time.monotonic() - t0
    item = {
        "country_code": iso,
        "status": "done",
        "od_rows": od_rows,
        "reachable_od": int(reachable_od or 0),
        "total_od": int(total_od or 0),
        "surface_rows": rows,
        "route_edges": int(route_edges or 0),
        "route_length_km": float(route_length_km or 0),
        "elapsed_s": round(elapsed, 1),
    }
    log(
        f"{iso} done od={int(total_od or 0):,} reachable={int(reachable_od or 0):,} "
        f"surface_rows={rows:,} elapsed_s={elapsed:.1f}"
    )
    return item


def write_csv_exports(conn: psycopg.Connection, scope: str, args: argparse.Namespace, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        with (out_dir / "by_country_crop_dest_surface.csv").open("wb") as handle:
            with cur.copy(
                """
                COPY (
                    WITH routes AS (
                        SELECT country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id,
                               max(total_edge_count) AS total_edge_count,
                               max(total_length_km) AS total_length_km,
                               max(total_travel_time_h) AS total_travel_time_h
                        FROM eq.crop_accessibility_base_route_surface_mix
                        WHERE graph_prefix = %s AND origin_scope = %s AND measurement = %s
                        GROUP BY country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id
                    ), denom AS (
                        SELECT country_code, crop_code, dest_type,
                               count(*) AS od_count,
                               sum(total_edge_count) AS total_edge_count,
                               sum(total_length_km) AS total_length_km,
                               sum(total_travel_time_h) AS total_travel_time_h
                        FROM routes
                        GROUP BY country_code, crop_code, dest_type
                    ), numer AS (
                        SELECT country_code, crop_code, dest_type, surface_group,
                               sum(surface_edge_count) AS surface_edge_count,
                               sum(surface_length_km) AS surface_length_km,
                               sum(surface_travel_time_h) AS surface_travel_time_h
                        FROM eq.crop_accessibility_base_route_surface_mix
                        WHERE graph_prefix = %s AND origin_scope = %s AND measurement = %s
                        GROUP BY country_code, crop_code, dest_type, surface_group
                    )
                    SELECT n.country_code, n.crop_code, n.dest_type, n.surface_group,
                           d.od_count,
                           n.surface_edge_count::double precision / NULLIF(d.total_edge_count, 0) AS edge_pct,
                           n.surface_length_km / NULLIF(d.total_length_km, 0) AS length_pct,
                           n.surface_travel_time_h / NULLIF(d.total_travel_time_h, 0) AS travel_time_pct,
                           n.surface_edge_count::bigint AS surface_edge_count,
                           d.total_edge_count::bigint AS total_edge_count,
                           n.surface_length_km,
                           d.total_length_km,
                           n.surface_travel_time_h,
                           d.total_travel_time_h
                    FROM numer n
                    JOIN denom d USING (country_code, crop_code, dest_type)
                    ORDER BY n.country_code, n.crop_code, n.dest_type, n.surface_group
                ) TO STDOUT WITH CSV HEADER
                """,
                (
                    args.graph_prefix,
                    scope,
                    args.measurement,
                    args.graph_prefix,
                    scope,
                    args.measurement,
                ),
            ) as copy:
                for data in copy:
                    handle.write(data)
        with (out_dir / "by_od_surface.csv").open("wb") as handle:
            with cur.copy(
                """
                COPY (
                    SELECT country_code, crop_code, candidate_rank, crop_rank,
                           cluster_cell_count, cluster_share,
                           dest_type, dest_rank, dest_id, dest_name,
                           route_status, total_edge_count, total_length_km, total_travel_time_h,
                           surface_group, surface_edge_count, surface_edge_pct,
                           surface_length_km, surface_length_pct,
                           surface_travel_time_h, surface_travel_time_pct
                    FROM eq.crop_accessibility_base_route_surface_mix
                    WHERE graph_prefix = %s AND origin_scope = %s AND measurement = %s
                    ORDER BY country_code, crop_code, candidate_rank, dest_type, dest_rank, surface_group
                ) TO STDOUT WITH CSV HEADER
                """,
                (args.graph_prefix, scope, args.measurement),
            ) as copy:
                for data in copy:
                    handle.write(data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute baseline route surface mix for crop-cluster OD paths.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="auto")
    parser.add_argument("--graph-prefix", default="cluster_connected")
    parser.add_argument("--top-per-crop", type=int, default=0)
    parser.add_argument("--small-city-limit", type=int, default=10)
    parser.add_argument("--large-city-limit", type=int, default=3)
    parser.add_argument("--port-limit", type=int, default=3)
    parser.add_argument("--airport-limit", type=int, default=3)
    parser.add_argument("--measurement", default="base_cost_no_rain")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scope = origin_scope(args)
    manifest: list[dict[str, object]] = []
    with psycopg.connect(args.db_url) as conn:
        conn.execute("SET application_name = 'base_route_surface_mix'")
        conn.execute("SET statement_timeout = 0")
        ensure_output_table(conn)
        countries = parse_countries(conn, args.countries, scope)
        log(f"scope={scope} countries={','.join(countries)}")
        for iso in countries:
            manifest.append(compute_country(conn, iso, args, scope))
        if args.export_csv:
            write_csv_exports(conn, scope, args, args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"manifest={manifest_path}")


if __name__ == "__main__":
    main()
