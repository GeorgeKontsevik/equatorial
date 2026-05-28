#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SPEED_KMH = 30.0


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def qident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return '"' + name + '"'


def qliteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def short_graph_tag(graph_prefix: str) -> str:
    if graph_prefix == "road_graph":
        return "rg"
    if graph_prefix == "component_connected":
        return "cc"
    if graph_prefix == "cluster_connected":
        return "clc"
    return re.sub(r"[^A-Za-z0-9_]", "_", graph_prefix)[:12]


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> int | float | str | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    return bool(scalar(conn, "SELECT to_regclass(%s)", (f"{schema}.{table}",)))


def crop_origin_node_table(conn: psycopg.Connection, iso: str, graph_prefix: str) -> str:
    suffix = iso.lower()
    prefixed = f"{graph_prefix}_crop_origin_nodes_{suffix}"
    if graph_prefix != "road_graph" and table_exists(conn, "eq", prefixed):
        return prefixed
    return f"crop_origin_nodes_{suffix}"


def run_sql(conn: psycopg.Connection, label: str, sql: str) -> float:
    log(f"start {label}")
    t0 = time.monotonic()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    elapsed = time.monotonic() - t0
    log(f"done {label} elapsed_s={elapsed:.1f}")
    return elapsed


def country_boundary_wkt(iso: str) -> str:
    path = ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg"
    if not path.exists():
        raise FileNotFoundError(f"Missing GADM boundary for {iso}: {path}")
    frame = gpd.read_file(path, layer="ADM_ADM_0").to_crs("EPSG:4326")
    return frame.geometry.union_all().wkt


@dataclass
class HeartbeatState:
    country: str = "-"
    stage: str = "init"
    week: str = "-"
    week_idx: int = 0
    week_count: int = 0
    done_weeks: int = 0
    last_rows: int = 0
    stage_started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def update(self, **kwargs: object) -> None:
        with self.lock:
            reset = bool(kwargs.pop("reset_timer", False))
            for key, value in kwargs.items():
                setattr(self, key, value)
            if reset:
                self.stage_started = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "country": self.country,
                "stage": self.stage,
                "week": self.week,
                "week_idx": self.week_idx,
                "week_count": self.week_count,
                "done_weeks": self.done_weeks,
                "last_rows": self.last_rows,
                "elapsed": time.monotonic() - self.stage_started,
            }


class Heartbeat:
    def __init__(self, state: HeartbeatState, interval_s: int) -> None:
        self.state = state
        self.interval_s = interval_s
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            snap = self.state.snapshot()
            log(
                "heartbeat "
                f"country={snap['country']} stage={snap['stage']} "
                f"week={snap['week']} {snap['week_idx']}/{snap['week_count']} "
                f"done_weeks={snap['done_weeks']} last_rows={snap['last_rows']} "
                f"stage_elapsed_s={snap['elapsed']:.0f}"
            )


def ensure_penalty_rules(conn: psycopg.Connection) -> None:
    run_sql(
        conn,
        "weekly rain penalty rules",
        """
        CREATE TABLE IF NOT EXISTS eq.weekly_rain_speed_penalty_rules (
            road_type text NOT NULL,
            min_weekly_mm double precision NOT NULL,
            max_weekly_mm double precision,
            speed_multiplier double precision NOT NULL,
            effect_label text NOT NULL,
            effectively_closed boolean NOT NULL DEFAULT false,
            PRIMARY KEY (road_type, min_weekly_mm)
        );
        TRUNCATE eq.weekly_rain_speed_penalty_rules;
        INSERT INTO eq.weekly_rain_speed_penalty_rules
            (road_type, min_weekly_mm, max_weekly_mm, speed_multiplier, effect_label, effectively_closed)
        VALUES
            ('paved', 0, 50, 1.00, 'no penalty', false),
            ('paved', 50, 100, 0.90, 'minor speed reduction', false),
            ('paved', 100, 200, 0.75, 'slowed', false),
            ('paved', 200, 300, 0.40, 'flood/damage risk', false),
            ('paved', 300, NULL, 0.05, 'effectively closed', true),
            ('unpaved', 0, 50, 1.00, 'no penalty', false),
            ('unpaved', 50, 100, 0.70, 'slowed', false),
            ('unpaved', 100, 150, 0.45, 'strongly slowed', false),
            ('unpaved', 150, 250, 0.20, 'severe degradation / low reliability', false),
            ('unpaved', 250, NULL, 0.05, 'effectively closed', true);
        """,
    )


