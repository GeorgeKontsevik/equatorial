#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
ORIGIN_SCOPE = "top20_per_crop_3small_3large_3ports"
SCENARIO = "weekly_sum_penalty_v1"


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qliteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def log(message: str) -> None:
    print(message, flush=True)


def short_graph_tag(graph_prefix: str) -> str:
    if graph_prefix == "road_graph":
        return "rg"
    if graph_prefix == "component_connected":
        return "cc"
    if graph_prefix == "cluster_connected":
        return "clc"
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in graph_prefix)[:12]


def infer_limits_from_scope(origin_scope: str) -> tuple[int, int, int, int]:
    import re

    m = re.search(r"_(\d+)small_(\d+)large_(\d+)ports(?:_(\d+)airports)?$", origin_scope)
    if not m:
        raise ValueError(f"Cannot infer destination limits from origin_scope={origin_scope!r}")
    small, large, ports, airports = m.groups()
    return int(small), int(large), int(ports), int(airports or 0)


def od_table_name(iso: str, graph_prefix: str, origin_scope: str) -> str:
    small, large, ports, airports = infer_limits_from_scope(origin_scope)
    limit_tag = f"{small}s_{large}l_{ports}p"
    if airports > 0:
        limit_tag += f"_{airports}a"
    table = f"crop_access_astar_od_{short_graph_tag(graph_prefix)}_{iso.lower()}_{limit_tag}"
    if "allclusters" in origin_scope:
        table += "_allclusters"
    return table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render diagnostic route map for crop-cluster OD paths in the max-delay week.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--country", default="ECU")
    parser.add_argument("--scenario", default=SCENARIO)
    parser.add_argument("--origin-scope", default=ORIGIN_SCOPE)
    parser.add_argument("--week-start", default=None, help="Optional YYYY-MM-DD week to render; defaults to the country's max-delay week.")
    parser.add_argument("--out-dir", default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "route_diagnostics"))
    parser.add_argument("--rain-min-mm", type=float, default=50.0)
    return parser.parse_args()


