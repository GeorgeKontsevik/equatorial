#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import psycopg
from pyproj import Geod

from render_base_routes_by_dest_type import (
    create_route_tables,
    fetch_destinations,
    fetch_origins,
    fetch_pairs,
    fetch_route_usage,
    render_map,
)
from run_weekly_astar_accessibility import (
    DEFAULT_DB_URL,
    qident,
    qliteral,
)


ROOT = Path(__file__).resolve().parents[1]
SNAP_CAP_M = 2500.0
DEST_TYPES = ("port", "city_5_100k", "city_100k_plus")
GEOD = Geod(ellps="WGS84")


def log(message: str) -> None:
    print(message, flush=True)


def available_countries(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'eq'
              AND tablename ~ '^road_graph_edges_pgr_[a-z]{3}$'
            ORDER BY tablename
            """
        )
        countries = []
        for (table,) in cur.fetchall():
            match = re.search(r"_([a-z]{3})$", table)
            if match:
                countries.append(match.group(1).upper())
        return countries


def read_points(conn: psycopg.Connection, sql: str) -> gpd.GeoDataFrame:
    frame = gpd.read_postgis(sql, conn, geom_col="geometry", crs="EPSG:4326")
    if frame.empty:
        return frame
    return frame.set_geometry("geometry").to_crs("EPSG:4326")


def nearest_node_within_cap(
    cur: psycopg.Cursor,
    iso: str,
    lon: float,
    lat: float,
    cache: dict[tuple[float, float], tuple[int | None, float | None]],
) -> tuple[int | None, float | None]:
    key = (round(float(lon), 6), round(float(lat), 6))
    if key in cache:
        return cache[key]
    suffix = iso.lower()
    # Local envelope keeps the KNN query small; exact WGS84 geodesic distance applies the 2.5 km cap.
    pad = 0.08
    cur.execute(
        f"""
        SELECT node_id, lon, lat
        FROM eq.{qident(f"road_graph_nodes_{suffix}")}
        WHERE geometry && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
        ORDER BY geometry <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        LIMIT 32
        """,
        (lon - pad, lat - pad, lon + pad, lat + pad, lon, lat),
    )
    best_node = None
    best_dist = None
    for node_id, node_lon, node_lat in cur.fetchall():
        _, _, dist_m = GEOD.inv(float(lon), float(lat), float(node_lon), float(node_lat))
        if dist_m <= SNAP_CAP_M and (best_dist is None or dist_m < best_dist):
            best_node = int(node_id)
            best_dist = float(dist_m)
    cache[key] = (best_node, best_dist)
    return cache[key]


def snap_to_road_nodes(conn: psycopg.Connection, iso: str, points: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    points = points.copy()
    node_ids: list[int | None] = []
    distances: list[float | None] = []
    cache: dict[tuple[float, float], tuple[int | None, float | None]] = {}
    with conn.cursor() as cur:
        for row in points.itertuples(index=False):
            node_id, dist_m = nearest_node_within_cap(cur, iso, float(row.lon), float(row.lat), cache)
            node_ids.append(node_id)
            distances.append(dist_m)
    points["node_id"] = node_ids
    points["node_distance_m"] = distances
    return points


def create_crop_origin_table(conn: psycopg.Connection, iso: str, rows: gpd.GeoDataFrame) -> dict[str, int | float | None]:
    suffix = iso.lower()
    out = f"crop_origin_nodes_{suffix}"
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(out)} (
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
                geometry geometry(Point, 4326)
            )
            """
        )
        insert_sql = f"""
            INSERT INTO eq.{qident(out)} (
                country_code, crop_code, candidate_rank, harvested_area, lon, lat,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                representative_distance_m, metric_crs, node_id, node_distance_m, geometry
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
        """
        values = []
        for row in rows.itertuples(index=False):
            node_id = None if pd.isna(row.node_id) else int(row.node_id)
            node_distance_m = None if pd.isna(row.node_distance_m) else float(row.node_distance_m)
            values.append(
                (
                    row.country_code,
                    row.crop_code,
                    int(row.candidate_rank),
                    None if pd.isna(row.harvested_area) else float(row.harvested_area),
                    float(row.lon),
                    float(row.lat),
                    None if pd.isna(row.cluster_cell_count) else int(row.cluster_cell_count),
                    None if pd.isna(row.representative_cell_harvested_area) else float(row.representative_cell_harvested_area),
                    None if pd.isna(row.cluster_share) else float(row.cluster_share),
                    None if pd.isna(row.representative_distance_m) else float(row.representative_distance_m),
                    row.metric_crs,
                    node_id,
                    node_distance_m,
                    float(row.lon),
                    float(row.lat),
                )
            )
        if values:
            cur.executemany(insert_sql, values)
        cur.execute(f"ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (country_code, crop_code, candidate_rank)")
        cur.execute(f"CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id)")
        cur.execute(f"CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry)")
        cur.execute(f"ANALYZE eq.{qident(out)}")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*)::int,
                   count(node_id)::int,
                   count(*) FILTER (WHERE node_id IS NULL)::int,
                   max(node_distance_m)::float
            FROM eq.{qident(out)}
            """
        )
        total, snapped, unsnapped, max_m = cur.fetchone()
    return {"total": total, "snapped": snapped, "unsnapped": unsnapped, "max_snap_m": max_m}


def resnap_crop_origins(conn: psycopg.Connection, iso: str) -> dict[str, int | float | None]:
    points = read_points(
        conn,
        f"""
        SELECT country_code, crop_code, candidate_rank, harvested_area, lon, lat,
               cluster_cell_count, representative_cell_harvested_area, cluster_share,
               representative_distance_m, metric_crs, geometry
        FROM eq.crop_origin_candidates
        WHERE country_code = {qliteral(iso)}
        """,
    )
    snapped = snap_to_road_nodes(conn, iso, points)
    return create_crop_origin_table(conn, iso, snapped)


def create_city_node_table(conn: psycopg.Connection, out: str, rows: gpd.GeoDataFrame) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(out)} (
                country_code text,
                geoname_id bigint,
                name text,
                population bigint,
                lon double precision,
                lat double precision,
                node_id bigint,
                node_distance_m double precision,
                geometry geometry(Point, 4326),
                PRIMARY KEY (country_code, geoname_id)
            )
            """
        )
        insert_sql = f"""
            INSERT INTO eq.{qident(out)}
                (country_code, geoname_id, name, population, lon, lat, node_id, node_distance_m, geometry)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
        """
        values = []
        for row in rows.dropna(subset=["node_id"]).itertuples(index=False):
            values.append(
                (
                    row.country_code,
                    int(row.geoname_id),
                    row.name,
                    None if pd.isna(row.population) else int(row.population),
                    float(row.lon),
                    float(row.lat),
                    int(row.node_id),
                    float(row.node_distance_m),
                    float(row.lon),
                    float(row.lat),
                )
            )
        if values:
            cur.executemany(insert_sql, values)
        cur.execute(f"CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id)")
        cur.execute(f"CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry)")
        cur.execute(f"ANALYZE eq.{qident(out)}")
    conn.commit()
    return len(values)