def ensure_results_table(conn: psycopg.Connection) -> None:
    run_sql(
        conn,
        "results table",
        """
        CREATE TABLE IF NOT EXISTS eq.crop_accessibility_weekly_astar (
            run_at timestamptz NOT NULL DEFAULT now(),
            country_code text NOT NULL,
            week_start date NOT NULL,
            scenario text NOT NULL,
            origin_scope text NOT NULL,
            crop_code text NOT NULL,
            candidate_rank integer NOT NULL,
            crop_rank integer NOT NULL,
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
            travel_time_h double precision,
            route_status text NOT NULL,
            PRIMARY KEY (
                country_code, week_start, scenario, origin_scope,
                crop_code, candidate_rank, dest_type, dest_rank, dest_id
            )
        );
        CREATE INDEX IF NOT EXISTS crop_accessibility_weekly_astar_country_week_idx
            ON eq.crop_accessibility_weekly_astar (country_code, week_start);
        CREATE INDEX IF NOT EXISTS crop_accessibility_weekly_astar_dest_idx
            ON eq.crop_accessibility_weekly_astar (country_code, dest_type, dest_id);
        ALTER TABLE eq.crop_accessibility_weekly_astar
            ADD COLUMN IF NOT EXISTS cluster_cell_count integer,
            ADD COLUMN IF NOT EXISTS representative_cell_harvested_area double precision,
            ADD COLUMN IF NOT EXISTS cluster_share double precision;
        """,
    )


def country_queue(conn: psycopg.Connection, requested: str) -> list[str]:
    if requested.lower() != "auto":
        return [x.strip().upper() for x in requested.split(",") if x.strip()]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH edges AS (
                SELECT upper(substring(relname from '^road_graph_edges_pgr_([a-z]{3})$')) iso,
                       reltuples::bigint edges
                FROM pg_class
                WHERE relnamespace = 'eq'::regnamespace
                  AND relname ~ '^road_graph_edges_pgr_[a-z]{3}$'
            ), crops AS (
                SELECT upper(substring(relname from '^crop_origin_nodes_([a-z]{3})$')) iso,
                       reltuples::bigint crop_nodes
                FROM pg_class
                WHERE relnamespace = 'eq'::regnamespace
                  AND relname ~ '^crop_origin_nodes_[a-z]{3}$'
            ), weeks AS (
                SELECT country_code iso, count(DISTINCT week_start) weeks
                FROM eq.era5_precip_weekly_grid
                GROUP BY country_code
            )
            SELECT e.iso
            FROM edges e
            JOIN crops c USING (iso)
            JOIN weeks w USING (iso)
            WHERE c.crop_nodes > 0 AND w.weeks = 53
            ORDER BY e.edges ASC
            """
        )
        return [row[0] for row in cur.fetchall()]


def ensure_city_nodes(conn: psycopg.Connection, iso: str, force: bool) -> None:
    suffix = iso.lower()
    nodes = f"road_graph_nodes_{suffix}"
    out = f"city_destination_nodes_5k_100k_{suffix}"
    if not force and table_exists(conn, "eq", out):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}")
        log(f"skip {iso} city nodes rows={int(rows or 0):,}")
        return
    run_sql(
        conn,
        f"{iso} snap 5k-100k cities",
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        SELECT c.country_code, c.geoname_id, c.name, c.population, c.lon, c.lat,
               n.node_id,
               ST_Distance(c.geometry::geography, n.geometry::geography) AS node_distance_m,
               c.geometry
        FROM eq.city_destinations_5k_100k c
        CROSS JOIN LATERAL (
            SELECT node_id, geometry
            FROM eq.{qident(nodes)} n
            ORDER BY n.geometry <-> c.geometry
            LIMIT 1
        ) n
        WHERE c.country_code = {qliteral(iso)};
        ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (country_code, geoname_id);
        CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
        CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
        ANALYZE eq.{qident(out)};
        """,
    )