def fetch_max_case(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> dict[str, object]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            WITH base AS (
                SELECT
                    country_code, week_start, crop_code, candidate_rank, dest_type, dest_rank,
                    dest_id, dest_name, origin_node, dest_node, travel_time_h,
                    concat_ws('|', country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key
                FROM eq.crop_accessibility_weekly_astar
                WHERE country_code = %s
                  AND scenario = %s
                  AND origin_scope = %s
                  AND route_status = 'ok'
                  AND travel_time_h IS NOT NULL
            ),
            baseline AS (
                SELECT od_key, min(travel_time_h) AS baseline_h
                FROM base
                GROUP BY od_key
            )
            SELECT
                b.*,
                bl.baseline_h,
                (b.travel_time_h - bl.baseline_h) * 60.0 AS delta_minutes
            FROM base b
            JOIN baseline bl USING (od_key)
            ORDER BY delta_minutes DESC NULLS LAST
            LIMIT 1
            """,
            (iso, scenario, origin_scope),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"No completed rows for {iso} {scenario} {origin_scope}")
    return dict(row)


def fetch_max_case_for_week(
    conn: psycopg.Connection,
    iso: str,
    week_start: str,
    scenario: str,
    origin_scope: str,
) -> dict[str, object]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            WITH base AS (
                SELECT
                    country_code, week_start, crop_code, candidate_rank, dest_type, dest_rank,
                    dest_id, dest_name, origin_node, dest_node, travel_time_h,
                    concat_ws('|', country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key
                FROM eq.crop_accessibility_weekly_astar
                WHERE country_code = %s
                  AND week_start = %s::date
                  AND scenario = %s
                  AND origin_scope = %s
                  AND route_status = 'ok'
                  AND travel_time_h IS NOT NULL
            ),
            baseline AS (
                SELECT
                    concat_ws('|', country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key,
                    min(travel_time_h) AS baseline_h
                FROM eq.crop_accessibility_weekly_astar
                WHERE country_code = %s
                  AND scenario = %s
                  AND origin_scope = %s
                  AND route_status = 'ok'
                  AND travel_time_h IS NOT NULL
                GROUP BY 1
            )
            SELECT
                b.*,
                bl.baseline_h,
                (b.travel_time_h - bl.baseline_h) * 60.0 AS delta_minutes
            FROM base b
            JOIN baseline bl USING (od_key)
            ORDER BY delta_minutes DESC NULLS LAST
            LIMIT 1
            """,
            (iso, week_start, scenario, origin_scope, iso, scenario, origin_scope),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"No completed rows for {iso} week={week_start} {scenario} {origin_scope}")
    return dict(row)


def fetch_week_stats(conn: psycopg.Connection, iso: str, week_start: str, scenario: str, origin_scope: str) -> dict[str, object]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            WITH base AS (
                SELECT
                    country_code, week_start, crop_code, candidate_rank, dest_type, dest_rank,
                    dest_id, travel_time_h,
                    concat_ws('|', country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key
                FROM eq.crop_accessibility_weekly_astar
                WHERE country_code = %s
                  AND scenario = %s
                  AND origin_scope = %s
                  AND route_status = 'ok'
                  AND travel_time_h IS NOT NULL
            ),
            baseline AS (
                SELECT od_key, min(travel_time_h) AS baseline_h
                FROM base
                GROUP BY od_key
            ),
            deltas AS (
                SELECT b.dest_type, (b.travel_time_h - bl.baseline_h) * 60.0 AS delta_minutes
                FROM base b
                JOIN baseline bl USING (od_key)
                WHERE b.week_start = %s::date
            )
            SELECT
                count(*) AS od_rows,
                count(*) FILTER (WHERE delta_minutes >= 180.0) AS ge_3h,
                count(*) FILTER (WHERE delta_minutes >= 360.0) AS ge_6h,
                count(*) FILTER (WHERE delta_minutes >= 720.0) AS ge_12h,
                jsonb_object_agg(dest_type, cnt ORDER BY dest_type) AS rows_by_dest
            FROM (
                SELECT *, count(*) OVER (PARTITION BY dest_type) AS cnt
                FROM deltas
            ) s
            """,
            (iso, scenario, origin_scope, week_start),
        )
        row = cur.fetchone()
    return dict(row or {})


def create_route_tables(conn: psycopg.Connection, iso: str, week_start: str, scenario: str, origin_scope: str) -> None:
    suffix = iso.lower()
    edge_table = f"road_graph_edges_pgr_{suffix}_bridge_astar_base"
    od_table = od_table_name(iso, "cluster_connected", origin_scope)
    edge_sql = f"""
        SELECT e.id, e.source, e.target,
               e.base_cost / GREATEST(
                   CASE WHEN e.surface_group = 'synthetic_connector' THEN 1.0 ELSE p.speed_multiplier END,
                   0.01
               ) AS cost,
               e.base_reverse_cost / GREATEST(
                   CASE WHEN e.surface_group = 'synthetic_connector' THEN 1.0 ELSE p.speed_multiplier END,
                   0.01
               ) AS reverse_cost,
               e.x1, e.y1, e.x2, e.y2
        FROM eq.{qident(edge_table)} e
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

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_route_pairs")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_route_pairs AS
            SELECT DISTINCT origin_node AS source, dest_node AS target
            FROM eq.{qident(od_table)}
            WHERE country_code = %s
            """,
            (iso,),
        )
        cur.execute("CREATE INDEX tmp_route_pairs_idx ON tmp_route_pairs (source, target)")
        cur.execute("DROP TABLE IF EXISTS tmp_route_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_route_steps AS
            SELECT *
            FROM pgr_aStar(%s, 'SELECT source, target FROM tmp_route_pairs', false, 5, 1.0, 1.0)
            WHERE edge <> -1
            """,
            (edge_sql,),
        )
        cur.execute("CREATE INDEX tmp_route_steps_edge_idx ON tmp_route_steps (edge)")
        cur.execute("CREATE INDEX tmp_route_steps_pair_idx ON tmp_route_steps (start_vid, end_vid)")
        cur.execute("DROP TABLE IF EXISTS tmp_route_edge_usage")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_route_edge_usage AS
            SELECT
                e.id AS edge_id,
                e.surface_group,
                e.cell_id,
                e.x1, e.y1, e.x2, e.y2,
                count(*) AS step_count,
                count(DISTINCT (r.start_vid::text || '|' || r.end_vid::text)) AS route_count
            FROM tmp_route_steps r
            JOIN eq.{qident(edge_table)} e ON e.id = r.edge
            GROUP BY e.id, e.surface_group, e.cell_id, e.x1, e.y1, e.x2, e.y2
            """,
        )
    conn.commit()


def fetch_route_usage(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT edge_id, surface_group, cell_id, x1, y1, x2, y2, step_count, route_count
        FROM tmp_route_edge_usage
        """,
        conn,
    )


