#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
NON_TRUCK_HIGHWAYS = ("footway", "path", "steps", "pedestrian", "cycleway", "bridleway", "living_street")


def qident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return f'"{value}"'


def qliteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def log(message: str) -> None:
    print(message, flush=True)


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        return cur.fetchone()[0] is not None


def loaded_countries(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT upper(substring(relname from '^crop_accessibility_astar_od_([a-z]{3})$')) AS iso
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'eq'
              AND c.relkind IN ('r', 'u')
              AND c.relname ~ '^crop_accessibility_astar_od_[a-z]{3}$'
            ORDER BY 1
            """
        )
        return [row[0] for row in cur.fetchall()]


def before_stats(conn: psycopg.Connection, iso: str) -> dict[str, int]:
    suffix = iso.lower()
    od = f"crop_accessibility_astar_od_{suffix}"
    components = f"road_graph_components_{suffix}"
    if not table_exists(conn, "eq", od) or not table_exists(conn, "eq", components):
        return {"od_total": 0, "reachable": 0, "unreachable": 0}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                count(*)::bigint AS od_total,
                count(*) FILTER (WHERE oc.component = dc.component)::bigint AS reachable,
                count(*) FILTER (WHERE oc.component IS NULL OR dc.component IS NULL OR oc.component <> dc.component)::bigint AS unreachable
            FROM eq.{qident(od)} od
            LEFT JOIN eq.{qident(components)} oc ON oc.node = od.origin_node
            LEFT JOIN eq.{qident(components)} dc ON dc.node = od.dest_node
            WHERE od.dest_type = 'port'
            """
        )
        row = cur.fetchone()
    return {key: int(row[key] or 0) for key in ["od_total", "reachable", "unreachable"]}