def ensure_large_city_nodes(conn: psycopg.Connection, iso: str, force: bool) -> None:
    suffix = iso.lower()
    nodes = f"road_graph_nodes_{suffix}"
    out = f"city_destination_nodes_100k_plus_{suffix}"
    if not force and table_exists(conn, "eq", out):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}")
        log(f"skip {iso} 100k+ city nodes rows={int(rows or 0):,}")
        return
    run_sql(
        conn,
        f"{iso} snap 100k+ cities",
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        SELECT c.country_code, c.geoname_id, c.name, c.population, c.lon, c.lat,
               n.node_id,
               ST_Distance(c.geometry::geography, n.geometry::geography) AS node_distance_m,
               c.geometry
        FROM eq.city_destinations c
        CROSS JOIN LATERAL (
            SELECT node_id, geometry
            FROM eq.{qident(nodes)} n
            ORDER BY n.geometry <-> c.geometry
            LIMIT 1
        ) n
        WHERE c.country_code = {qliteral(iso)}
          AND c.population >= 100000;
        ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (country_code, geoname_id);
        CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
        CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
        ANALYZE eq.{qident(out)};
        """,
    )


def ensure_airport_destinations(conn: psycopg.Connection, force: bool) -> None:
    if not force and table_exists(conn, "eq", "airport_destinations"):
        rows = scalar(conn, "SELECT count(*) FROM eq.airport_destinations")
        log(f"skip airport destinations rows={int(rows or 0):,}")
        return
    csv_path = ROOT / "data/raw/airports/ourairports_airports.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing airports CSV: {csv_path}")
    run_sql(
        conn,
        "airport destinations schema",
        """
        DROP TABLE IF EXISTS eq.airport_destinations;
        CREATE TABLE eq.airport_destinations (
            airport_id bigint PRIMARY KEY,
            ident text,
            airport_type text NOT NULL,
            name text NOT NULL,
            iso_country text,
            municipality text,
            scheduled_service text,
            iata_code text,
            lon double precision NOT NULL,
            lat double precision NOT NULL,
            geometry geometry(Point, 4326) NOT NULL
        );
        """,
    )
    rows: list[tuple] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            airport_type = (row.get("type") or "").strip()
            if airport_type not in {"large_airport", "medium_airport", "small_airport"}:
                continue
            try:
                lat = float(row.get("latitude_deg") or "")
                lon = float(row.get("longitude_deg") or "")
            except ValueError:
                continue
            rows.append(
                (
                    int(row["id"]),
                    row.get("ident") or None,
                    airport_type,
                    row.get("name") or "",
                    row.get("iso_country") or None,
                    row.get("municipality") or None,
                    row.get("scheduled_service") or None,
                    row.get("iata_code") or None,
                    lon,
                    lat,
                )
            )
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO eq.airport_destinations (
                airport_id, ident, airport_type, name, iso_country, municipality,
                scheduled_service, iata_code, lon, lat, geometry
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
            """,
            [(*row, row[8], row[9]) for row in rows],
        )
    conn.commit()
    run_sql(
        conn,
        "airport destinations indexes",
        """
        CREATE INDEX airport_destinations_geom_idx ON eq.airport_destinations USING GIST (geometry);
        CREATE INDEX airport_destinations_iso_idx ON eq.airport_destinations (iso_country);
        ANALYZE eq.airport_destinations;
        """,
    )
    log(f"airport destinations loaded rows={len(rows):,}")


def ensure_airport_nodes(conn: psycopg.Connection, iso: str, force: bool) -> None:
    suffix = iso.lower()
    nodes = f"road_graph_nodes_{suffix}"
    out = f"airport_destination_nodes_{suffix}"
    if not force and table_exists(conn, "eq", out):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}")
        log(f"skip {iso} airport nodes rows={int(rows or 0):,}")
        return
    boundary_wkt = country_boundary_wkt(iso)
    run_sql(
        conn,
        f"{iso} snap in-country airports",
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        SELECT a.airport_id, a.ident, a.airport_type, a.name, a.iso_country,
               a.municipality, a.scheduled_service, a.iata_code, a.lon, a.lat,
               n.node_id,
               ST_Distance(a.geometry::geography, n.geometry::geography) AS node_distance_m,
               a.geometry
        FROM eq.airport_destinations a
        CROSS JOIN LATERAL (
            SELECT node_id, geometry
            FROM eq.{qident(nodes)} n
            ORDER BY n.geometry <-> a.geometry
            LIMIT 1
        ) n
        WHERE ST_Intersects(a.geometry, ST_GeomFromText({qliteral(boundary_wkt)}, 4326));
        ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (airport_id);
        CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
        CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
        ANALYZE eq.{qident(out)};
        """,
    )
    rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}") or 0)
    log(f"{iso} in-country airport nodes rows={rows:,}")


def ensure_port_nodes(conn: psycopg.Connection, iso: str, force: bool) -> None:
    suffix = iso.lower()
    nodes = f"road_graph_nodes_{suffix}"
    out = f"port_destination_nodes_{suffix}"
    if not force and table_exists(conn, "eq", out):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}")
        log(f"skip {iso} port nodes rows={int(rows or 0):,}")
        return
    boundary_wkt = country_boundary_wkt(iso)
    run_sql(
        conn,
        f"{iso} snap in-country ports",
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        SELECT p.port_id, p.name, p.natlscale, p.lon, p.lat,
               n.node_id,
               ST_Distance(p.geometry::geography, n.geometry::geography) AS node_distance_m,
               p.geometry
        FROM eq.port_destinations p
        CROSS JOIN LATERAL (
            SELECT node_id, geometry
            FROM eq.{qident(nodes)} n
            ORDER BY n.geometry <-> p.geometry
            LIMIT 1
        ) n
        WHERE ST_Intersects(p.geometry, ST_GeomFromText({qliteral(boundary_wkt)}, 4326));
        ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (port_id);
        CREATE INDEX {out}_node_idx ON eq.{qident(out)} (node_id);
        CREATE INDEX {out}_geom_idx ON eq.{qident(out)} USING GIST (geometry);
        ANALYZE eq.{qident(out)};
        """,
    )
    rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}") or 0)
    log(f"{iso} in-country port nodes rows={rows:,}")