def fetch_max_route_usage(conn: psycopg.Connection, max_case: dict[str, object]) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT u.*
        FROM tmp_route_steps r
        JOIN tmp_route_edge_usage u ON u.edge_id = r.edge
        WHERE r.start_vid = %(origin_node)s
          AND r.end_vid = %(dest_node)s
        ORDER BY r.path_seq
        """,
        conn,
        params={"origin_node": max_case["origin_node"], "dest_node": max_case["dest_node"]},
    )


def fetch_origins(conn: psycopg.Connection, iso: str, week_start: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    suffix = iso.lower()
    return pd.read_sql_query(
        f"""
        WITH reachability AS (
            SELECT crop_code, candidate_rank,
                   count(*) FILTER (WHERE route_status = 'ok') AS ok_od_rows,
                   count(*) AS total_od_rows
            FROM eq.crop_accessibility_weekly_astar
            WHERE country_code = %(iso)s
              AND week_start = %(week_start)s::date
              AND scenario = %(scenario)s
              AND origin_scope = %(origin_scope)s
            GROUP BY crop_code, candidate_rank
        )
        SELECT o.crop_code, o.candidate_rank, o.lon, o.lat, o.node_id,
               coalesce(r.ok_od_rows, 0) AS ok_od_rows,
               coalesce(r.total_od_rows, 0) AS total_od_rows,
               (coalesce(r.ok_od_rows, 0) > 0) AS has_reachable_od
        FROM eq.{qident(f"crop_origin_nodes_{suffix}")} o
        LEFT JOIN reachability r
          ON r.crop_code = o.crop_code
         AND r.candidate_rank = o.candidate_rank
        WHERE o.country_code = %(iso)s
        ORDER BY o.crop_code, o.candidate_rank
        """,
        conn,
        params={"iso": iso, "week_start": week_start, "scenario": scenario, "origin_scope": origin_scope},
    )


def fetch_destinations(conn: psycopg.Connection, iso: str, graph_prefix: str, origin_scope: str) -> pd.DataFrame:
    suffix = iso.lower()
    od_table = od_table_name(iso, graph_prefix, origin_scope)
    return pd.read_sql_query(
        f"""
        SELECT DISTINCT od.dest_type, od.dest_rank, od.dest_id, od.dest_name, od.population,
               n.lon, n.lat, od.dest_node
        FROM eq.{qident(od_table)} od
        JOIN eq.{qident(f"road_graph_nodes_{suffix}")} n ON n.node_id = od.dest_node
        WHERE od.country_code = %(iso)s
        ORDER BY od.dest_type, od.dest_rank, od.dest_name
        """,
        conn,
        params={"iso": iso},
    )


def fetch_rain(conn: psycopg.Connection, iso: str, week_start: str, rain_min_mm: float) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT cell_lon, cell_lat, tp_sum_weekly_mm
        FROM eq.era5_precip_weekly_grid
        WHERE country_code = %(iso)s
          AND week_start = %(week_start)s::date
          AND tp_sum_weekly_mm >= %(rain_min_mm)s
        """,
        conn,
        params={"iso": iso, "week_start": week_start, "rain_min_mm": rain_min_mm},
    )


def load_boundary(iso: str) -> gpd.GeoDataFrame:
    path = ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg"
    return gpd.read_file(path, layer="ADM_ADM_0").to_crs("EPSG:4326")


def line_length_km(frame: pd.DataFrame) -> pd.Series:
    lon1 = np.radians(frame["x1"].astype(float))
    lat1 = np.radians(frame["y1"].astype(float))
    lon2 = np.radians(frame["x2"].astype(float))
    lat2 = np.radians(frame["y2"].astype(float))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * np.arcsin(np.sqrt(a))


def plot_segments(ax: plt.Axes, frame: pd.DataFrame, surface_group: str, color: str, label: str, max_width: float = 2.8) -> None:
    subset = frame[frame["surface_class"].eq(surface_group)]
    if subset.empty:
        return
    max_count = max(float(subset["route_count"].max()), 1.0)
    for row in subset.itertuples(index=False):
        width = 0.25 + max_width * math.sqrt(float(row.route_count) / max_count)
        ax.plot([row.x1, row.x2], [row.y1, row.y2], color=color, linewidth=width, alpha=0.34, solid_capstyle="round", zorder=3)
    ax.plot([], [], color=color, linewidth=2.5, alpha=0.75, label=label)


