#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd
import psycopg


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_ORIGIN_SCOPE = "top20_per_crop_3small_3large_3ports"


DEST_LABELS = {
    "port": "ports",
    "city_5_100k": "small cities 5-100k",
    "city_100k_plus": "large cities 100k+",
}


def qident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Unsafe SQL identifier: {value}")
    return f'"{value}"'


def qliteral(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def log(message: str) -> None:
    print(message, flush=True)


def load_boundary(iso: str) -> gpd.GeoDataFrame:
    return gpd.read_file(ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg", layer="ADM_ADM_0").to_crs("EPSG:4326")


def create_route_tables(conn: psycopg.Connection, iso: str, dest_type: str, graph_mode: str = "bridge") -> None:
    suffix = iso.lower()
    if graph_mode == "base":
        edge_table = f"road_graph_edges_pgr_{suffix}_astar_base"
    elif graph_mode == "bridge":
        edge_table = f"road_graph_edges_pgr_{suffix}_bridge_astar_base"
    else:
        raise ValueError(f"Unsupported graph_mode={graph_mode!r}")
    od_table = f"crop_accessibility_astar_od_{suffix}"
    edge_sql = f"""
        SELECT id, source, target,
               base_cost AS cost,
               base_reverse_cost AS reverse_cost,
               x1, y1, x2, y2
        FROM eq.{qident(edge_table)}
    """.replace("\n", " ")
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS tmp_base_route_pairs")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_base_route_pairs AS
            SELECT od_id, crop_code, candidate_rank, dest_type, dest_rank, dest_id, dest_name,
                   origin_node AS source, dest_node AS target
            FROM eq.{qident(od_table)}
            WHERE country_code = %s
              AND dest_type = %s
            """,
            (iso, dest_type),
        )
        cur.execute("CREATE INDEX tmp_base_route_pairs_pair_idx ON tmp_base_route_pairs (source, target)")
        cur.execute("DROP TABLE IF EXISTS tmp_base_route_steps")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_base_route_steps AS
            SELECT *
            FROM pgr_aStar(%s, 'SELECT source, target FROM tmp_base_route_pairs', false, 5, 1.0, 1.0)
            WHERE edge <> -1
            """,
            (edge_sql,),
        )
        cur.execute("CREATE INDEX tmp_base_route_steps_edge_idx ON tmp_base_route_steps (edge)")
        cur.execute("CREATE INDEX tmp_base_route_steps_pair_idx ON tmp_base_route_steps (start_vid, end_vid)")
        cur.execute("DROP TABLE IF EXISTS tmp_base_route_edge_usage")
        cur.execute(
            f"""
            CREATE TEMP TABLE tmp_base_route_edge_usage AS
            SELECT e.id AS edge_id, e.surface_group, e.x1, e.y1, e.x2, e.y2,
                   count(*) AS step_count,
                   count(DISTINCT (s.start_vid::text || '|' || s.end_vid::text)) AS route_count
            FROM tmp_base_route_steps s
            JOIN eq.{qident(edge_table)} e ON e.id = s.edge
            GROUP BY e.id, e.surface_group, e.x1, e.y1, e.x2, e.y2
            """
        )
        cur.execute("DROP TABLE IF EXISTS tmp_base_reachable_pairs")
        cur.execute(
            """
            CREATE TEMP TABLE tmp_base_reachable_pairs AS
            SELECT p.*, (r.start_vid IS NOT NULL) AS reachable
            FROM tmp_base_route_pairs p
            LEFT JOIN (
                SELECT DISTINCT start_vid, end_vid
                FROM tmp_base_route_steps
            ) r
              ON r.start_vid = p.source
             AND r.end_vid = p.target
            """
        )
        cur.execute("CREATE INDEX tmp_base_reachable_pairs_origin_idx ON tmp_base_reachable_pairs (crop_code, candidate_rank)")
    conn.commit()


def fetch_route_usage(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT edge_id, surface_group, x1, y1, x2, y2, step_count, route_count
        FROM tmp_base_route_edge_usage
        """,
        conn,
    )


def fetch_pairs(conn: psycopg.Connection, iso: str) -> pd.DataFrame:
    suffix = iso.lower()
    return pd.read_sql_query(
        f"""
        SELECT p.od_id, p.crop_code, p.candidate_rank, p.dest_type, p.dest_rank,
               p.dest_id, p.dest_name, p.source, p.target, p.reachable,
               o.lon AS origin_lon, o.lat AS origin_lat,
               n.lon AS dest_lon, n.lat AS dest_lat
        FROM tmp_base_reachable_pairs p
        JOIN eq.{qident(f"crop_origin_nodes_{suffix}")} o
          ON o.crop_code = p.crop_code
         AND o.candidate_rank = p.candidate_rank
         AND o.country_code = %(iso)s
        JOIN eq.{qident(f"road_graph_nodes_{suffix}")} n ON n.node_id = p.target
        ORDER BY p.crop_code, p.candidate_rank, p.dest_rank
        """,
        conn,
        params={"iso": iso},
    )


def fetch_origins(conn: psycopg.Connection, iso: str) -> pd.DataFrame:
    suffix = iso.lower()
    return pd.read_sql_query(
        f"""
        SELECT o.crop_code, o.candidate_rank, o.lon, o.lat, o.node_id, o.node_distance_m
        FROM eq.{qident(f"crop_origin_nodes_{suffix}")} o
        WHERE o.country_code = %(iso)s
        ORDER BY o.crop_code, o.candidate_rank
        """,
        conn,
        params={"iso": iso},
    )


def fetch_destinations(conn: psycopg.Connection, iso: str) -> pd.DataFrame:
    suffix = iso.lower()
    return pd.read_sql_query(
        f"""
        SELECT DISTINCT p.dest_type, p.dest_rank, p.dest_id, p.dest_name,
               n.lon, n.lat, p.reachable
        FROM tmp_base_reachable_pairs p
        JOIN eq.{qident(f"road_graph_nodes_{suffix}")} n ON n.node_id = p.target
        ORDER BY p.dest_rank, p.dest_name
        """,
        conn,
    )


def plot_route_edges(ax: plt.Axes, route_usage: pd.DataFrame, surface_group: str, color: str, label: str, max_width: float = 3.2) -> None:
    subset = route_usage[route_usage["surface_class"].eq(surface_group)]
    if subset.empty:
        return
    max_count = max(float(subset["route_count"].max()), 1.0)
    for row in subset.itertuples(index=False):
        width = 0.2 + max_width * math.sqrt(float(row.route_count) / max_count)
        ax.plot([row.x1, row.x2], [row.y1, row.y2], color=color, linewidth=width, alpha=0.48, solid_capstyle="round", zorder=3)
    ax.plot([], [], color=color, linewidth=2.8, alpha=0.78, label=label)


def render_map(
    iso: str,
    dest_type: str,
    route_usage: pd.DataFrame,
    pairs: pd.DataFrame,
    origins: pd.DataFrame,
    destinations: pd.DataFrame,
    out_path: Path,
) -> dict[str, object]:
    boundary = load_boundary(iso)
    minx, miny, maxx, maxy = boundary.total_bounds
    pad_x = max((maxx - minx) * 0.06, 0.2)
    pad_y = max((maxy - miny) * 0.06, 0.2)

    route_usage = route_usage.copy()
    route_usage["surface_class"] = route_usage["surface_group"].where(route_usage["surface_group"].eq("paved"), "unpaved_unknown")

    reachable_pairs = pairs[pairs["reachable"].astype(bool)].copy()
    unreachable_pairs = pairs[~pairs["reachable"].astype(bool)].copy()
    reachable_origin_keys = set(zip(reachable_pairs["crop_code"], reachable_pairs["candidate_rank"]))
    origins = origins.copy()
    origins["has_reachable"] = [(row.crop_code, row.candidate_rank) in reachable_origin_keys for row in origins.itertuples(index=False)]

    fig, ax = plt.subplots(figsize=(13.5, 15.5))
    boundary.boundary.plot(ax=ax, color="#202020", linewidth=0.9, zorder=2)

    plot_route_edges(ax, route_usage, "unpaved_unknown", "#d95f02", "route edge: unpaved/unknown")
    plot_route_edges(ax, route_usage, "paved", "#1b9e77", "route edge: paved")

    # Draw unreachable OD as very faint straight lines only to show missing target intent.
    for row in unreachable_pairs.itertuples(index=False):
        ax.plot([row.origin_lon, row.dest_lon], [row.origin_lat, row.dest_lat], color="#bdbdbd", linewidth=0.25, alpha=0.18, linestyle=":", zorder=1)

    crop_colors = {
        "avocado": "#4daf4a",
        "banana": "#ffd92f",
        "plantain": "#a6d854",
        "mango": "#ff7f00",
        "pineapple": "#984ea3",
    }
    for crop, group in origins[origins["has_reachable"]].groupby("crop_code"):
        ax.scatter(
            group["lon"],
            group["lat"],
            s=36,
            color=crop_colors.get(crop, "#555555"),
            edgecolor="#111111",
            linewidth=0.35,
            label=f"crop: {crop}",
            zorder=6,
        )
    unreachable_origins = origins[~origins["has_reachable"]]
    if not unreachable_origins.empty:
        ax.scatter(
            unreachable_origins["lon"],
            unreachable_origins["lat"],
            s=42,
            marker="x",
            color="#737373",
            linewidth=1.1,
            label="crop: no reachable route to this destination type",
            zorder=7,
        )

    dest_marker = {"port": "s", "city_5_100k": "o", "city_100k_plus": "^"}.get(dest_type, "D")
    dest_color = {"port": "#e41a1c", "city_5_100k": "#377eb8", "city_100k_plus": "#08519c"}.get(dest_type, "#444444")
    ax.scatter(
        destinations["lon"],
        destinations["lat"],
        s=80,
        marker=dest_marker,
        color=dest_color,
        edgecolor="white",
        linewidth=0.65,
        label=f"destination: {DEST_LABELS.get(dest_type, dest_type)}",
        zorder=8,
    )
    for row in destinations.itertuples(index=False):
        if dest_type == "port":
            ax.text(row.lon + 0.03, row.lat + 0.02, str(row.dest_name), fontsize=8.0, color="#a50f15", zorder=9)

    ax.set_title(f"{iso} base road routes from crop clusters to {DEST_LABELS.get(dest_type, dest_type)}\nbase graph = noded roads; line width scales with route reuse", fontsize=14)
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(color="#d9d9d9", linewidth=0.4, alpha=0.55)

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
        f"OD={len(pairs):,} | reachable={len(reachable_pairs):,} | unreachable={len(unreachable_pairs):,}\n"
        f"crop clusters reachable={int(origins['has_reachable'].sum()):,}/{len(origins):,}\n"
        f"route edges used={len(route_usage):,}"
    )
    ax.text(
        0.985,
        0.035,
        stats_text,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=9.0,
        bbox={"facecolor": "white", "edgecolor": "#bdbdbd", "alpha": 0.88},
        zorder=10,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)

    return {
        "country_code": iso,
        "dest_type": dest_type,
        "od_rows": int(len(pairs)),
        "reachable_od_rows": int(len(reachable_pairs)),
        "unreachable_od_rows": int(len(unreachable_pairs)),
        "origin_clusters": int(len(origins)),
        "reachable_origin_clusters": int(origins["has_reachable"].sum()),
        "unreachable_origin_clusters": int((~origins["has_reachable"]).sum()),
        "route_edges": int(len(route_usage)),
        "png": str(out_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render base road routes from crop clusters by destination type.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--country", default="GUY")
    parser.add_argument("--dest-types", default="port,city_5_100k,city_100k_plus")
    parser.add_argument("--out-dir", default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "base_routes_by_dest_type"))
    parser.add_argument("--graph-mode", choices=["base", "bridge"], default="bridge")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    iso = args.country.strip().upper()
    dest_types = [part.strip() for part in args.dest_types.split(",") if part.strip()]
    out_dir = Path(args.out_dir)
    results = []
    with psycopg.connect(args.db_url) as conn:
        for dest_type in dest_types:
            log(f"[routes] {iso} dest_type={dest_type}")
            create_route_tables(conn, iso, dest_type, graph_mode=args.graph_mode)
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
                out_dir / f"{iso}_base_routes_to_{dest_type}.png",
            )
            results.append(result)
            log(f"[done] {dest_type} reachable={result['reachable_od_rows']}/{result['od_rows']} png={result['png']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / f"{iso}_base_routes_by_dest_type_manifest.json"
    manifest.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log(f"[done] manifest={manifest}")


if __name__ == "__main__":
    main()