def ensure_bridge(conn: psycopg.Connection, iso: str, force: bool, connector_speed_kmh: float, max_connector_km: float) -> None:
    suffix = iso.lower()
    bridge = f"road_graph_edges_pgr_{suffix}_bridge"
    bridge_components = f"road_graph_components_{suffix}_bridge"
    if not force and table_exists(conn, "eq", bridge) and table_exists(conn, "eq", bridge_components):
        rows = scalar(conn, f"SELECT reltuples::bigint FROM pg_class WHERE oid = 'eq.{bridge}'::regclass")
        log(f"skip {iso} bridge approx_edges={int(rows or 0):,}")
        return

    pgr = f"road_graph_edges_pgr_{suffix}"
    nodes = f"road_graph_nodes_{suffix}"
    components = f"road_graph_components_{suffix}"
    origin_nodes = crop_origin_node_table(conn, iso, graph_prefix)
    city_small_nodes = f"city_destination_nodes_5k_100k_{suffix}"
    city_large_nodes = f"city_destination_nodes_100k_plus_{suffix}"
    port_nodes = f"port_destination_nodes_{suffix}"
    airport_nodes = f"airport_destination_nodes_{suffix}"
    connectors = f"road_graph_connectors_{suffix}"
    run_sql(
        conn,
        f"{iso} bridge graph",
        f"""
        DROP TABLE IF EXISTS eq.{qident(connectors)};
        CREATE UNLOGGED TABLE eq.{qident(connectors)} AS
        WITH terminal_nodes AS (
            SELECT DISTINCT o.node_id, n.geometry, 'crop_origin'::text AS terminal_type
            FROM eq.{qident(origin_nodes)} o
            JOIN eq.{qident(nodes)} n ON n.node_id = o.node_id
            WHERE o.country_code = {qliteral(iso)}
              AND o.node_id IS NOT NULL
            UNION
            SELECT DISTINCT c.node_id, n.geometry, 'city_5_100k'::text AS terminal_type
            FROM eq.{qident(city_small_nodes)} c
            JOIN eq.{qident(nodes)} n ON n.node_id = c.node_id
            WHERE c.country_code = {qliteral(iso)}
            UNION
            SELECT DISTINCT c.node_id, n.geometry, 'city_100k_plus'::text AS terminal_type
            FROM eq.{qident(city_large_nodes)} c
            JOIN eq.{qident(nodes)} n ON n.node_id = c.node_id
            WHERE c.country_code = {qliteral(iso)}
            UNION
            SELECT DISTINCT p.node_id, n.geometry, 'port'::text AS terminal_type
            FROM eq.{qident(port_nodes)} p
            JOIN eq.{qident(nodes)} n ON n.node_id = p.node_id
            UNION
            SELECT DISTINCT a.node_id, n.geometry, 'airport'::text AS terminal_type
            FROM eq.{qident(airport_nodes)} a
            JOIN eq.{qident(nodes)} n ON n.node_id = a.node_id
        ), terminal_component_nodes AS (
            SELECT DISTINCT cc.component, t.node_id, t.geometry
            FROM terminal_nodes t
            JOIN eq.{qident(components)} cc ON cc.node = t.node_id
        ), terminal_components AS (
            SELECT component,
                   count(*) AS terminal_nodes,
                   row_number() OVER (ORDER BY count(*) DESC, component) AS component_rank
            FROM terminal_component_nodes
            GROUP BY component
        ), component_links AS (
            SELECT DISTINCT ON (src.component)
                   src.component AS source_component,
                   src.node_id AS source,
                   target.node_id AS target,
                   ST_Distance(src.geometry::geography, target.geometry::geography) AS length_m
            FROM terminal_component_nodes src
            JOIN terminal_components src_comp ON src_comp.component = src.component
            CROSS JOIN LATERAL (
                SELECT dst.node_id, dst.geometry
                FROM terminal_component_nodes dst
                JOIN terminal_components dst_comp ON dst_comp.component = dst.component
                WHERE dst_comp.component_rank < src_comp.component_rank
                ORDER BY src.geometry <-> dst.geometry
                LIMIT 1
            ) target
            WHERE src_comp.component_rank > 1
            ORDER BY src.component, ST_Distance(src.geometry::geography, target.geometry::geography), src.node_id, target.node_id
        )
        SELECT row_number() OVER ()::bigint AS connector_id,
               {qliteral(iso)}::text AS country_code,
               source, target, source_component,
               length_m / 1000.0 AS length_km,
               {float(connector_speed_kmh)}::double precision AS base_speed_kmh,
               (length_m / 1000.0) / {float(connector_speed_kmh)}::double precision AS cost,
               'terminal_component_spanning_connector'::text AS connector_type
        FROM component_links
        WHERE length_m > 0
          AND length_m <= {float(max_connector_km) * 1000.0};
        ALTER TABLE eq.{qident(connectors)} ADD PRIMARY KEY (connector_id);
        CREATE INDEX {connectors}_source_idx ON eq.{qident(connectors)} (source);
        CREATE INDEX {connectors}_target_idx ON eq.{qident(connectors)} (target);
        ANALYZE eq.{qident(connectors)};

        DROP TABLE IF EXISTS eq.{qident(bridge)};
        CREATE UNLOGGED TABLE eq.{qident(bridge)} AS
        SELECT id, source, target, road_row_id, part_id, highway, surface_group,
               base_speed_kmh::double precision AS base_speed_kmh,
               length_km, cost, reverse_cost
        FROM eq.{qident(pgr)}
        UNION ALL
        SELECT (SELECT coalesce(max(id), 0) FROM eq.{qident(pgr)}) + connector_id AS id,
               source, target,
               NULL::bigint AS road_row_id,
               NULL::integer AS part_id,
               'unpaved_synthetic'::text AS highway,
               'unpaved_synthetic'::text AS surface_group,
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
        CREATE UNLOGGED TABLE eq.{qident(bridge_components)} AS
        SELECT * FROM pgr_connectedComponents(
            'SELECT id, source, target, cost, reverse_cost FROM eq.{bridge}'
        );
        CREATE INDEX {bridge_components}_node_idx ON eq.{qident(bridge_components)} (node);
        CREATE INDEX {bridge_components}_component_idx ON eq.{qident(bridge_components)} (component);
        ANALYZE eq.{qident(bridge_components)};
        """,
    )
    connectors_count = scalar(conn, f"SELECT count(*) FROM eq.{qident(connectors)}")
    log(f"{iso} bridge connectors={int(connectors_count or 0):,} max_connector_km={max_connector_km:g}")