def render_map(
    iso: str,
    max_case: dict[str, object],
    week_stats: dict[str, object],
    route_usage: pd.DataFrame,
    max_route: pd.DataFrame,
    origins: pd.DataFrame,
    destinations: pd.DataFrame,
    rain: pd.DataFrame,
    out_path: Path,
) -> dict[str, object]:
    boundary = load_boundary(iso)
    minx, miny, maxx, maxy = boundary.total_bounds
    pad_x = max((maxx - minx) * 0.06, 0.2)
    pad_y = max((maxy - miny) * 0.06, 0.2)

    route_usage = route_usage.copy()
    route_usage["surface_class"] = np.where(
        route_usage["surface_group"].eq("paved"),
        "paved",
        np.where(route_usage["surface_group"].isin(["synthetic_connector", "unpaved_synthetic"]), "unpaved_synthetic", "unpaved_unknown"),
    )
    route_usage["length_km"] = line_length_km(route_usage)
    length_summary = (
        route_usage.groupby("surface_class", dropna=False)
        .agg(edges=("edge_id", "count"), route_weighted_km=("length_km", lambda s: float(np.sum(s * route_usage.loc[s.index, "route_count"]))))
        .reset_index()
    )

    fig, ax = plt.subplots(figsize=(13.5, 11.0))
    boundary.boundary.plot(ax=ax, color="#202020", linewidth=0.85, zorder=2)

    if not rain.empty:
        rain_values = np.clip(rain["tp_sum_weekly_mm"].astype(float), 50, 300)
        ax.scatter(
            rain["cell_lon"],
            rain["cell_lat"],
            c=rain_values,
            cmap="Blues",
            marker="s",
            s=22,
            alpha=0.28,
            linewidths=0,
            zorder=1,
        )

    plot_segments(ax, route_usage, "unpaved_unknown", "#d95f02", "route edge: unpaved/unknown")
    plot_segments(ax, route_usage, "unpaved_synthetic", "#6a3d9a", "route edge: unpaved synthetic <=2.5km", max_width=1.6)
    plot_segments(ax, route_usage, "paved", "#1b9e77", "route edge: paved")

    if not max_route.empty:
        for row in max_route.itertuples(index=False):
            ax.plot([row.x1, row.x2], [row.y1, row.y2], color="#111111", linewidth=2.8, alpha=0.75, zorder=5)
        ax.plot([], [], color="#111111", linewidth=2.8, alpha=0.75, label="max-delay OD route")

    crop_colors = {
        "avocado": "#4daf4a",
        "banana": "#ffd92f",
        "plantain": "#a6d854",
        "mango": "#ff7f00",
        "pineapple": "#984ea3",
    }
    reachable_origins = origins[origins["has_reachable_od"].astype(bool)].copy()
    unreachable_origins = origins[~origins["has_reachable_od"].astype(bool)].copy()
    for crop, group in reachable_origins.groupby("crop_code"):
        ax.scatter(
            group["lon"],
            group["lat"],
            s=32,
            color=crop_colors.get(crop, "#555555"),
            edgecolor="#111111",
            linewidth=0.35,
            label=f"crop cluster: {crop}",
            zorder=6,
        )
    if not unreachable_origins.empty:
        ax.scatter(
            unreachable_origins["lon"],
            unreachable_origins["lat"],
            s=42,
            marker="x",
            color="#737373",
            linewidth=1.15,
            label="crop cluster: unreachable this week",
            zorder=8,
        )
        ax.scatter(
            unreachable_origins["lon"],
            unreachable_origins["lat"],
            s=32,
            marker="o",
            facecolor="#d9d9d9",
            edgecolor="#4d4d4d",
            linewidth=0.45,
            zorder=7,
        )

    dest_styles = {
        "city_5_100k": {"marker": "o", "color": "#377eb8", "label": "destination: city 5-100k"},
        "city_100k_plus": {"marker": "^", "color": "#08519c", "label": "destination: city 100k+"},
        "port": {"marker": "s", "color": "#e41a1c", "label": "destination: port"},
    }
    for dest_type, group in destinations.groupby("dest_type"):
        style = dest_styles.get(dest_type, {"marker": "D", "color": "#444444", "label": f"destination: {dest_type}"})
        ax.scatter(
            group["lon"],
            group["lat"],
            s=54,
            marker=style["marker"],
            color=style["color"],
            edgecolor="white",
            linewidth=0.55,
            label=style["label"],
            zorder=7,
        )

    max_delta = float(max_case["delta_minutes"])
    title = (
        f"{iso} route diagnostic, max-delay week {max_case['week_start']}\n"
        f"max OD: {max_case['crop_code']} cluster {max_case['candidate_rank']} -> {max_case['dest_type']} "
        f"{max_case['dest_name']} | delta={max_delta:.0f} min"
    )
    ax.set_title(title, fontsize=12.5)
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(color="#d9d9d9", linewidth=0.35, alpha=0.5)

    handles, labels = ax.get_legend_handles_labels()
    seen = set()
    dedup_handles = []
    dedup_labels = []
    for handle, label in zip(handles, labels):
        if label in seen:
            continue
        seen.add(label)
        dedup_handles.append(handle)
        dedup_labels.append(label)
    ax.legend(dedup_handles, dedup_labels, loc="lower left", fontsize=8.0, frameon=True, framealpha=0.88, ncol=2)

    stats_text = (
        f"week OD rows={int(week_stats.get('od_rows') or 0):,}\n"
        f">=3h: {int(week_stats.get('ge_3h') or 0):,} | >=6h: {int(week_stats.get('ge_6h') or 0):,} | >=12h: {int(week_stats.get('ge_12h') or 0):,}\n"
        f"unique route edges={len(route_usage):,}; line width scales with route reuse\n"
        f"rain cells shown where weekly sum >=50 mm"
    )
    ax.text(
        0.995,
        0.015,
        stats_text,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.2,
        bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "alpha": 0.88, "boxstyle": "round,pad=0.35"},
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)

    return {
        "country_code": iso,
        "week_start": str(max_case["week_start"]),
        "max_delta_minutes": max_delta,
        "max_crop_code": max_case["crop_code"],
        "max_candidate_rank": int(max_case["candidate_rank"]),
        "max_dest_type": max_case["dest_type"],
        "max_dest_name": max_case["dest_name"],
        "od_rows": int(week_stats.get("od_rows") or 0),
        "ge_3h": int(week_stats.get("ge_3h") or 0),
        "ge_6h": int(week_stats.get("ge_6h") or 0),
        "ge_12h": int(week_stats.get("ge_12h") or 0),
        "route_edges": int(len(route_usage)),
        "reachable_origin_clusters": int(len(reachable_origins)),
        "unreachable_origin_clusters": int(len(unreachable_origins)),
        "surface_length_summary": length_summary.to_dict(orient="records"),
        "png": str(out_path),
    }


