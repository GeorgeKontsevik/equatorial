#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from run_weekly_astar_accessibility import DEFAULT_DB_URL, country_boundary_wkt, qident, qliteral, scalar, table_exists


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "terminal_connected_graphs"
CONNECTOR_SPEED_KMH = 15.0


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def metric_crs(bounds) -> str:
    minx, miny, maxx, maxy = bounds
    lon0 = (minx + maxx) / 2.0
    lat0 = (miny + maxy) / 2.0
    return f"+proj=aeqd +lat_0={lat0:.8f} +lon_0={lon0:.8f} +datum=WGS84 +units=m +no_defs"


def read_sql_gdf(conn, sql: str) -> gpd.GeoDataFrame:
    frame = gpd.read_postgis(sql, conn, geom_col="geometry")
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame.to_crs("EPSG:4326")


def country_queue(conn: psycopg.Connection, requested: str) -> list[str]:
    if requested.lower() != "auto":
        return [x.strip().upper() for x in requested.split(",") if x.strip()]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code
            FROM eq.crop_origin_candidates
            GROUP BY country_code
            ORDER BY country_code
            """
        )
        return [row[0] for row in cur.fetchall()]


def load_terminals(conn: psycopg.Connection, iso: str) -> gpd.GeoDataFrame:
    frames = [
        read_sql_gdf(
            conn,
            f"""
            SELECT 'crop'::text AS terminal_type, crop_code || ':' || candidate_rank AS terminal_id,
                   crop_code AS label, geometry
            FROM eq.crop_origin_candidates
            WHERE country_code = {qliteral(iso)}
            """,
        ),
        read_sql_gdf(
            conn,
            f"""
            SELECT 'city_5_100k'::text AS terminal_type, geoname_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.city_destinations_5k_100k
            WHERE country_code = {qliteral(iso)}
            """,
        ),
        read_sql_gdf(
            conn,
            f"""
            SELECT 'city_100k_plus'::text AS terminal_type, geoname_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.city_destinations
            WHERE country_code = {qliteral(iso)} AND population >= 100000
            """,
        ),
        read_sql_gdf(
            conn,
            f"""
            SELECT 'port'::text AS terminal_type, port_id::text AS terminal_id,
                   name AS label, geometry
            FROM eq.port_destinations p
            WHERE ST_Intersects(
                p.geometry,
                ST_GeomFromText({qliteral(country_boundary_wkt(iso))}, 4326)
            )
            """,
        ),
    ]
    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326")
    out["terminal_key"] = iso + ":" + out.terminal_type + ":" + out.terminal_id.astype(str)
    return out


def create_pgr_and_components(conn: psycopg.Connection, iso: str, prefix: str) -> None:
    suffix = iso.lower()
    edges = f"{prefix}_edges_{suffix}"
    nodes = f"{prefix}_nodes_{suffix}"
    pgr = f"{prefix}_edges_pgr_{suffix}"
    comps = f"{prefix}_components_{suffix}"
    astar = f"{prefix}_edges_pgr_{suffix}_astar_base"
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(pgr)} CASCADE")
        cur.execute(
            f"""
            CREATE UNLOGGED TABLE eq.{qident(pgr)} AS
            SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
                   e.road_row_id, e.part_id, e.highway, e.surface_group,
                   e.base_speed_kmh::double precision AS base_speed_kmh,
                   e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
            FROM eq.{qident(edges)} e
            JOIN eq.{qident(nodes)} ns ON ns.node_key = e.source_node_id
            JOIN eq.{qident(nodes)} nt ON nt.node_key = e.target_node_id
            WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0;
            ALTER TABLE eq.{qident(pgr)} ADD PRIMARY KEY (id);
            CREATE INDEX {pgr}_source_idx ON eq.{qident(pgr)} (source);
            CREATE INDEX {pgr}_target_idx ON eq.{qident(pgr)} (target);
            ANALYZE eq.{qident(pgr)};

            DROP TABLE IF EXISTS eq.{qident(comps)} CASCADE;
            CREATE UNLOGGED TABLE eq.{qident(comps)} AS
            SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{pgr}');
            CREATE INDEX {comps}_node_idx ON eq.{qident(comps)} (node);
            CREATE INDEX {comps}_component_idx ON eq.{qident(comps)} (component);
            ANALYZE eq.{qident(comps)};

            DROP TABLE IF EXISTS eq.{qident(astar)} CASCADE;
            CREATE UNLOGGED TABLE eq.{qident(astar)} AS
            SELECT e.id, e.source, e.target, e.surface_group,
                   e.cost AS base_cost, e.reverse_cost AS base_reverse_cost,
                   m.cell_id,
                   ns.lon AS x1, ns.lat AS y1,
                   nt.lon AS x2, nt.lat AS y2
            FROM eq.{qident(pgr)} e
            JOIN eq.{qident(nodes)} ns ON ns.node_id = e.source
            JOIN eq.{qident(nodes)} nt ON nt.node_id = e.target
            LEFT JOIN eq.road_era5_cell_map m
              ON m.country_code = {qliteral(iso)}
             AND m.road_row_id = e.road_row_id;
            ALTER TABLE eq.{qident(astar)} ADD PRIMARY KEY (id);
            CREATE INDEX {astar}_source_idx ON eq.{qident(astar)} (source);
            CREATE INDEX {astar}_target_idx ON eq.{qident(astar)} (target);
            CREATE INDEX {astar}_cell_idx ON eq.{qident(astar)} (cell_id);
            ANALYZE eq.{qident(astar)};
            """
        )
    conn.commit()


def add_terminal_nodes(conn: psycopg.Connection, iso: str, prefix: str, terminals: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    suffix = iso.lower()
    src_edges = f"road_graph_edges_{suffix}"
    src_nodes = f"road_graph_nodes_{suffix}"
    edges = f"{prefix}_edges_{suffix}"
    nodes = f"{prefix}_nodes_{suffix}"
    road_nodes = read_sql_gdf(conn, f"SELECT node_id, node_key, lon, lat, geometry FROM eq.{qident(src_nodes)}")
    crs = metric_crs(terminals.total_bounds)
    terminals_m = terminals.to_crs(crs)
    road_nodes_m = road_nodes.to_crs(crs)
    tree = cKDTree(list(zip(road_nodes_m.geometry.x, road_nodes_m.geometry.y)))
    distances, indexes = tree.query(list(zip(terminals_m.geometry.x, terminals_m.geometry.y)), k=1)

    connectors = []
    max_node_id = int(road_nodes.node_id.max() or 0)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(edges)} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(edges)} AS TABLE eq.{qident(src_edges)}")
        cur.execute(f"DROP TABLE IF EXISTS eq.{qident(nodes)} CASCADE")
        cur.execute(f"CREATE TABLE eq.{qident(nodes)} AS TABLE eq.{qident(src_nodes)}")
        max_edge_id = int(scalar(conn, f"SELECT max(edge_id) FROM eq.{qident(edges)}") or 0)
        for i, (terminal, dist_m, nearest_idx) in enumerate(zip(terminals.itertuples(index=False), distances, indexes, strict=True), start=1):
            road = road_nodes.iloc[int(nearest_idx)]
            node_id = max_node_id + i
            edge_id = max_edge_id + i
            x1, y1 = float(terminal.geometry.x), float(terminal.geometry.y)
            x2, y2 = float(road.lon), float(road.lat)
            key = f"terminal:{iso}:{terminal.terminal_type}:{terminal.terminal_id}"
            cur.execute(
                f"""
                INSERT INTO eq.{qident(nodes)} (node_id, node_key, lon, lat, geometry)
                VALUES (%s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326))
                """,
                (node_id, key, x1, y1, x1, y1),
            )
            length_km = float(dist_m) / 1000.0
            cur.execute(
                f"""
                INSERT INTO eq.{qident(edges)} (
                    edge_id, country_code, road_row_id, part_id, source_node_id, target_node_id,
                    source_lon, source_lat, target_lon, target_lat,
                    highway, surface_group, base_speed_kmh, length_km, base_time_h, geometry
                )
                VALUES (
                    %s, %s, NULL, %s, %s, %s,
                    %s, %s, %s, %s,
                    'terminal_connector', 'unpaved_synthetic_line', %s, %s, %s,
                    ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)), 4326)
                )
                """,
                (
                    edge_id,
                    iso,
                    edge_id,
                    key,
                    str(road.node_key),
                    x1,
                    y1,
                    x2,
                    y2,
                    CONNECTOR_SPEED_KMH,
                    length_km,
                    length_km / CONNECTOR_SPEED_KMH,
                    x1,
                    y1,
                    x2,
                    y2,
                ),
            )
            connectors.append(
                {
                    "terminal_type": terminal.terminal_type,
                    "terminal_id": terminal.terminal_id,
                    "terminal_node_id": node_id,
                    "road_node_id": int(road.node_id),
                    "length_m": float(dist_m),
                    "geometry": LineString([(x1, y1), (x2, y2)]),
                }
            )
        cur.execute(f"ALTER TABLE eq.{qident(nodes)} ADD PRIMARY KEY (node_id)")
        cur.execute(f"CREATE UNIQUE INDEX {nodes}_key_idx ON eq.{qident(nodes)} (node_key)")
        cur.execute(f"CREATE INDEX {nodes}_geom_idx ON eq.{qident(nodes)} USING GIST (geometry)")
        cur.execute(f"ALTER TABLE eq.{qident(edges)} ADD PRIMARY KEY (edge_id)")
        cur.execute(f"CREATE INDEX {edges}_geom_idx ON eq.{qident(edges)} USING GIST (geometry)")
        cur.execute(f"ANALYZE eq.{qident(nodes)}")
        cur.execute(f"ANALYZE eq.{qident(edges)}")
    conn.commit()
    create_pgr_and_components(conn, iso, prefix)
    return gpd.GeoDataFrame(connectors, geometry="geometry", crs="EPSG:4326")


def terminal_snap(conn: psycopg.Connection, iso: str, prefix: str, terminals: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    suffix = iso.lower()
    nodes = f"{prefix}_nodes_{suffix}"
    comps = f"{prefix}_components_{suffix}"
    sql = f"""
    SELECT n.node_id, n.node_key, c.component
    FROM eq.{qident(nodes)} n
    JOIN eq.{qident(comps)} c ON c.node = n.node_id
    WHERE n.node_key LIKE {qliteral(f'terminal:{iso}:%')}
    """
    rows = pd.read_sql_query(sql, conn)
    rows["terminal_key"] = rows.node_key.str.replace(f"^terminal:{iso}:", f"{iso}:", regex=True)
    return terminals.merge(rows[["terminal_key", "node_id", "component"]], on="terminal_key", how="left")


def add_component_links(conn: psycopg.Connection, iso: str, prefix: str, snapped: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    suffix = iso.lower()
    edges = f"{prefix}_edges_{suffix}"
    nodes = f"{prefix}_nodes_{suffix}"
    comps = f"{prefix}_components_{suffix}"
    terminal_components = sorted(int(c) for c in snapped.component.dropna().unique())
    if len(terminal_components) <= 1:
        return gpd.GeoDataFrame(columns=["source", "target", "length_m", "geometry"], geometry="geometry", crs="EPSG:4326")
    comp_list = ",".join(str(c) for c in terminal_components)
    node_rows = read_sql_gdf(
        conn,
        f"""
        SELECT n.node_id, n.node_key, c.component, n.lon, n.lat, n.geometry
        FROM eq.{qident(nodes)} n
        JOIN eq.{qident(comps)} c ON c.node = n.node_id
        WHERE c.component IN ({comp_list})
        """,
    )
    crs = metric_crs(node_rows.total_bounds)
    node_rows_m = node_rows.to_crs(crs)
    comp_counts = snapped.component.value_counts()
    connected = {int(comp_counts.index[0])}
    remaining = set(terminal_components) - connected
    links = []
    while remaining:
        connected_nodes = node_rows_m[node_rows_m.component.isin(connected)]
        tree = cKDTree(list(zip(connected_nodes.geometry.x, connected_nodes.geometry.y)))
        best = None
        for comp in sorted(remaining):
            comp_nodes = node_rows_m[node_rows_m.component.eq(comp)]
            distances, indexes = tree.query(list(zip(comp_nodes.geometry.x, comp_nodes.geometry.y)), k=1)
            pos = int(distances.argmin())
            src = connected_nodes.iloc[int(indexes[pos])]
            dst = comp_nodes.iloc[pos]
            item = {
                "source": int(src.node_id),
                "target": int(dst.node_id),
                "source_key": src.node_key,
                "target_key": dst.node_key,
                "target_component": int(dst.component),
                "length_m": float(distances[pos]),
                "geometry": LineString([(float(src.lon), float(src.lat)), (float(dst.lon), float(dst.lat))]),
            }
            if best is None or item["length_m"] < best["length_m"]:
                best = item
        links.append(best)
        connected.add(best["target_component"])
        remaining.remove(best["target_component"])
    links_gdf = gpd.GeoDataFrame(links, geometry="geometry", crs="EPSG:4326")
    with conn.cursor() as cur:
        max_edge_id = int(scalar(conn, f"SELECT max(edge_id) FROM eq.{qident(edges)}") or 0)
        for i, row in enumerate(links_gdf.itertuples(index=False), start=1):
            edge_id = max_edge_id + i
            x1, y1 = row.geometry.coords[0]
            x2, y2 = row.geometry.coords[-1]
            length_km = float(row.length_m) / 1000.0
            cur.execute(
                f"""
                INSERT INTO eq.{qident(edges)} (
                    edge_id, country_code, road_row_id, part_id, source_node_id, target_node_id,
                    source_lon, source_lat, target_lon, target_lat,
                    highway, surface_group, base_speed_kmh, length_km, base_time_h, geometry
                )
                VALUES (
                    %s, %s, NULL, %s, %s, %s,
                    %s, %s, %s, %s,
                    'component_connector', 'unpaved_synthetic_line', %s, %s, %s,
                    ST_SetSRID(ST_MakeLine(ST_MakePoint(%s,%s), ST_MakePoint(%s,%s)), 4326)
                )
                """,
                (
                    edge_id,
                    iso,
                    edge_id,
                    str(row.source_key),
                    str(row.target_key),
                    x1,
                    y1,
                    x2,
                    y2,
                    CONNECTOR_SPEED_KMH,
                    length_km,
                    length_km / CONNECTOR_SPEED_KMH,
                    x1,
                    y1,
                    x2,
                    y2,
                ),
            )
    conn.commit()
    create_pgr_and_components(conn, iso, prefix)
    return links_gdf


def od_metrics(snapped: gpd.GeoDataFrame) -> dict[str, int]:
    crops = snapped[snapped.terminal_type.eq("crop")]
    out = {}
    for dest_type in ["port", "city_5_100k", "city_100k_plus"]:
        dests = snapped[snapped.terminal_type.eq(dest_type)]
        total = len(crops) * len(dests)
        same = 0
        for comp, group in crops.groupby("component"):
            same += len(group) * int((dests.component == comp).sum())
        out[f"{dest_type}_od"] = int(total)
        out[f"{dest_type}_same_component"] = int(same)
    return out


def draw_map(conn: psycopg.Connection, iso: str, prefix: str, stage: str, snapped: gpd.GeoDataFrame, extra_edges: gpd.GeoDataFrame) -> Path:
    suffix = iso.lower()
    path = ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg"
    boundary = gpd.read_file(path, layer="ADM_ADM_0").to_crs("EPSG:4326")
    edges = read_sql_gdf(conn, f"SELECT surface_group, geometry FROM eq.{qident(f'{prefix}_edges_{suffix}')}")
    fig, ax = plt.subplots(figsize=(12, 15))
    boundary.boundary.plot(ax=ax, color="#222222", linewidth=0.9)
    styles = [
        ("paved", "#009e73", 0.48, 0.80),
        ("unpaved", "#d55e00", 0.50, 0.78),
        ("unknown", "#8c8c8c", 0.34, 0.42),
        ("unpaved_newosm", "#e7298a", 0.80, 0.90),
        ("unpaved_synthetic_line", "#54278f", 1.9, 0.95),
    ]
    for surface, color, width, alpha in styles:
        sub = edges[edges.surface_group.eq(surface)]
        if not sub.empty:
            sub.plot(ax=ax, color=color, linewidth=width, alpha=alpha, label=surface)
    if extra_edges is not None and not extra_edges.empty:
        extra_edges.plot(ax=ax, color="#6a3d9a", linewidth=1.2, alpha=0.95, label="new links")
    for terminal_type, marker, color in [
        ("crop", "x", "#666666"),
        ("city_5_100k", "o", "#377eb8"),
        ("city_100k_plus", "^", "#08519c"),
        ("port", "s", "#e41a1c"),
    ]:
        sub = snapped[snapped.terminal_type.eq(terminal_type)]
        if not sub.empty:
            ax.scatter(sub.geometry.x, sub.geometry.y, s=24, marker=marker, color=color, label=terminal_type, zorder=6)
    minx, miny, maxx, maxy = boundary.total_bounds
    ax.set_xlim(minx - 0.25, maxx + 0.25)
    ax.set_ylim(miny - 0.25, maxy + 0.25)
    ax.grid(True, color="#dddddd", linewidth=0.4, alpha=0.55)
    ax.set_title(f"{iso} terminal connected graph: {stage}")
    ax.legend(loc="lower left", fontsize=7, frameon=True)
    out = OUT_DIR / iso / f"{iso}_{stage}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170)
    plt.close(fig)
    return out


def component_summary(conn: psycopg.Connection, iso: str, prefix: str) -> dict[str, float | int]:
    suffix = iso.lower()
    comps = f"{prefix}_components_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH comp AS (
                SELECT component, count(*)::bigint nodes
                FROM eq.{qident(comps)}
                GROUP BY component
            )
            SELECT sum(nodes)::bigint, count(*)::bigint, max(nodes)::bigint,
                   max(nodes)::float / sum(nodes)::float
            FROM comp
            """
        )
        nodes, components, largest, share = cur.fetchone()
    return {"nodes": nodes, "components": components, "largest_nodes": largest, "largest_share": share}