def ensure_astar_base(conn: psycopg.Connection, iso: str, force: bool, graph_prefix: str = "road_graph") -> str:
    suffix = iso.lower()
    if graph_prefix == "road_graph":
        edge_table = f"road_graph_edges_pgr_{suffix}_bridge"
        nodes = f"road_graph_nodes_{suffix}"
        out = f"road_graph_edges_pgr_{suffix}_bridge_astar_base"
    else:
        edge_table = f"{graph_prefix}_edges_pgr_{suffix}"
        nodes = f"{graph_prefix}_nodes_{suffix}"
        out = f"{graph_prefix}_edges_pgr_{suffix}_astar_base"
    if not force and table_exists(conn, "eq", out):
        rows = scalar(conn, f"SELECT reltuples::bigint FROM pg_class WHERE oid = 'eq.{out}'::regclass")
        log(f"skip {iso} astar base approx_edges={int(rows or 0):,}")
        return out
    run_sql(
        conn,
        f"{iso} astar edge base",
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        SELECT e.id, e.source, e.target, e.surface_group,
               e.cost AS base_cost,
               e.reverse_cost AS base_reverse_cost,
               m.cell_id,
               ns.lon AS x1, ns.lat AS y1,
               nt.lon AS x2, nt.lat AS y2
        FROM eq.{qident(edge_table)} e
        JOIN eq.{qident(nodes)} ns ON ns.node_id = e.source
        JOIN eq.{qident(nodes)} nt ON nt.node_id = e.target
        LEFT JOIN eq.road_era5_cell_map m
          ON m.country_code = {qliteral(iso)}
         AND m.road_row_id = e.road_row_id
        WHERE e.cost IS NOT NULL AND e.cost > 0
          AND e.reverse_cost IS NOT NULL AND e.reverse_cost > 0;
        ALTER TABLE eq.{qident(out)} ADD PRIMARY KEY (id);
        CREATE INDEX {out}_cell_idx ON eq.{qident(out)} (cell_id);
        CREATE INDEX {out}_source_idx ON eq.{qident(out)} (source);
        CREATE INDEX {out}_target_idx ON eq.{qident(out)} (target);
        ANALYZE eq.{qident(out)};
        """,
    )
    return out


def build_od(
    conn: psycopg.Connection,
    iso: str,
    top_per_crop: int,
    force: bool,
    graph_prefix: str = "road_graph",
    small_city_limit: int = 3,
    port_limit: int = 3,
    large_city_limit: int = 3,
    airport_limit: int = 0,
) -> tuple[str, int]:
    suffix = iso.lower()
    limit_tag = f"{small_city_limit}s_{large_city_limit}l_{port_limit}p"
    if airport_limit > 0:
        limit_tag += f"_{airport_limit}a"
    origin_tag = "allclusters" if top_per_crop <= 0 else f"top{top_per_crop}"
    out = f"crop_access_astar_od_{short_graph_tag(graph_prefix)}_{suffix}_{limit_tag}"
    if top_per_crop <= 0:
        out += "_allclusters"
    if not force and table_exists(conn, "eq", out):
        rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}") or 0)
        log(f"skip {iso} od rows={rows:,}")
        return out, rows

    origin_nodes = crop_origin_node_table(conn, iso, graph_prefix)
    components = f"road_graph_components_{suffix}" if graph_prefix == "road_graph" else f"{graph_prefix}_components_{suffix}"
    city_small_nodes = f"city_destination_nodes_5k_100k_{suffix}"
    city_large_nodes = f"city_destination_nodes_100k_plus_{suffix}"
    port_nodes = f"port_destination_nodes_{suffix}"
    airport_nodes = f"airport_destination_nodes_{suffix}"
    run_sql(
        conn,
        (
            f"{iso} build OD {origin_tag}/crop to "
            f"{small_city_limit} small cities, {port_limit} ports, "
            f"{large_city_limit} large cities, {airport_limit} airports"
        ),
        f"""
        DROP TABLE IF EXISTS eq.{qident(out)};
        CREATE UNLOGGED TABLE eq.{qident(out)} AS
        WITH ranked_origins AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                   o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
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
            WHERE {("true" if top_per_crop <= 0 else f"crop_rank <= {int(top_per_crop)}")}
        ), city_small_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                   o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
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
                LIMIT {int(small_city_limit)}
            ) c
        ), city_large_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                   o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
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
                LIMIT {int(large_city_limit)}
            ) c
        ), port_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                   o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
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
                LIMIT {int(port_limit)}
            ) p
        ), airport_od AS (
            SELECT o.country_code, o.crop_code, o.candidate_rank, o.crop_rank, o.harvested_area,
                   o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
                   'airport'::text AS dest_type, a.rank::integer AS dest_rank,
                   a.airport_id::text AS dest_id, a.name AS dest_name, NULL::bigint AS population,
                   o.origin_node, a.node_id AS dest_node,
                   ST_Distance(o.geometry::geography, a.geometry::geography) / 1000.0 AS straight_dist_km
            FROM origins o
            CROSS JOIN LATERAL (
                SELECT a.airport_id, a.name, a.node_id, a.geometry,
                       row_number() OVER (ORDER BY o.geometry <-> a.geometry) AS rank
                FROM eq.{qident(airport_nodes)} a
                ORDER BY o.geometry <-> a.geometry
                LIMIT {int(airport_limit)}
            ) a
        )
        SELECT row_number() OVER () AS od_id, *
        FROM (
            SELECT * FROM city_small_od
            UNION ALL
            SELECT * FROM city_large_od
            UNION ALL
            SELECT * FROM port_od
            UNION ALL
            SELECT * FROM airport_od
        ) q;
        ALTER TABLE eq.{qident(out)} ADD CONSTRAINT {qident(out + '_pk')} PRIMARY KEY (od_id);
        CREATE INDEX {out}_pair_idx ON eq.{qident(out)} (origin_node, dest_node);
        ANALYZE eq.{qident(out)};
        """,
    )
    rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(out)}") or 0)
    return out, rows


def weeks_for_country(conn: psycopg.Connection, iso: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT week_start::text
            FROM eq.era5_precip_weekly_grid
            WHERE country_code = %s
            GROUP BY week_start
            ORDER BY week_start
            """,
            (iso,),
        )
        return [row[0] for row in cur.fetchall()]


