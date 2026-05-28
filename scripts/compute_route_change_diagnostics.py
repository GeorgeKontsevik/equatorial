#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
import seaborn as sns

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SCENARIO = "weekly_sum_penalty_v1"
DEFAULT_ORIGIN_SCOPE = "cluster_connected_allclusters_10small_3large_3ports_3airports"
DEFAULT_GRAPH_PREFIX = "cluster_connected"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "route_change_diagnostics"
DEFAULT_BASELINE_MEASUREMENT = "base_cost_no_rain"

CROP_ORDER = ["avocado", "banana", "mango", "pineapple", "plantain"]
CROP_COLORS = {
    "avocado": "#41c7d8",
    "banana": "#ffd91a",
    "mango": "#ff2b65",
    "pineapple": "#69e600",
    "plantain": "#9b42f5",
}
SURFACE_ORDER = ["paved", "unpaved", "unpaved_synthetic_line", "unknown"]
SURFACE_LABELS = {
    "paved": "paved",
    "unpaved": "unpaved",
    "unpaved_synthetic_line": "synthetic unpaved link",
    "unknown": "unknown",
}
SURFACE_COLORS = {
    "paved": "#2f9e44",
    "unpaved": "#f08c00",
    "unpaved_synthetic_line": "#d9480f",
    "unknown": "#868e96",
}
WEEK_ORDER = ["mean_impact", "peak_impact"]
WEEK_LABELS = {"mean_impact": "mean impact week", "peak_impact": "peak impact week"}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


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


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        return cur.fetchone()[0] is not None


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> object | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def available_countries(conn: psycopg.Connection, scenario: str, origin_scope: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code
            FROM eq.crop_accessibility_weekly_astar
            WHERE scenario = %s AND origin_scope = %s
            GROUP BY country_code
            ORDER BY country_code
            """,
            (scenario, origin_scope),
        )
        return [row[0] for row in cur.fetchall()]


def parse_countries(conn: psycopg.Connection, requested: str, scenario: str, origin_scope: str) -> list[str]:
    if requested.lower() == "auto":
        return available_countries(conn, scenario, origin_scope)
    return [item.strip().upper() for item in requested.split(",") if item.strip()]


def read_df(conn: psycopg.Connection, sql: str, params: dict | None = None) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def setup_country_tables(
    conn: psycopg.Connection,
    iso: str,
    graph_prefix: str,
    origin_scope: str,
    measurement: str,
) -> tuple[str, str, str]:
    suffix = iso.lower()
    edge_table = f"{graph_prefix}_edges_pgr_{suffix}"
    astar_base = f"{graph_prefix}_edges_pgr_{suffix}_astar_base"
    od_table = od_table_name(iso, graph_prefix, origin_scope)
    missing = [name for name in [edge_table, astar_base, od_table] if not table_exists(conn, "eq", name)]
    if missing:
        raise RuntimeError(f"{iso} missing required tables: {', '.join(missing)}")

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_diag_od")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_diag_od AS
            SELECT od_id, country_code, crop_code, candidate_rank, crop_rank,
                   harvested_area, cluster_cell_count, representative_cell_harvested_area,
                   cluster_share, dest_type, dest_rank, dest_id, dest_name, population,
                   origin_node, dest_node, straight_dist_km
            FROM eq.{qident(od_table)}
            WHERE country_code = %s
            """,
            (iso,),
        )
        cur.execute("CREATE INDEX tmp_diag_od_pair_idx ON tmp_diag_od (origin_node, dest_node)")
        cur.execute("CREATE INDEX tmp_diag_od_id_idx ON tmp_diag_od (od_id)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_edges")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_diag_edges AS
            SELECT b.id, b.source, b.target, b.surface_group, b.base_cost, b.base_reverse_cost,
                   b.cell_id, b.x1, b.y1, b.x2, b.y2,
                   e.length_km
            FROM eq.{qident(astar_base)} b
            JOIN eq.{qident(edge_table)} e ON e.id = b.id
            WHERE b.base_cost IS NOT NULL AND b.base_cost > 0
              AND b.base_reverse_cost IS NOT NULL AND b.base_reverse_cost > 0
              AND e.length_km IS NOT NULL AND e.length_km > 0
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_edges_id_idx ON tmp_diag_edges (id)")
        cur.execute("CREATE INDEX tmp_diag_edges_cell_idx ON tmp_diag_edges (cell_id)")
        edge_sql = (
            "SELECT id, source, target, base_cost AS cost, base_reverse_cost AS reverse_cost, x1, y1, x2, y2 "
            "FROM tmp_diag_edges"
        )
        pairs_sql = "SELECT DISTINCT origin_node AS source, dest_node AS target FROM tmp_diag_od"
        cur.execute("DROP TABLE IF EXISTS tmp_diag_base_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_base_steps AS
            SELECT *
            FROM pgr_aStar(%s, %s, false, 5, 1.0, 1.0)
            WHERE edge <> -1
            """,
            (edge_sql, pairs_sql),
        )
        cur.execute("CREATE INDEX tmp_diag_base_steps_pair_idx ON tmp_diag_base_steps (start_vid, end_vid)")
        cur.execute("CREATE INDEX tmp_diag_base_steps_edge_idx ON tmp_diag_base_steps (edge)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_base_od_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_base_od_steps AS
            SELECT od.od_id, s.path_seq, s.edge, s.cost, e.length_km
            FROM tmp_diag_base_steps s
            JOIN tmp_diag_od od ON od.origin_node = s.start_vid AND od.dest_node = s.end_vid
            JOIN tmp_diag_edges e ON e.id = s.edge
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_base_od_steps_od_idx ON tmp_diag_base_od_steps (od_id)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_base_od_totals")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_base_od_totals AS
            SELECT od_id,
                   count(*)::integer AS base_edge_count,
                   sum(length_km)::double precision AS base_length_km,
                   sum(cost)::double precision AS base_travel_time_h,
                   md5(string_agg(edge::text, ',' ORDER BY path_seq)) AS base_path_signature
            FROM tmp_diag_base_od_steps
            GROUP BY od_id
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_base_od_totals_od_idx ON tmp_diag_base_od_totals (od_id)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_stored_base_od_totals")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_stored_base_od_totals AS
            SELECT od.od_id,
                   max(b.total_edge_count)::integer AS stored_base_edge_count,
                   max(b.total_length_km)::double precision AS stored_base_length_km,
                   max(b.total_travel_time_h)::double precision AS stored_base_travel_time_h
            FROM tmp_diag_od od
            JOIN eq.crop_accessibility_base_route_surface_mix b
              ON b.country_code = od.country_code
             AND b.crop_code = od.crop_code
             AND b.candidate_rank = od.candidate_rank
             AND b.dest_type = od.dest_type
             AND b.dest_rank = od.dest_rank
             AND b.dest_id = od.dest_id
            WHERE b.graph_prefix = %s
              AND b.origin_scope = %s
              AND b.measurement = %s
            GROUP BY od.od_id
            """,
            (graph_prefix, origin_scope, measurement),
        )
        cur.execute("DROP TABLE IF EXISTS tmp_diag_base_surface_summary")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_base_surface_summary AS
            SELECT b.country_code, b.crop_code, b.dest_type, b.surface_group,
                   sum(b.surface_length_km * GREATEST(COALESCE(od.cluster_cell_count, 1), 1)) AS base_weighted_length_km,
                   sum(b.surface_travel_time_h * GREATEST(COALESCE(od.cluster_cell_count, 1), 1)) AS base_weighted_time_h
            FROM tmp_diag_od od
            JOIN eq.crop_accessibility_base_route_surface_mix b
              ON b.country_code = od.country_code
             AND b.crop_code = od.crop_code
             AND b.candidate_rank = od.candidate_rank
             AND b.dest_type = od.dest_type
             AND b.dest_rank = od.dest_rank
             AND b.dest_id = od.dest_id
            WHERE b.graph_prefix = %s
              AND b.origin_scope = %s
              AND b.measurement = %s
            GROUP BY b.country_code, b.crop_code, b.dest_type, b.surface_group
            """,
            (graph_prefix, origin_scope, measurement),
        )
        cur.execute("ANALYZE tmp_diag_od")
        cur.execute("ANALYZE tmp_diag_edges")
        cur.execute("ANALYZE tmp_diag_base_od_steps")
        cur.execute("ANALYZE tmp_diag_base_od_totals")
    return edge_table, astar_base, od_table


def select_diagnostic_weeks(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_diag_week_scores")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_week_scores AS
            WITH base AS (
                SELECT week_start, crop_code, candidate_rank, dest_type, dest_rank, dest_id,
                       cluster_cell_count, travel_time_h,
                       concat_ws('|', crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key
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
                SELECT b.week_start,
                       GREATEST(COALESCE(b.cluster_cell_count, 1), 1)::double precision AS weight,
                       GREATEST(b.travel_time_h - bl.baseline_h, 0.0) AS extra_h
                FROM base b
                JOIN baseline bl USING (od_key)
            )
            SELECT week_start,
                   sum(extra_h * weight) / NULLIF(sum(weight), 0) AS impact_weighted_extra_time_h,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY extra_h) AS impact_median_extra_time_h,
                   avg((extra_h >= 3.0)::integer)::double precision AS affected_ge_3h_share,
                   count(*) AS od_rows
            FROM deltas
            GROUP BY week_start
            """,
            (iso, scenario, origin_scope),
        )
        cur.execute("ANALYZE tmp_diag_week_scores")
    scores = read_df(
        conn,
        """
        WITH mean_score AS (
            SELECT avg(impact_weighted_extra_time_h) AS mean_impact_h
            FROM tmp_diag_week_scores
            WHERE impact_weighted_extra_time_h IS NOT NULL
        ),
        selected AS (
            (
                SELECT 'peak_impact'::text AS week_type, s.*, mean_score.mean_impact_h
                FROM tmp_diag_week_scores s CROSS JOIN mean_score
                ORDER BY s.impact_weighted_extra_time_h DESC NULLS LAST, s.week_start
                LIMIT 1
            )
            UNION ALL
            (
                SELECT 'mean_impact'::text AS week_type, s.*, mean_score.mean_impact_h
                FROM tmp_diag_week_scores s CROSS JOIN mean_score
                ORDER BY abs(s.impact_weighted_extra_time_h - mean_score.mean_impact_h), s.week_start
                LIMIT 1
            )
        )
        SELECT week_type, week_start, impact_weighted_extra_time_h, impact_median_extra_time_h,
               affected_ge_3h_share, od_rows, mean_impact_h
        FROM selected
        ORDER BY CASE week_type WHEN 'mean_impact' THEN 1 ELSE 2 END
        """,
    )
    if scores.empty:
        raise RuntimeError(f"{iso} has no accessibility-impact week scores")
    scores.insert(0, "country_code", iso)
    return scores