def run_country(conn: psycopg.Connection, iso: str) -> dict:
    suffix = iso.lower()
    prefix = "terminal_connected"
    required = [f"road_graph_edges_{suffix}", f"road_graph_nodes_{suffix}"]
    missing = [t for t in required if not table_exists(conn, "eq", t)]
    if missing:
        return {"country_code": iso, "skipped": True, "reason": f"missing {missing}"}
    terminals = load_terminals(conn, iso)
    if terminals.empty or not (terminals.terminal_type == "crop").any():
        return {"country_code": iso, "skipped": True, "reason": "no terminals/crops"}
    log(f"{iso} terminals={len(terminals):,}")
    terminal_connectors = add_terminal_nodes(conn, iso, prefix, terminals)
    snapped_before = terminal_snap(conn, iso, prefix, terminals)
    map_before = draw_map(conn, iso, prefix, "01_terminal_connectors", snapped_before, terminal_connectors)
    component_links = add_component_links(conn, iso, prefix, snapped_before)
    snapped_after = terminal_snap(conn, iso, prefix, terminals)
    map_after = draw_map(conn, iso, prefix, "02_terminal_connected", snapped_after, component_links)
    metrics = {
        "country_code": iso,
        "terminal_count": int(len(terminals)),
        "terminal_connectors": {
            "count": int(len(terminal_connectors)),
            "max_length_m": float(terminal_connectors.length_m.max()) if not terminal_connectors.empty else 0.0,
            "p95_length_m": float(terminal_connectors.length_m.quantile(0.95)) if not terminal_connectors.empty else 0.0,
            "over_2500m": int((terminal_connectors.length_m > 2500).sum()) if not terminal_connectors.empty else 0,
        },
        "component_links": {
            "count": int(len(component_links)),
            "max_length_m": float(component_links.length_m.max()) if not component_links.empty else 0.0,
            "p95_length_m": float(component_links.length_m.quantile(0.95)) if not component_links.empty else 0.0,
            "over_2500m": int((component_links.length_m > 2500).sum()) if not component_links.empty else 0,
        },
        "before": component_summary(conn, iso, prefix) | od_metrics(snapped_before),
        "after": component_summary(conn, iso, prefix) | od_metrics(snapped_after),
        "maps": [str(map_before), str(map_after)],
    }
    out = OUT_DIR / iso / f"{iso}_terminal_connected_metrics.json"
    out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    log(
        f"{iso} after ports={metrics['after']['port_same_component']}/{metrics['after']['port_od']} "
        f"small={metrics['after']['city_5_100k_same_component']}/{metrics['after']['city_5_100k_od']} "
        f"large={metrics['after']['city_100k_plus_same_component']}/{metrics['after']['city_100k_plus_od']}"
    )
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="auto")
    parser.add_argument("--exclude", default="BRA,IDN")
    parser.add_argument("--max-countries", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    exclude = {x.strip().upper() for x in args.exclude.split(",") if x.strip()}
    summaries = []
    with psycopg.connect(args.db_url) as conn:
        countries = [iso for iso in country_queue(conn, args.countries) if iso not in exclude]
        if args.max_countries:
            countries = countries[: args.max_countries]
        for iso in countries:
            try:
                summaries.append(run_country(conn, iso))
            except Exception as exc:
                conn.rollback()
                log(f"{iso} ERROR {type(exc).__name__}: {exc}")
                summaries.append({"country_code": iso, "error": f"{type(exc).__name__}: {exc}"})
    (OUT_DIR / "terminal_connected_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    pd.DataFrame(summaries).to_csv(OUT_DIR / "terminal_connected_summary.csv", index=False)
    log(f"done countries={len(summaries)} out={OUT_DIR}")


if __name__ == "__main__":
    main()