def run_week(
    conn: psycopg.Connection,
    iso: str,
    week_start: str,
    scenario: str,
    origin_scope: str,
    astar_base: str,
    od_table: str,
    replace: bool,
) -> int:
    if not replace:
        existing = scalar(
            conn,
            """
            SELECT count(*) FROM eq.crop_accessibility_weekly_astar
            WHERE country_code = %s AND week_start = %s AND scenario = %s AND origin_scope = %s
            """,
            (iso, week_start, scenario, origin_scope),
        )
        if int(existing or 0) > 0:
            return int(existing or 0)

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM eq.crop_accessibility_weekly_astar
            WHERE country_code = %s AND week_start = %s AND scenario = %s AND origin_scope = %s
            """,
            (iso, week_start, scenario, origin_scope),
        )
    conn.commit()

    edge_sql = f"""
        SELECT e.id, e.source, e.target,
               e.base_cost / GREATEST(
                   CASE
                       WHEN e.surface_group = 'synthetic_connector' THEN 1.0
                       ELSE p.speed_multiplier
                   END,
                   0.01
               ) AS cost,
               e.base_reverse_cost / GREATEST(
                   CASE
                       WHEN e.surface_group = 'synthetic_connector' THEN 1.0
                       ELSE p.speed_multiplier
                   END,
                   0.01
               ) AS reverse_cost,
               e.x1, e.y1, e.x2, e.y2
        FROM eq.{astar_base} e
        LEFT JOIN eq.era5_precip_weekly_grid g
          ON g.country_code = {qliteral(iso)}
         AND g.week_start = DATE {qliteral(week_start)}
         AND g.cell_id = e.cell_id
        JOIN LATERAL (
            SELECT speed_multiplier
            FROM eq.weekly_rain_speed_penalty_rules p
            WHERE p.road_type = CASE WHEN e.surface_group = 'paved' THEN 'paved' ELSE 'unpaved' END
              AND COALESCE(g.tp_sum_weekly_mm, 0) >= p.min_weekly_mm
              AND (p.max_weekly_mm IS NULL OR COALESCE(g.tp_sum_weekly_mm, 0) < p.max_weekly_mm)
            ORDER BY p.min_weekly_mm DESC
            LIMIT 1
        ) p ON true
    """.replace("\n", " ")
    combinations_sql = f"SELECT origin_node AS source, dest_node AS target FROM eq.{od_table}"

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_weekly_astar_costs")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_weekly_astar_costs ON COMMIT DROP AS
            SELECT * FROM pgr_aStarCost(%s, %s, false, 5, 1.0, 1.0)
            """,
            (edge_sql, combinations_sql),
        )
        cur.execute("CREATE INDEX tmp_weekly_astar_costs_idx ON tmp_weekly_astar_costs (start_vid, end_vid)")
        cur.execute(
            f"""
            INSERT INTO eq.crop_accessibility_weekly_astar (
                country_code, week_start, scenario, origin_scope,
                crop_code, candidate_rank, crop_rank, harvested_area,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                dest_type, dest_rank, dest_id, dest_name, population,
                origin_node, dest_node, straight_dist_km, travel_time_h, route_status
            )
            SELECT od.country_code, %s::date, %s::text, %s::text,
                   od.crop_code, od.candidate_rank, od.crop_rank, od.harvested_area,
                   od.cluster_cell_count, od.representative_cell_harvested_area, od.cluster_share,
                   od.dest_type, od.dest_rank, od.dest_id, od.dest_name, od.population,
                   od.origin_node, od.dest_node, od.straight_dist_km,
                   r.agg_cost,
                   CASE WHEN r.agg_cost IS NULL THEN 'unreachable' ELSE 'ok' END
            FROM eq.{qident(od_table)} od
            LEFT JOIN tmp_weekly_astar_costs r
              ON r.start_vid = od.origin_node AND r.end_vid = od.dest_node
            """,
            (week_start, scenario, origin_scope),
        )
    conn.commit()
    return int(
        scalar(
            conn,
            """
            SELECT count(*) FROM eq.crop_accessibility_weekly_astar
            WHERE country_code = %s AND week_start = %s AND scenario = %s AND origin_scope = %s
            """,
            (iso, week_start, scenario, origin_scope),
        )
        or 0
    )