def compute_wet_routes(conn: psycopg.Connection, iso: str, week_start: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_diag_week_grid")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_week_grid AS
            SELECT cell_id, tp_sum_weekly_mm
            FROM eq.era5_precip_weekly_grid
            WHERE country_code = %s
              AND week_start = %s::date
            """,
            (iso, week_start),
        )
        cur.execute("CREATE INDEX tmp_diag_week_grid_cell_idx ON tmp_diag_week_grid (cell_id)")
        cur.execute("ANALYZE tmp_diag_week_grid")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_wet_edges")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_wet_edges AS
            SELECT e.id, e.source, e.target, e.surface_group,
                   CASE WHEN e.surface_group = 'paved' THEN 'paved' ELSE 'unpaved' END AS effective_road_type,
                   COALESCE(g.tp_sum_weekly_mm, 0.0) AS precip_weekly_mm,
                   p.speed_multiplier,
                   p.effect_label,
                   p.effectively_closed,
                   e.base_cost / GREATEST(p.speed_multiplier, 0.01) AS cost,
                   e.base_reverse_cost / GREATEST(p.speed_multiplier, 0.01) AS reverse_cost,
                   e.x1, e.y1, e.x2, e.y2,
                   e.length_km
            FROM tmp_diag_edges e
            LEFT JOIN tmp_diag_week_grid g ON g.cell_id = e.cell_id
            JOIN LATERAL (
                SELECT speed_multiplier, effect_label, effectively_closed
                FROM eq.weekly_rain_speed_penalty_rules p
                WHERE p.road_type = CASE WHEN e.surface_group = 'paved' THEN 'paved' ELSE 'unpaved' END
                  AND COALESCE(g.tp_sum_weekly_mm, 0.0) >= p.min_weekly_mm
                  AND (p.max_weekly_mm IS NULL OR COALESCE(g.tp_sum_weekly_mm, 0.0) < p.max_weekly_mm)
                ORDER BY p.min_weekly_mm DESC
                LIMIT 1
            ) p ON true
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_wet_edges_id_idx ON tmp_diag_wet_edges (id)")
        cur.execute("CREATE INDEX tmp_diag_wet_edges_penalty_idx ON tmp_diag_wet_edges (surface_group, speed_multiplier)")
        edge_sql = (
            "SELECT id, source, target, cost, reverse_cost, x1, y1, x2, y2 "
            "FROM tmp_diag_wet_edges"
        )
        pairs_sql = "SELECT DISTINCT origin_node AS source, dest_node AS target FROM tmp_diag_od"
        cur.execute("DROP TABLE IF EXISTS tmp_diag_wet_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_wet_steps AS
            SELECT *
            FROM pgr_aStar(%s, %s, false, 5, 1.0, 1.0)
            WHERE edge <> -1
            """,
            (edge_sql, pairs_sql),
        )
        cur.execute("CREATE INDEX tmp_diag_wet_steps_pair_idx ON tmp_diag_wet_steps (start_vid, end_vid)")
        cur.execute("CREATE INDEX tmp_diag_wet_steps_edge_idx ON tmp_diag_wet_steps (edge)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_wet_od_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_wet_od_steps AS
            SELECT od.od_id, od.crop_code, od.candidate_rank, od.dest_type, od.dest_rank, od.dest_id,
                   od.cluster_cell_count, s.path_seq, s.edge, s.cost,
                   e.length_km, e.surface_group, e.effective_road_type,
                   e.precip_weekly_mm, e.speed_multiplier, e.effect_label, e.effectively_closed
            FROM tmp_diag_wet_steps s
            JOIN tmp_diag_od od ON od.origin_node = s.start_vid AND od.dest_node = s.end_vid
            JOIN tmp_diag_wet_edges e ON e.id = s.edge
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_wet_od_steps_od_idx ON tmp_diag_wet_od_steps (od_id)")
        cur.execute("CREATE INDEX tmp_diag_wet_od_steps_edge_idx ON tmp_diag_wet_od_steps (edge)")
        cur.execute("DROP TABLE IF EXISTS tmp_diag_wet_od_totals")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_diag_wet_od_totals AS
            SELECT od_id,
                   count(*)::integer AS wet_edge_count,
                   sum(length_km)::double precision AS wet_length_km,
                   sum(cost)::double precision AS wet_travel_time_h,
                   md5(string_agg(edge::text, ',' ORDER BY path_seq)) AS wet_path_signature,
                   sum(length_km) FILTER (WHERE speed_multiplier < 0.999)::double precision AS wet_penalized_length_km,
                   sum(length_km) FILTER (WHERE speed_multiplier <= 0.05)::double precision AS wet_closed_like_length_km
            FROM tmp_diag_wet_od_steps
            GROUP BY od_id
            """,
        )
        cur.execute("CREATE INDEX tmp_diag_wet_od_totals_od_idx ON tmp_diag_wet_od_totals (od_id)")
        cur.execute("ANALYZE tmp_diag_wet_od_steps")
        cur.execute("ANALYZE tmp_diag_wet_od_totals")


def fetch_od_change(conn: psycopg.Connection, iso: str, week_type: str, week_start: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    frame = read_df(
        conn,
        """
        SELECT od.country_code, %(week_type)s AS week_type, %(week_start)s::date AS week_start,
               od.crop_code, od.candidate_rank, od.crop_rank, od.harvested_area,
               od.cluster_cell_count, od.cluster_share,
               od.dest_type, od.dest_rank, od.dest_id, od.dest_name,
               od.origin_node, od.dest_node, od.straight_dist_km,
               b.base_edge_count, b.base_length_km, b.base_travel_time_h,
               w.wet_edge_count, w.wet_length_km, w.wet_travel_time_h,
               b.base_path_signature, w.wet_path_signature,
               COALESCE(w.wet_penalized_length_km, 0.0) AS wet_penalized_length_km,
               COALESCE(w.wet_closed_like_length_km, 0.0) AS wet_closed_like_length_km,
               wr.travel_time_h AS stored_weekly_travel_time_h,
               (w.wet_travel_time_h - b.base_travel_time_h) AS extra_time_h,
               (w.wet_length_km - b.base_length_km) AS extra_length_km,
               (w.wet_length_km / NULLIF(b.base_length_km, 0) - 1.0) AS extra_length_pct,
               (w.wet_travel_time_h / NULLIF(b.base_travel_time_h, 0)) AS travel_time_ratio,
               CASE
                   WHEN w.od_id IS NULL THEN true
                   WHEN b.base_path_signature IS DISTINCT FROM w.wet_path_signature THEN true
                   ELSE false
               END AS path_changed_by_edge_sequence,
               (w.wet_travel_time_h - wr.travel_time_h) AS stored_weekly_diff_h
        FROM tmp_diag_od od
        LEFT JOIN tmp_diag_base_od_totals b ON b.od_id = od.od_id
        LEFT JOIN tmp_diag_wet_od_totals w ON w.od_id = od.od_id
        LEFT JOIN eq.crop_accessibility_weekly_astar wr
          ON wr.country_code = od.country_code
         AND wr.week_start = %(week_start)s::date
         AND wr.scenario = %(scenario)s
         AND wr.origin_scope = %(origin_scope)s
         AND wr.crop_code = od.crop_code
         AND wr.candidate_rank = od.candidate_rank
         AND wr.dest_type = od.dest_type
         AND wr.dest_rank = od.dest_rank
         AND wr.dest_id = od.dest_id
        ORDER BY od.crop_code, od.candidate_rank, od.dest_type, od.dest_rank
        """,
        {
            "week_type": week_type,
            "week_start": week_start,
            "scenario": scenario,
            "origin_scope": origin_scope,
        },
    )
    frame["country_code"] = iso
    return frame


def fetch_surface_summary(conn: psycopg.Connection, iso: str, week_type: str, week_start: str) -> pd.DataFrame:
    return read_df(
        conn,
        """
        WITH base AS (
            SELECT %(iso)s AS country_code, %(week_type)s AS week_type, %(week_start)s::date AS week_start,
                   crop_code, dest_type, surface_group,
                   base_weighted_length_km,
                   base_weighted_time_h
            FROM tmp_diag_base_surface_summary
        ),
        wet AS (
            SELECT %(iso)s AS country_code, %(week_type)s AS week_type, %(week_start)s::date AS week_start,
                   crop_code, dest_type, surface_group, effective_road_type, effect_label, speed_multiplier,
                   sum(length_km * GREATEST(COALESCE(cluster_cell_count, 1), 1)) AS wet_weighted_length_km,
                   sum(cost * GREATEST(COALESCE(cluster_cell_count, 1), 1)) AS wet_weighted_time_h,
                   COALESCE(
                       sum(length_km * GREATEST(COALESCE(cluster_cell_count, 1), 1)) FILTER (WHERE speed_multiplier < 0.999),
                       0.0
                   ) AS affected_weighted_length_km,
                   count(*) AS wet_step_rows
            FROM tmp_diag_wet_od_steps
            GROUP BY crop_code, dest_type, surface_group, effective_road_type, effect_label, speed_multiplier
        )
        SELECT w.*, b.base_weighted_length_km, b.base_weighted_time_h
        FROM wet w
        LEFT JOIN base b USING (country_code, week_type, week_start, crop_code, dest_type, surface_group)
        ORDER BY crop_code, dest_type, surface_group, speed_multiplier
        """,
        {"iso": iso, "week_type": week_type, "week_start": week_start},
    )


def weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    mask = values.notna() & weights.notna() & weights.gt(0)
    if not mask.any():
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def weighted_mean_where(values: pd.Series, weights: pd.Series, condition: pd.Series) -> float:
    mask = values.notna() & weights.notna() & weights.gt(0) & condition.fillna(False)
    if not mask.any():
        return float("nan")
    return float(np.average(values[mask], weights=weights[mask]))


def summarize_crop(od_change: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    group_cols = ["country_code", "week_type", "week_start", "crop_code", "dest_type"]
    for keys, group in od_change.groupby(group_cols, dropna=False):
        weight = group["cluster_cell_count"].fillna(1).clip(lower=1)
        rows.append(
            {
                **dict(zip(group_cols, keys, strict=True)),
                "od_count": int(len(group)),
                "cluster_weight_sum": float(weight.sum()),
                "weighted_mean_extra_time_h": weighted_mean(group["extra_time_h"], weight),
                "weighted_median_extra_time_h": float(group["extra_time_h"].median(skipna=True)),
                "weighted_mean_extra_length_km": weighted_mean(group["extra_length_km"], weight),
                "weighted_mean_extra_length_pct": weighted_mean(group["extra_length_pct"], weight),
                "path_changed_weighted_share": weighted_mean(group["path_changed_by_edge_sequence"].astype(float), weight),
                "rerouted_abs_extra_length_km_mean": weighted_mean_where(
                    group["extra_length_km"].abs(),
                    weight,
                    group["path_changed_by_edge_sequence"].astype(bool),
                ),
                "rerouted_abs_extra_length_pct_mean": weighted_mean_where(
                    group["extra_length_pct"].abs(),
                    weight,
                    group["path_changed_by_edge_sequence"].astype(bool),
                ),
                "affected_ge_3h_weighted_share": weighted_mean(group["extra_time_h"].ge(3.0).astype(float), weight),
                "affected_ge_6h_weighted_share": weighted_mean(group["extra_time_h"].ge(6.0).astype(float), weight),
                "max_extra_time_h": float(group["extra_time_h"].max(skipna=True)),
                "stored_weekly_abs_diff_max_h": float(group["stored_weekly_diff_h"].abs().max(skipna=True)),
            }
        )
    return pd.DataFrame(rows)


def summarize_country(crop_summary: pd.DataFrame, od_change: pd.DataFrame, weeks: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for (country, week_type, week_start), group in od_change.groupby(["country_code", "week_type", "week_start"], dropna=False):
        weight = group["cluster_cell_count"].fillna(1).clip(lower=1)
        week_row = weeks[(weeks["country_code"].eq(country)) & (weeks["week_type"].eq(week_type))]
        selector_score = float(week_row["impact_weighted_extra_time_h"].iloc[0]) if not week_row.empty else float("nan")
        rows.append(
            {
                "country_code": country,
                "week_type": week_type,
                "week_start": str(week_start)[:10],
                "selector_weighted_extra_time_h": selector_score,
                "od_count": int(len(group)),
                "cluster_weight_sum": float(weight.sum()),
                "weighted_mean_extra_time_h": weighted_mean(group["extra_time_h"], weight),
                "median_extra_time_h": float(group["extra_time_h"].median(skipna=True)),
                "max_extra_time_h": float(group["extra_time_h"].max(skipna=True)),
                "weighted_mean_extra_length_pct": weighted_mean(group["extra_length_pct"], weight),
                "path_changed_weighted_share": weighted_mean(group["path_changed_by_edge_sequence"].astype(float), weight),
                "rerouted_abs_extra_length_km_mean": weighted_mean_where(
                    group["extra_length_km"].abs(),
                    weight,
                    group["path_changed_by_edge_sequence"].astype(bool),
                ),
                "rerouted_abs_extra_length_pct_mean": weighted_mean_where(
                    group["extra_length_pct"].abs(),
                    weight,
                    group["path_changed_by_edge_sequence"].astype(bool),
                ),
                "affected_ge_3h_weighted_share": weighted_mean(group["extra_time_h"].ge(3.0).astype(float), weight),
                "affected_ge_6h_weighted_share": weighted_mean(group["extra_time_h"].ge(6.0).astype(float), weight),
                "stored_weekly_abs_diff_max_h": float(group["stored_weekly_diff_h"].abs().max(skipna=True)),
            }
        )
    return pd.DataFrame(rows)


def nearest_node_sanity(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> dict[str, object]:
    with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(
            """
            WITH per_od AS (
                SELECT crop_code, candidate_rank, dest_type, dest_rank, dest_id,
                       count(DISTINCT origin_node) AS origin_nodes,
                       count(DISTINCT dest_node) AS dest_nodes,
                       count(DISTINCT origin_node::text || '|' || dest_node::text) AS node_pairs
                FROM eq.crop_accessibility_weekly_astar
                WHERE country_code = %s
                  AND scenario = %s
                  AND origin_scope = %s
                GROUP BY crop_code, candidate_rank, dest_type, dest_rank, dest_id
            )
            SELECT %s::text AS country_code,
                   count(*) AS od_keys,
                   count(*) FILTER (WHERE origin_nodes > 1) AS origin_node_changed_od,
                   count(*) FILTER (WHERE dest_nodes > 1) AS dest_node_changed_od,
                   count(*) FILTER (WHERE node_pairs > 1) AS node_pair_changed_od
            FROM per_od
            """,
            (iso, scenario, origin_scope, iso),
        )
        return dict(cur.fetchone())


def process_country(
    conn: psycopg.Connection,
    iso: str,
    scenario: str,
    origin_scope: str,
    graph_prefix: str,
    measurement: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]]:
    t0 = time.monotonic()
    setup_country_tables(conn, iso, graph_prefix, origin_scope, measurement)
    od_rows = int(scalar(conn, "SELECT count(*) FROM tmp_diag_od") or 0)
    edge_rows = int(scalar(conn, "SELECT count(*) FROM tmp_diag_edges") or 0)
    base_od_rows = int(scalar(conn, "SELECT count(*) FROM tmp_diag_base_od_totals") or 0)
    log(f"{iso} baseline loaded od={base_od_rows:,}/{od_rows:,} graph_edges={edge_rows:,}")
    weeks = select_diagnostic_weeks(conn, iso, scenario, origin_scope)
    week_desc = ", ".join(f"{r.week_type}:{str(r.week_start)[:10]} {r.impact_weighted_extra_time_h:.2f}h" for r in weeks.itertuples())
    log(f"{iso} selected weeks {week_desc}")

    od_frames: list[pd.DataFrame] = []
    surface_frames: list[pd.DataFrame] = []
    for week in weeks.itertuples(index=False):
        week_start = str(week.week_start)[:10]
        log(f"{iso} {week.week_type} wet routes week={week_start}")
        compute_wet_routes(conn, iso, week_start)
        od_frames.append(fetch_od_change(conn, iso, week.week_type, week_start, scenario, origin_scope))
        surface_frames.append(fetch_surface_summary(conn, iso, week.week_type, week_start))

    od_change = pd.concat(od_frames, ignore_index=True)
    surface = pd.concat(surface_frames, ignore_index=True)
    crop_summary = summarize_crop(od_change)
    country_summary = summarize_country(crop_summary, od_change, weeks)
    sanity = nearest_node_sanity(conn, iso, scenario, origin_scope)
    sanity["elapsed_s"] = round(time.monotonic() - t0, 1)
    sanity["baseline_od_rows"] = base_od_rows
    sanity["od_rows"] = od_rows
    return weeks, od_change, surface, crop_summary, sanity


def save_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)


def ordered_week_columns(frame: pd.DataFrame) -> list[str]:
    return [week for week in WEEK_ORDER if week in set(frame["week_type"])]


def plot_country_summary(country: pd.DataFrame, out_path: Path) -> None:
    if country.empty:
        return
    metrics = [
        ("weighted_mean_extra_time_h", "weighted mean extra time, h", "YlOrRd", None),
        ("path_changed_weighted_share", "rerouted OD exposure share", "PuBu", (0, 1)),
        ("rerouted_abs_extra_length_km_mean", "mean route length change on rerouted OD, km", "YlGnBu", None),
    ]
    countries = (
        country.loc[country["week_type"].eq("peak_impact")]
        .sort_values("weighted_mean_extra_time_h", ascending=False)["country_code"]
        .tolist()
    )
    for iso in country["country_code"].sort_values().unique():
        if iso not in countries:
            countries.append(iso)
    fig, axes = plt.subplots(1, len(metrics), figsize=(13.5, max(7.0, 0.28 * len(countries))), constrained_layout=True)
    if len(metrics) == 1:
        axes = [axes]
    for ax, (metric, title, cmap, fixed_range) in zip(axes, metrics, strict=True):
        table = country.pivot_table(index="country_code", columns="week_type", values=metric, aggfunc="mean")
        table = table.reindex(index=countries, columns=ordered_week_columns(country))
        data = table.to_numpy(dtype=float)
        if fixed_range is None:
            vmax = np.nanpercentile(data, 95)
            if not np.isfinite(vmax) or vmax == 0:
                vmax = 1.0
            vmin = 0.0
        else:
            vmin, vmax = fixed_range
        image = ax.imshow(data, aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax)
        ax.set_title(title)
        ax.set_xticks(np.arange(len(table.columns)))
        ax.set_xticklabels([WEEK_LABELS.get(c, c) for c in table.columns], rotation=25, ha="right")
        ax.set_yticks(np.arange(len(table.index)))
        ax.set_yticklabels(table.index)
        for i in range(data.shape[0]):
            for j in range(data.shape[1]):
                value = data[i, j]
                if not np.isfinite(value):
                    continue
                if metric == "path_changed_weighted_share":
                    label = f"{value:.0%}"
                elif metric.endswith("_km_mean"):
                    label = f"{value:.1f}"
                else:
                    label = f"{value:.1f}"
                ax.text(j, i, label, ha="center", va="center", fontsize=7.2, color="#212529")
        cbar = fig.colorbar(image, ax=ax, fraction=0.045, pad=0.02)
        if "share" in metric or "pct" in metric:
            cbar.ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    fig.suptitle("Route-change diagnostics by selected impact week", fontsize=14)
    fig.savefig(out_path, dpi=220)
    plt.close(fig)


def plot_crop_surface(surface: pd.DataFrame, out_path: Path) -> None:
    if surface.empty:
        return
    sns.set_theme(style="whitegrid", context="talk")
    affected = surface[surface["speed_multiplier"].lt(0.999)].copy()
    if affected.empty:
        affected = surface.copy()
        affected["affected_weighted_length_km"] = 0.0
    grouped = (
        affected.groupby(["week_type", "crop_code", "surface_group"], as_index=False)["affected_weighted_length_km"]
        .sum()
    )
    grouped["crop_code"] = pd.Categorical(grouped["crop_code"], categories=CROP_ORDER, ordered=True)
    grouped["surface_group"] = pd.Categorical(grouped["surface_group"], categories=SURFACE_ORDER, ordered=True)
    max_value = float(grouped["affected_weighted_length_km"].max(skipna=True) or 0.0)
    scale = 1e9 if max_value >= 1e9 else 1e6 if max_value >= 1e6 else 1.0
    unit = "billion cluster-km" if scale == 1e9 else "million cluster-km" if scale == 1e6 else "cluster-km"
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.6), sharey=True, constrained_layout=True)
    for ax, week_type in zip(axes, WEEK_ORDER, strict=True):
        subset = grouped[grouped["week_type"].eq(week_type)]
        table = subset.pivot_table(index="crop_code", columns="surface_group", values="affected_weighted_length_km", aggfunc="sum", fill_value=0.0)
        table = table.reindex(index=CROP_ORDER, columns=SURFACE_ORDER, fill_value=0.0)
        x = np.arange(len(table.index))
        bottom = np.zeros(len(table.index))
        for surface_group in SURFACE_ORDER:
            values = table[surface_group].to_numpy(dtype=float) / scale
            ax.bar(
                x,
                values,
                bottom=bottom,
                color=SURFACE_COLORS[surface_group],
                edgecolor="#ffffff",
                linewidth=1.0,
                label=SURFACE_LABELS[surface_group],
            )
            bottom += values
        ax.set_title(WEEK_LABELS.get(week_type, week_type), fontsize=16, pad=8)
        ax.set_xticks(x)
        ax.set_xticklabels(table.index, rotation=25, ha="right")
        ax.grid(axis="y", color="#d0d4da", linewidth=0.8)
        ax.grid(axis="x", visible=False)
        ax.set_axisbelow(True)
        ax.set_ylabel(f"Affected route length, {unit}", fontsize=13)
        sns.despine(ax=ax, left=False, bottom=False)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.supxlabel("Crop", fontsize=13, y=0.05)
    fig.legend(
        handles,
        labels,
        title="Road surface",
        loc="lower center",
        ncols=4,
        bbox_to_anchor=(0.5, -0.12),
        frameon=False,
        fontsize=12,
        title_fontsize=12,
    )
    fig.suptitle("Affected route-edge exposure by crop and surface type", fontsize=18, y=1.03)
    fig.savefig(out_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def plot_reroute_scatter(od_change: pd.DataFrame, out_path: Path) -> None:
    if od_change.empty:
        return
    sns.set_theme(style="whitegrid", context="talk")
    plot = od_change.copy()
    plot = plot[plot["extra_time_h"].notna() & plot["extra_length_pct"].notna()].copy()
    plot["extra_time_h"] = plot["extra_time_h"].clip(lower=0.0)
    plot["extra_length_pct"] = plot["extra_length_pct"].clip(lower=0.0)
    if len(plot) > 80000:
        plot = plot.sample(80000, random_state=42)
    bins = [-1e-12, 1e-12, 0.005, 0.01, 0.02, 0.05, 0.10, 1.0]
    labels = ["0%", "(0,0.5%]", "0.5-1%", "1-2%", "2-5%", "5-10%", ">10%"]
    plot["length_bin"] = pd.cut(plot["extra_length_pct"], bins=bins, labels=labels, include_lowest=True, right=True)
    fig, axes = plt.subplots(1, 2, figsize=(14.0, 5.8), sharey=True, constrained_layout=True)
    for ax, week_type in zip(axes, WEEK_ORDER, strict=True):
        subset = plot[plot["week_type"].eq(week_type)]
        if subset.empty:
            continue
        sns.boxenplot(
            data=subset,
            x="length_bin",
            y="extra_time_h",
            color="#2f7fb8",
            linewidth=1.0,
            k_depth="proportion",
            showfliers=False,
            ax=ax,
        )
        ax.set_title(WEEK_LABELS.get(week_type, week_type), fontsize=16, pad=8)
        ax.set_xlabel("Route length increase bins vs baseline")
        ax.set_yscale("log")
        ax.grid(color="#d0d4da", linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", rotation=25)
        counts = subset["length_bin"].value_counts().reindex(labels, fill_value=0)
        for tick, n in zip(ax.get_xticks(), counts.tolist(), strict=True):
            if n <= 0:
                continue
            ax.text(tick, 0.06, f"n={n}", transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9, color="#4c5660")
        sns.despine(ax=ax, left=False, bottom=False)
    axes[0].set_ylabel("Extra travel time vs baseline, h")
    fig.suptitle("Accessibility loss by route-length increase bins", fontsize=18, y=1.03)
    fig.savefig(out_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def render_plots(out_dir: Path, country_summary: pd.DataFrame, surface: pd.DataFrame, od_change: pd.DataFrame) -> list[dict[str, object]]:
    plots_dir = out_dir / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    plots = [
        ("01_country_route_change_summary.png", lambda p: plot_country_summary(country_summary, p)),
        ("02_crop_surface_affected_length.png", lambda p: plot_crop_surface(surface, p)),
        ("03_reroute_scatter.png", lambda p: plot_reroute_scatter(od_change, p)),
    ]
    out: list[dict[str, object]] = []
    for name, fn in plots:
        path = plots_dir / name
        fn(path)
        out.append({"name": name.removesuffix(".png"), "path": str(path)})
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute route-change diagnostics for peak and mean accessibility-impact weeks.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--origin-scope", default=DEFAULT_ORIGIN_SCOPE)
    parser.add_argument("--graph-prefix", default=DEFAULT_GRAPH_PREFIX)
    parser.add_argument("--baseline-measurement", default=DEFAULT_BASELINE_MEASUREMENT)
    parser.add_argument("--countries", default="auto")
    parser.add_argument("--max-countries", type=int, default=0)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--skip-plots", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    week_frames: list[pd.DataFrame] = []
    od_frames: list[pd.DataFrame] = []
    surface_frames: list[pd.DataFrame] = []
    crop_frames: list[pd.DataFrame] = []
    country_frames: list[pd.DataFrame] = []
    sanity_rows: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []

    with psycopg.connect(args.db_url) as conn:
        conn.execute("SET application_name = 'route_change_diagnostics'")
        conn.execute("SET statement_timeout = 0")
        countries = parse_countries(conn, args.countries, args.scenario, args.origin_scope)
        if args.max_countries > 0:
            countries = countries[: args.max_countries]
        log(f"countries={','.join(countries)}")
        for idx, iso in enumerate(countries, start=1):
            try:
                log(f"{iso} start {idx}/{len(countries)}")
                weeks, od_change, surface, crop_summary, sanity = process_country(
                    conn, iso, args.scenario, args.origin_scope, args.graph_prefix, args.baseline_measurement
                )
                country_summary = summarize_country(crop_summary, od_change, weeks)
                week_frames.append(weeks)
                od_frames.append(od_change)
                surface_frames.append(surface)
                crop_frames.append(crop_summary)
                country_frames.append(country_summary)
                sanity_rows.append(sanity)
                log(f"{iso} done elapsed_s={sanity['elapsed_s']}")
            except Exception as exc:
                errors.append({"country_code": iso, "error": repr(exc)})
                log(f"{iso} ERROR {exc!r}")
                conn.rollback()

    diagnostic_weeks = pd.concat(week_frames, ignore_index=True) if week_frames else pd.DataFrame()
    od_change = pd.concat(od_frames, ignore_index=True) if od_frames else pd.DataFrame()
    surface_summary = pd.concat(surface_frames, ignore_index=True) if surface_frames else pd.DataFrame()
    crop_summary = pd.concat(crop_frames, ignore_index=True) if crop_frames else pd.DataFrame()
    country_summary = pd.concat(country_frames, ignore_index=True) if country_frames else pd.DataFrame()
    nearest_sanity = pd.DataFrame(sanity_rows)

    save_csv(diagnostic_weeks, args.out_dir / "diagnostic_weeks.csv")
    save_csv(od_change, args.out_dir / "od_route_change.csv")
    save_csv(surface_summary, args.out_dir / "route_surface_penalty_summary.csv")
    save_csv(crop_summary, args.out_dir / "crop_route_change_summary.csv")
    save_csv(country_summary, args.out_dir / "country_route_change_summary.csv")
    save_csv(nearest_sanity, args.out_dir / "nearest_node_sanity.csv")

    plots: list[dict[str, object]] = []
    if not args.skip_plots:
        plots = render_plots(args.out_dir, country_summary, surface_summary, od_change)

    manifest = {
        "scenario": args.scenario,
        "origin_scope": args.origin_scope,
        "graph_prefix": args.graph_prefix,
        "baseline_measurement": args.baseline_measurement,
        "baseline_source": "eq.crop_accessibility_base_route_surface_mix",
        "path_changed_definition": "wet route edge-id sequence differs from recomputed no-rain baseline route edge-id sequence",
        "diagnostic_week_definition": "selected from existing weekly accessibility results: peak=max weighted extra travel time, mean=closest to mean weighted extra travel time",
        "countries_done": sorted(nearest_sanity["country_code"].tolist()) if not nearest_sanity.empty else [],
        "errors": errors,
        "outputs": {
            "diagnostic_weeks": str(args.out_dir / "diagnostic_weeks.csv"),
            "od_route_change": str(args.out_dir / "od_route_change.csv"),
            "route_surface_penalty_summary": str(args.out_dir / "route_surface_penalty_summary.csv"),
            "crop_route_change_summary": str(args.out_dir / "crop_route_change_summary.csv"),
            "country_route_change_summary": str(args.out_dir / "country_route_change_summary.csv"),
            "nearest_node_sanity": str(args.out_dir / "nearest_node_sanity.csv"),
        },
        "plots": plots,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"done countries={len(manifest['countries_done'])} errors={len(errors)} out_dir={args.out_dir}")


if __name__ == "__main__":
    main()