def create_port_node_table(conn: psycopg.Connection, out: str, rows: gpd.GeoDataFrame) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE TABLE eq.{qident(out)} (
                port_id text PRIMARY KEY,
                name text,
                natlscale integer,
                lon double precision,
                lat double precision,
                node_id bigint,
                node_distance_m double precision,
                geometry geometry(Point, 4326)
            )
            """
        )
        insert_sql = f"""
            INSERT INTO eq.{qident(out)}
                (port_id, name, natlscale, lon, lat, node_id, node_distance_m, geometry)
            VALUES (%s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
        """
        values = []
        for row in rows.dropna(subset=["node_id"]).itertuples(index=False):
            values.append(
                (
                    row.port_id,
                    row.name,
                    None if pd.isna(row.natlscale) else int(row.natlscale),
                    float(row.lon),
                    float(row.lat),
                    int(row.node_id),
                    float(row.node_distance_m),
                    float(row.lon),
                    float(row.lat),
                )
            )
        if values:
            cur.executemany(insert_sql, values)
        cur.execute(f"CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id)")
        cur.execute(f"CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry)")
        cur.execute(f"ANALYZE eq.{qident(out)}")
    conn.commit()
    return len(values)


def resnap_destinations(conn: psycopg.Connection, iso: str) -> dict[str, int]:
    suffix = iso.lower()
    from run_weekly_astar_accessibility import country_boundary_wkt

    boundary_wkt = country_boundary_wkt(iso)
    nodes = f"road_graph_nodes_{suffix}"

    def city_sql(source: str, out: str, where: str) -> int:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
            cur.execute(
                f"""
                CREATE TABLE eq.{qident(out)} AS
                SELECT c.country_code, c.geoname_id, c.name, c.population, c.lon, c.lat,
                       n.node_id, n.node_distance_m, c.geometry
                FROM eq.{qident(source)} c
                JOIN LATERAL (
                    SELECT node_id, node_distance_m
                    FROM (
                        SELECT rn.node_id,
                               ST_Distance(c.geometry::geography, rn.geometry::geography) AS node_distance_m
                        FROM eq.{qident(nodes)} rn
                        WHERE rn.geometry && ST_Expand(c.geometry, 0.08)
                        ORDER BY rn.geometry <-> c.geometry
                        LIMIT 32
                    ) candidate
                    WHERE node_distance_m <= {SNAP_CAP_M}
                    ORDER BY node_distance_m
                    LIMIT 1
                ) n ON true
                WHERE {where};
                ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (country_code, geoname_id);
                CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
                CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
                ANALYZE eq.{qident(out)};
                """
            )
            cur.execute(f"SELECT count(*) FROM eq.{qident(out)}")
            rows = int(cur.fetchone()[0])
        conn.commit()
        return rows

    def port_sql(out: str) -> int:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
            cur.execute(
                f"""
                CREATE TABLE eq.{qident(out)} AS
                SELECT p.port_id, p.name, p.natlscale, p.lon, p.lat,
                       n.node_id, n.node_distance_m, p.geometry
                FROM eq.port_destinations p
                JOIN LATERAL (
                    SELECT node_id, node_distance_m
                    FROM (
                        SELECT rn.node_id,
                               ST_Distance(p.geometry::geography, rn.geometry::geography) AS node_distance_m
                        FROM eq.{qident(nodes)} rn
                        WHERE rn.geometry && ST_Expand(p.geometry, 0.08)
                        ORDER BY rn.geometry <-> p.geometry
                        LIMIT 32
                    ) candidate
                    WHERE node_distance_m <= {SNAP_CAP_M}
                    ORDER BY node_distance_m
                    LIMIT 1
                ) n ON true
                WHERE ST_Intersects(p.geometry, ST_GeomFromText({qliteral(boundary_wkt)}, 4326));
                ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (port_id);
                CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
                CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
                ANALYZE eq.{qident(out)};
                """
            )
            cur.execute(f"SELECT count(*) FROM eq.{qident(out)}")
            rows = int(cur.fetchone()[0])
        conn.commit()
        return rows

    return {
        "city_5_100k": city_sql(
            "city_destinations_5k_100k",
            f"city_destination_nodes_5k_100k_{suffix}",
            f"c.country_code = {qliteral(iso)}",
        ),
        "city_100k_plus": city_sql(
            "city_destinations",
            f"city_destination_nodes_100k_plus_{suffix}",
            f"c.country_code = {qliteral(iso)} AND c.population >= 100000",
        ),
        "port": port_sql(f"port_destination_nodes_{suffix}"),
    }


def table_row_count(conn: psycopg.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"eq.{table}",))
        if cur.fetchone()[0] is None:
            return 0
        cur.execute(f"SELECT count(*) FROM eq.{qident(table)}")
        return int(cur.fetchone()[0])


def ensure_base_astar_table(conn: psycopg.Connection, iso: str, force: bool) -> str:
    suffix = iso.lower()
    out = f"road_graph_edges_pgr_{suffix}_astar_base"
    if not force and table_row_count(conn, out) > 0:
        log(f"[skip] {iso} base astar rows={table_row_count(conn, out):,}")
        return out
    pgr = f"road_graph_edges_pgr_{suffix}"
    nodes = f"road_graph_nodes_{suffix}"
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE UNLOGGED TABLE eq.{qident(out)} AS
            SELECT e.id, e.source, e.target, e.surface_group,
                   e.cost AS base_cost,
                   e.reverse_cost AS base_reverse_cost,
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
    log(f"[base] {iso} astar rows={table_row_count(conn, out):,}")
    return out


def build_base_od(conn: psycopg.Connection, iso: str, top_per_crop: int = 20) -> tuple[str, int]:
    suffix = iso.lower()
    out = f"crop_accessibility_astar_od_{suffix}"
    origin_nodes = f"crop_origin_nodes_{suffix}"
    components = f"road_graph_components_{suffix}"
    city_small_nodes = f"city_destination_nodes_5k_100k_{suffix}"
    city_large_nodes = f"city_destination_nodes_100k_plus_{suffix}"
    port_nodes = f"port_destination_nodes_{suffix}"
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(out)}")
        cur.execute(
            f"""
            CREATE UNLOGGED TABLE eq.{qident(out)} AS
            WITH ranked_origins AS (
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                       o.node_id AS origin_node, oc.component, o.geometry,
                       row_number() OVER (
                           PARTITION BY o.crop_code
                           ORDER BY o.harvested_area DESC NULLS LAST, o.candidate_rank
                       ) AS crop_rank
                FROM eq.{qident(origin_nodes)} o
                JOIN eq.{qident(components)} oc ON oc.node = o.node_id
                WHERE o.country_code = {qliteral(iso)} AND o.node_id IS NOT NULL
            ), origins AS (
                SELECT *
                FROM ranked_origins
                WHERE crop_rank <= {int(top_per_crop)}
            ), city_small_od AS (
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                       'city_5_100k'::text AS dest_type, c.rank::integer AS dest_rank,
                       c.geoname_id::text AS dest_id, c.name AS dest_name, c.population,
                       o.origin_node, c.node_id AS dest_node,
                       ST_Distance(o.geometry::geography, c.geometry::geography) / 1000.0 AS straight_dist_km
                FROM origins o
                CROSS JOIN LATERAL (
                    SELECT c.geoname_id, c.name, c.population, c.node_id, c.geometry,
                           row_number() OVER (ORDER BY o.geometry <-> c.geometry) AS rank
                    FROM eq.{qident(city_small_nodes)} c
                    ORDER BY o.geometry <-> c.geometry
                    LIMIT 3
                ) c
            ), city_large_od AS (
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                       'city_100k_plus'::text AS dest_type, c.rank::integer AS dest_rank,
                       c.geoname_id::text AS dest_id, c.name AS dest_name, c.population,
                       o.origin_node, c.node_id AS dest_node,
                       ST_Distance(o.geometry::geography, c.geometry::geography) / 1000.0 AS straight_dist_km
                FROM origins o
                CROSS JOIN LATERAL (
                    SELECT c.geoname_id, c.name, c.population, c.node_id, c.geometry,
                           row_number() OVER (ORDER BY o.geometry <-> c.geometry) AS rank
                    FROM eq.{qident(city_large_nodes)} c
                    ORDER BY o.geometry <-> c.geometry
                    LIMIT 3
                ) c
            ), port_od AS (
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                       'port'::text AS dest_type, p.rank::integer AS dest_rank,
                       p.port_id::text AS dest_id, p.name AS dest_name, NULL::bigint AS population,
                       o.origin_node, p.node_id AS dest_node,
                       ST_Distance(o.geometry::geography, p.geometry::geography) / 1000.0 AS straight_dist_km
                FROM origins o
                CROSS JOIN LATERAL (
                    SELECT p.port_id, p.name, p.node_id, p.geometry,
                           row_number() OVER (ORDER BY o.geometry <-> p.geometry) AS rank
                    FROM eq.{qident(port_nodes)} p
                    ORDER BY o.geometry <-> p.geometry
                    LIMIT 3
                ) p
            )
            SELECT row_number() OVER () AS od_id, *
            FROM (
                SELECT * FROM city_small_od
                UNION ALL
                SELECT * FROM city_large_od
                UNION ALL
                SELECT * FROM port_od
            ) q;
            ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (od_id);
            CREATE INDEX {out}_pair_idx ON eq.{qident(out)} (origin_node, dest_node);
            ANALYZE eq.{qident(out)};
            """
        )
        cur.execute(f"SELECT count(*) FROM eq.{qident(out)}")
        rows = int(cur.fetchone()[0])
    conn.commit()
    return out, rows


def render_country(conn: psycopg.Connection, iso: str, out_dir: Path, force_bridge: bool, force_cache: bool) -> list[dict[str, object]]:
    snap = resnap_crop_origins(conn, iso)
    log(
        f"[snap] {iso} crops snapped={snap['snapped']}/{snap['total']} "
        f"unsnapped={snap['unsnapped']} max_snap_m={snap['max_snap_m']}"
    )
    dest_counts = resnap_destinations(conn, iso)
    log(f"[snap] {iso} destinations {dest_counts}")
    ensure_base_astar_table(conn, iso, force=force_cache)
    od_table, od_rows = build_base_od(conn, iso, top_per_crop=20)
    log(f"[od] {iso} table={od_table} rows={od_rows}")

    results = []
    country_dir = out_dir / iso
    for dest_type in DEST_TYPES:
        log(f"[routes] {iso} dest_type={dest_type}")
        create_route_tables(conn, iso, dest_type, graph_mode="base")
        route_usage = fetch_route_usage(conn)
        pairs = fetch_pairs(conn, iso)
        origins = fetch_origins(conn, iso)
        destinations = fetch_destinations(conn, iso)
        result = render_map(
            iso,
            dest_type,
            route_usage,
            pairs,
            origins,
            destinations,
            country_dir / f"{iso}_base_routes_to_{dest_type}.png",
        )
        results.append(result)
        log(f"[done] {iso} {dest_type} reachable={result['reachable_od_rows']}/{result['od_rows']}")
    country_dir.mkdir(parents=True, exist_ok=True)
    (country_dir / f"{iso}_base_routes_by_dest_type_manifest.json").write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render base route maps for countries with 2.5 km crop/destination snap cap.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="all")
    parser.add_argument("--out-dir", default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "base_routes_2p5km_snap_cap_all"))
    parser.add_argument("--force-bridge", action="store_true")
    parser.add_argument("--force-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "all":
            countries = available_countries(conn)
        else:
            countries = [part.strip().upper() for part in args.countries.split(",") if part.strip()]
        log(f"[queue] countries={','.join(countries)}")
        for iso in countries:
            try:
                render_country(conn, iso, out_dir, args.force_bridge, args.force_cache)
            except Exception as exc:
                conn.rollback()
                log(f"[error] {iso} {type(exc).__name__}: {exc}")
        total_png = len(list(out_dir.glob("*/*.png")))
        log(f"[complete] png={total_png} out_dir={out_dir}")


if __name__ == "__main__":
    main()