def main() -> None:
    args = parse_args()
    iso = args.country.strip().upper()
    out_dir = Path(args.out_dir)
    with psycopg.connect(args.db_url) as conn:
        if args.week_start:
            week_start = str(args.week_start)
            max_case = fetch_max_case_for_week(conn, iso, week_start, args.scenario, args.origin_scope)
        else:
            max_case = fetch_max_case(conn, iso, args.scenario, args.origin_scope)
            week_start = str(max_case["week_start"])
        log(f"[case] {iso} week={week_start} max_delta={float(max_case['delta_minutes']):.1f} min")
        week_stats = fetch_week_stats(conn, iso, week_start, args.scenario, args.origin_scope)
        log(f"[routes] building pgr_aStar routes for all OD pairs")
        create_route_tables(conn, iso, week_start, args.scenario, args.origin_scope)
        route_usage = fetch_route_usage(conn)
        max_route = fetch_max_route_usage(conn, max_case)
        origins = fetch_origins(conn, iso, week_start, args.scenario, args.origin_scope)
        destinations = fetch_destinations(conn, iso, "cluster_connected", args.origin_scope)
        rain = fetch_rain(conn, iso, week_start, args.rain_min_mm)
        item = render_map(
            iso,
            max_case,
            week_stats,
            route_usage,
            max_route,
            origins,
            destinations,
            rain,
            out_dir / f"{iso}_max_week_route_diagnostic.png",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / f"{iso}_max_week_route_diagnostic.json"
    manifest_path.write_text(json.dumps(item, indent=2), encoding="utf-8")
    log(f"[done] png={item['png']}")
    log(f"[done] manifest={manifest_path}")


if __name__ == "__main__":
    main()