def build_temp_noded_graph(conn: psycopg.Connection, iso: str) -> dict[str, int]:
    suffix = iso.lower()
    source = f"road_surface_{suffix}"
    if not table_exists(conn, "public", source):
        raise RuntimeError(f"missing public.{source}")
    non_truck = "(" + ",".join(qliteral(x) for x in NON_TRUCK_HIGHWAYS) + ")"
    sql = f"""
    DROP TABLE IF EXISTS tmp_noded_edges;
    DROP TABLE IF EXISTS tmp_noded_nodes;
    DROP TABLE IF EXISTS tmp_noded_pgr;
    DROP TABLE IF EXISTS tmp_noded_components;

    CREATE TEMP TABLE tmp_noded_edges AS
    WITH dumped AS (
        SELECT (d).geom::geometry(LineString, 4326) AS geometry
        FROM public.{qident(source)} r
        CROSS JOIN LATERAL ST_Dump(r.geometry) AS d
        WHERE r.geometry IS NOT NULL
          AND NOT ST_IsEmpty(r.geometry)
          AND GeometryType((d).geom) = 'LINESTRING'
          AND coalesce(lower(r.highway::text), '') NOT IN {non_truck}
    ), linework AS (
        SELECT ST_UnaryUnion(ST_Collect(geometry)) AS geometry
        FROM dumped
    )
    SELECT row_number() OVER ()::bigint AS edge_id,
           md5(round(ST_X(ST_StartPoint((d).geom))::numeric, 5)::text || ':' || round(ST_Y(ST_StartPoint((d).geom))::numeric, 5)::text) AS source_node_key,
           md5(round(ST_X(ST_EndPoint((d).geom))::numeric, 5)::text || ':' || round(ST_Y(ST_EndPoint((d).geom))::numeric, 5)::text) AS target_node_key,
           ST_X(ST_StartPoint((d).geom)) AS source_lon,
           ST_Y(ST_StartPoint((d).geom)) AS source_lat,
           ST_X(ST_EndPoint((d).geom)) AS target_lon,
           ST_Y(ST_EndPoint((d).geom)) AS target_lat,
           ST_Length((d).geom::geography) / 1000.0 AS length_km,
           (d).geom::geometry(LineString, 4326) AS geometry
        FROM linework
        CROSS JOIN LATERAL ST_Dump(ST_Node(linework.geometry)) AS d
        WHERE GeometryType((d).geom) = 'LINESTRING'
          AND ST_NPoints((d).geom) >= 2
          AND ST_Length((d).geom::geography) > 0;
    CREATE INDEX tmp_noded_edges_geom_idx ON tmp_noded_edges USING GIST (geometry);
    ANALYZE tmp_noded_edges;

    CREATE TEMP TABLE tmp_noded_nodes AS
    WITH raw_nodes AS (
        SELECT source_node_key AS node_key, source_lon AS lon, source_lat AS lat FROM tmp_noded_edges
        UNION ALL
        SELECT target_node_key AS node_key, target_lon AS lon, target_lat AS lat FROM tmp_noded_edges
    ), grouped AS (
        SELECT node_key, avg(lon) AS lon, avg(lat) AS lat
        FROM raw_nodes
        GROUP BY node_key
    )
    SELECT row_number() OVER ()::bigint AS node_id,
           node_key,
           ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geometry(Point, 4326) AS geometry
    FROM grouped;
    CREATE UNIQUE INDEX tmp_noded_nodes_key_idx ON tmp_noded_nodes (node_key);
    CREATE INDEX tmp_noded_nodes_geom_idx ON tmp_noded_nodes USING GIST (geometry);
    ANALYZE tmp_noded_nodes;

    CREATE TEMP TABLE tmp_noded_pgr AS
    SELECT e.edge_id AS id,
           ns.node_id AS source,
           nt.node_id AS target,
           GREATEST(e.length_km / 30.0, 0.000001)::double precision AS cost,
           GREATEST(e.length_km / 30.0, 0.000001)::double precision AS reverse_cost
    FROM tmp_noded_edges e
    JOIN tmp_noded_nodes ns ON ns.node_key = e.source_node_key
    JOIN tmp_noded_nodes nt ON nt.node_key = e.target_node_key
    WHERE ns.node_id <> nt.node_id
      AND e.length_km > 0;
    CREATE INDEX tmp_noded_pgr_source_idx ON tmp_noded_pgr (source);
    CREATE INDEX tmp_noded_pgr_target_idx ON tmp_noded_pgr (target);
    ANALYZE tmp_noded_pgr;

    CREATE TEMP TABLE tmp_noded_components AS
    SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM tmp_noded_pgr');
    CREATE INDEX tmp_noded_components_node_idx ON tmp_noded_components (node);
    CREATE INDEX tmp_noded_components_component_idx ON tmp_noded_components (component);
    ANALYZE tmp_noded_components;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            """
            WITH sizes AS (
                SELECT component, count(*) AS nodes
                FROM tmp_noded_components
                GROUP BY component
            )
            SELECT
                (SELECT count(*) FROM tmp_noded_edges)::bigint AS edges,
                (SELECT count(*) FROM tmp_noded_nodes)::bigint AS nodes,
                count(*)::bigint AS components,
                coalesce(max(nodes), 0)::bigint AS largest_component_nodes
            FROM sizes
            """
        )
        row = cur.fetchone()
    return {
        "noded_edges": int(row[0] or 0),
        "noded_nodes": int(row[1] or 0),
        "noded_components": int(row[2] or 0),
        "noded_largest_nodes": int(row[3] or 0),
    }


def after_stats(conn: psycopg.Connection, iso: str) -> dict[str, int | float]:
    suffix = iso.lower()
    od = f"crop_accessibility_astar_od_{suffix}"
    origins = f"crop_origin_nodes_{suffix}"
    ports = f"port_destination_nodes_{suffix}"
    if not table_exists(conn, "eq", od) or not table_exists(conn, "eq", origins) or not table_exists(conn, "eq", ports):
        return {"reachable": 0, "unreachable": 0}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH od_ports AS (
                SELECT od.od_id, od.crop_code, od.candidate_rank, od.dest_id
                FROM eq.{qident(od)} od
                WHERE od.dest_type = 'port'
            ), snapped_origins AS (
                SELECT DISTINCT ON (o.crop_code, o.candidate_rank)
                       o.crop_code, o.candidate_rank,
                       n.node_id,
                       ST_Distance(o.geometry::geography, n.geometry::geography) AS snap_m
                FROM eq.{qident(origins)} o
                JOIN od_ports od
                  ON od.crop_code = o.crop_code
                 AND od.candidate_rank = o.candidate_rank
                CROSS JOIN LATERAL (
                    SELECT node_id, geometry
                    FROM tmp_noded_nodes n
                    ORDER BY n.geometry <-> o.geometry
                    LIMIT 1
                ) n
                WHERE o.country_code = {qliteral(iso)}
            ), snapped_ports AS (
                SELECT DISTINCT ON (p.port_id)
                       p.port_id,
                       n.node_id,
                       ST_Distance(p.geometry::geography, n.geometry::geography) AS snap_m
                FROM eq.{qident(ports)} p
                JOIN od_ports od ON od.dest_id = p.port_id
                CROSS JOIN LATERAL (
                    SELECT node_id, geometry
                    FROM tmp_noded_nodes n
                    ORDER BY n.geometry <-> p.geometry
                    LIMIT 1
                ) n
            ), joined AS (
                SELECT od.od_id,
                       oc.component AS origin_component,
                       pc.component AS port_component,
                       so.snap_m AS origin_snap_m,
                       sp.snap_m AS port_snap_m
                FROM od_ports od
                JOIN snapped_origins so
                  ON so.crop_code = od.crop_code
                 AND so.candidate_rank = od.candidate_rank
                JOIN snapped_ports sp ON sp.port_id = od.dest_id
                LEFT JOIN tmp_noded_components oc ON oc.node = so.node_id
                LEFT JOIN tmp_noded_components pc ON pc.node = sp.node_id
            )
            SELECT
                count(*)::bigint AS od_total,
                count(*) FILTER (WHERE origin_component = port_component)::bigint AS reachable,
                count(*) FILTER (WHERE origin_component IS NULL OR port_component IS NULL OR origin_component <> port_component)::bigint AS unreachable,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY origin_snap_m)::double precision AS origin_snap_p50_m,
                max(origin_snap_m)::double precision AS origin_snap_max_m,
                max(port_snap_m)::double precision AS port_snap_max_m
            FROM joined
            """
        )
        row = cur.fetchone()
    return {
        "od_total": int(row["od_total"] or 0),
        "reachable": int(row["reachable"] or 0),
        "unreachable": int(row["unreachable"] or 0),
        "origin_snap_p50_m": float(row["origin_snap_p50_m"] or 0.0),
        "origin_snap_max_m": float(row["origin_snap_max_m"] or 0.0),
        "port_snap_max_m": float(row["port_snap_max_m"] or 0.0),
    }