def run_country(conn: psycopg.Connection, iso: str, args: argparse.Namespace, hb_state: HeartbeatState) -> None:
    suffix = iso.lower()
    hb_state.update(country=iso, stage="prepare", week="-", week_idx=0, week_count=0, reset_timer=True)
    graph_prefix = args.graph_prefix
    required = [
        f"road_graph_edges_pgr_{suffix}",
        f"road_graph_nodes_{suffix}",
        f"road_graph_components_{suffix}",
        crop_origin_node_table(conn, iso, graph_prefix),
    ]
    if graph_prefix != "road_graph":
        required.extend(
            [
                f"{graph_prefix}_edges_pgr_{suffix}",
                f"{graph_prefix}_nodes_{suffix}",
                f"{graph_prefix}_components_{suffix}",
            ]
        )
    missing = [t for t in required if not table_exists(conn, "eq", t)]
    if missing:
        log(f"skip {iso} missing={','.join(missing)}")
        return

    origin_nodes = crop_origin_node_table(conn, iso, graph_prefix)
    crop_rows = int(scalar(conn, f"SELECT count(*) FROM eq.{qident(origin_nodes)} WHERE country_code = %s", (iso,)) or 0)
    if crop_rows == 0:
        log(f"skip {iso} crop_rows=0")
        return

    ensure_city_nodes(conn, iso, args.force_snap)
    ensure_large_city_nodes(conn, iso, args.force_snap)
    ensure_port_nodes(conn, iso, args.force_snap)
    if args.airport_limit > 0:
        ensure_airport_nodes(conn, iso, args.force_snap)
    if graph_prefix == "road_graph":
        hb_state.update(stage="bridge", reset_timer=True)
        ensure_bridge(conn, iso, args.force_bridge, args.connector_speed_kmh, args.max_connector_km)
    hb_state.update(stage="astar_base", reset_timer=True)
    astar_base = ensure_astar_base(conn, iso, args.force_cache, graph_prefix)
    hb_state.update(stage="od", reset_timer=True)
    od_table, od_rows = build_od(
        conn,
        iso,
        args.top_per_crop,
        args.force_od,
        graph_prefix,
        args.small_city_limit,
        args.port_limit,
        args.large_city_limit,
        args.airport_limit,
    )
    if od_rows == 0:
        log(f"skip {iso} od_rows=0")
        return
    if args.prepare_only:
        log(f"{iso} prepare-only od_rows={od_rows:,}")
        return

    weeks = weeks_for_country(conn, iso)
    hb_state.update(stage="weeks", week_count=len(weeks), reset_timer=True)
    origin_prefix = "allclusters" if args.top_per_crop <= 0 else f"top{args.top_per_crop}_per_crop"
    origin_scope = f"{origin_prefix}_{args.small_city_limit}small_{args.large_city_limit}large_{args.port_limit}ports"
    if args.airport_limit > 0:
        origin_scope += f"_{args.airport_limit}airports"
    if graph_prefix != "road_graph":
        origin_scope = f"{graph_prefix}_{origin_scope}"
    log(f"{iso} run weeks={len(weeks)} od_rows={od_rows:,} origin_scope={origin_scope}")
    for idx, week in enumerate(weeks, start=1):
        hb_state.update(stage="astar", week=week, week_idx=idx, week_count=len(weeks), reset_timer=True)
        t0 = time.monotonic()
        rows = run_week(conn, iso, week, args.scenario, origin_scope, astar_base, od_table, args.replace)
        elapsed = time.monotonic() - t0
        hb_state.update(done_weeks=idx, last_rows=rows)
        log(f"{iso} week={week} {idx}/{len(weeks)} rows={rows:,} elapsed_s={elapsed:.1f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run weekly A* crop accessibility by country from small graphs to large graphs.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="auto", help="auto or comma-separated ISO3 list")
    parser.add_argument("--scenario", default="weekly_sum_penalty_v1")
    parser.add_argument("--top-per-crop", type=int, default=0, help="0 means all snapped cluster origins for each crop.")
    parser.add_argument("--small-city-limit", type=int, default=3)
    parser.add_argument("--port-limit", type=int, default=3)
    parser.add_argument("--large-city-limit", type=int, default=3)
    parser.add_argument("--airport-limit", type=int, default=0)
    parser.add_argument("--heartbeat-s", type=int, default=60)
    parser.add_argument("--connector-speed-kmh", type=float, default=DEFAULT_SPEED_KMH)
    parser.add_argument("--max-connector-km", type=float, default=2.5)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--force-snap", action="store_true")
    parser.add_argument("--force-bridge", action="store_true")
    parser.add_argument("--force-cache", action="store_true")
    parser.add_argument("--force-od", action="store_true")
    parser.add_argument("--max-countries", type=int, default=0)
    parser.add_argument("--graph-prefix", default="road_graph", help="road_graph or a custom graph table prefix such as component_connected")
    parser.add_argument("--prepare-only", action="store_true", help="Build snap/bridge/cache/OD tables without running weekly A*.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    hb_state = HeartbeatState()
    heartbeat = Heartbeat(hb_state, args.heartbeat_s)
    heartbeat.start()
    try:
        with psycopg.connect(args.db_url) as conn:
            conn.execute("SET application_name = 'weekly_astar_accessibility'")
            conn.execute("SET statement_timeout = 0")
            ensure_penalty_rules(conn)
            ensure_results_table(conn)
            ensure_airport_destinations(conn, args.force_snap)
            queue = country_queue(conn, args.countries)
            if args.max_countries > 0:
                queue = queue[: args.max_countries]
            log(f"queue countries={','.join(queue)}")
            for iso in queue:
                run_country(conn, iso, args, hb_state)
        log("complete")
    finally:
        heartbeat.stop()


if __name__ == "__main__":
    main()