def compare_country(conn: psycopg.Connection, iso: str) -> dict[str, object]:
    t0 = time.time()
    log(f"[start] {iso}")
    before = before_stats(conn, iso)
    graph = build_temp_noded_graph(conn, iso)
    after = after_stats(conn, iso)
    result = {
        "country_code": iso,
        "od_total": before["od_total"],
        "before_reachable": before["reachable"],
        "before_unreachable": before["unreachable"],
        "after_reachable": after["reachable"],
        "after_unreachable": after["unreachable"],
        "delta_reachable": after["reachable"] - before["reachable"],
        **graph,
        "origin_snap_p50_m_after": after.get("origin_snap_p50_m", 0.0),
        "origin_snap_max_m_after": after.get("origin_snap_max_m", 0.0),
        "port_snap_max_m_after": after.get("port_snap_max_m", 0.0),
        "elapsed_s": round(time.time() - t0, 1),
    }
    log(
        f"[done] {iso} port_reachable {result['before_reachable']}/{result['od_total']} -> "
        f"{result['after_reachable']}/{result['od_total']} components={result['noded_components']:,} "
        f"largest={result['noded_largest_nodes']:,} elapsed_s={result['elapsed_s']}"
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare port OD reachability before/after noding road intersections.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="loaded", help="loaded or comma-separated ISO3 list")
    parser.add_argument("--out-csv", default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "port_reachability_noded_compare.csv"))
    parser.add_argument("--out-json", default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "port_reachability_noded_compare.json"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_csv = Path(args.out_csv)
    out_json = Path(args.out_json)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "loaded":
            countries = loaded_countries(conn)
        else:
            countries = [part.strip().upper() for part in args.countries.split(",") if part.strip()]
        log(f"[countries] {','.join(countries)}")
        results: list[dict[str, object]] = []
        for iso in countries:
            try:
                results.append(compare_country(conn, iso))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                log(f"[fail] {iso} {type(exc).__name__}: {exc}")
                results.append({"country_code": iso, "error": f"{type(exc).__name__}: {exc}"})
    out_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    if results:
        keys = sorted({key for row in results for key in row})
        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
    log(f"[written] {out_csv}")
    log(f"[written] {out_json}")


if __name__ == "__main__":
    main()
