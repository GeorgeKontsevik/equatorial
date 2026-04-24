"""Render a diagnostic map showing OD points and their nearest mapped graph nodes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString


matplotlib.use("Agg")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot OD-to-node mapping diagnostics for weekly runs.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--overlay-gpkg", type=Path, default=None)
    parser.add_argument("--min-component-nodes", type=int, default=None)
    return parser.parse_args()


def _iter_lines(geometry: object) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [line for line in geometry.geoms if isinstance(line, LineString)]
    return []


def _round_coord(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), 1), round(float(y), 1))


def _build_nodes_edges(roads: gpd.GeoDataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    node_lookup: dict[tuple[float, float], int] = {}
    node_rows: list[dict[str, float | int]] = []
    edge_rows: list[dict[str, int]] = []

    def ensure_node(x: float, y: float) -> int:
        key = _round_coord(x, y)
        node_id = node_lookup.get(key)
        if node_id is None:
            node_id = len(node_lookup)
            node_lookup[key] = node_id
            node_rows.append({"node_id": node_id, "x": key[0], "y": key[1]})
        return node_id

    for geom in roads.geometry:
        for line in _iter_lines(geom):
            coords = list(line.coords)
            for a, b in zip(coords[:-1], coords[1:], strict=False):
                u = ensure_node(a[0], a[1])
                v = ensure_node(b[0], b[1])
                if u != v:
                    edge_rows.append({"u": int(u), "v": int(v)})

    nodes = pd.DataFrame(node_rows)
    edges = pd.DataFrame(edge_rows)
    return nodes, edges


def _component_filter(nodes: pd.DataFrame, edges: pd.DataFrame, min_component_nodes: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if min_component_nodes <= 1:
        return nodes, edges

    parent = np.arange(len(nodes), dtype=np.int64)
    rank = np.zeros(len(nodes), dtype=np.int8)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = int(parent[x])
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for u, v in edges[["u", "v"]].itertuples(index=False):
        union(int(u), int(v))

    roots = np.asarray([find(i) for i in range(len(nodes))], dtype=np.int64)
    comp_roots, comp_counts = np.unique(roots, return_counts=True)
    keep = set(comp_roots[comp_counts >= min_component_nodes].tolist())
    keep_node_mask = np.asarray([int(root) in keep for root in roots], dtype=bool)
    keep_nodes = nodes.loc[keep_node_mask].copy()
    keep_ids = set(keep_nodes["node_id"].astype(int).tolist())
    keep_edges = edges.loc[edges["u"].astype(int).isin(keep_ids) & edges["v"].astype(int).isin(keep_ids)].copy()
    return keep_nodes, keep_edges


def main() -> None:
    args = parse_args()
    summary_path = args.results_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Missing {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    overlay_path = args.overlay_gpkg
    if overlay_path is None:
        overlay_path = Path(summary["overlay_source"])
        if not overlay_path.is_absolute():
            overlay_path = Path(__file__).resolve().parents[2] / overlay_path

    min_component_nodes = int(
        args.min_component_nodes
        if args.min_component_nodes is not None
        else summary.get("min_component_nodes", 1)
    )

    roads = gpd.read_file(overlay_path)
    origins = gpd.read_file(args.results_dir / "origins_used.gpkg")
    cities = gpd.read_file(args.results_dir / "cities_used.gpkg")
    if roads.empty or origins.empty or cities.empty:
        raise RuntimeError("One of required layers is empty.")

    target_crs = roads.estimate_utm_crs()
    roads = roads.to_crs(target_crs)
    origins = origins.to_crs(target_crs)
    cities = cities.to_crs(target_crs)

    nodes, edges = _build_nodes_edges(roads)
    nodes, _edges = _component_filter(nodes, edges, min_component_nodes=min_component_nodes)
    node_gdf = gpd.GeoDataFrame(
        nodes[["node_id"]].copy(),
        geometry=gpd.points_from_xy(nodes["x"], nodes["y"]),
        crs=target_crs,
    )

    origins_map = origins[["origin_id", "geometry"]].sjoin_nearest(
        node_gdf[["node_id", "geometry"]],
        how="left",
        distance_col="snap_dist_m",
    )
    cities_map = cities[["name", "population", "geometry"]].sjoin_nearest(
        node_gdf[["node_id", "geometry"]],
        how="left",
        distance_col="snap_dist_m",
    )

    origin_target = origins_map.merge(
        node_gdf.rename(columns={"geometry": "node_geometry"}),
        on="node_id",
        how="left",
    )
    city_target = cities_map.merge(
        node_gdf.rename(columns={"geometry": "node_geometry"}),
        on="node_id",
        how="left",
    )

    origin_lines = gpd.GeoDataFrame(
        origin_target[["origin_id", "snap_dist_m"]].copy(),
        geometry=[
            LineString([row.geometry, row.node_geometry]) for row in origin_target.itertuples() if row.node_geometry is not None
        ],
        crs=target_crs,
    )
    city_lines = gpd.GeoDataFrame(
        city_target[["name", "snap_dist_m"]].copy(),
        geometry=[
            LineString([row.geometry, row.node_geometry]) for row in city_target.itertuples() if row.node_geometry is not None
        ],
        crs=target_crs,
    )

    fig, ax = plt.subplots(figsize=(12.5, 9.5))
    roads.plot(ax=ax, color="#d7d7d7", linewidth=0.25, alpha=0.7)
    node_gdf.sample(n=min(len(node_gdf), 15000), random_state=42).plot(ax=ax, color="#9e9e9e", markersize=0.5, alpha=0.2)
    if not origin_lines.empty:
        origin_lines.plot(ax=ax, color="#2b83ba", linewidth=1.0, alpha=0.85)
    if not city_lines.empty:
        city_lines.plot(ax=ax, color="#f46d43", linewidth=1.0, alpha=0.75)

    origins.plot(ax=ax, color="#1a9850", markersize=42, marker="o", edgecolor="white", linewidth=0.7, label="Origins")
    cities.plot(ax=ax, color="#d73027", markersize=58, marker="s", edgecolor="white", linewidth=0.8, label="Cities")
    gpd.GeoDataFrame(geometry=origin_target["node_geometry"], crs=target_crs).plot(
        ax=ax, color="#2b83ba", markersize=24, marker="x", label="Origin mapped nodes"
    )
    gpd.GeoDataFrame(geometry=city_target["node_geometry"], crs=target_crs).plot(
        ax=ax, color="#f46d43", markersize=26, marker="x", label="City mapped nodes"
    )

    for row in origins.itertuples():
        ax.annotate(f"O{int(row.origin_id)}", (row.geometry.x, row.geometry.y), xytext=(3, 3), textcoords="offset points", fontsize=8)
    for row in cities.itertuples():
        ax.annotate(str(row.name), (row.geometry.x, row.geometry.y), xytext=(4, 4), textcoords="offset points", fontsize=8)

    ax.set_title("OD -> Nearest Graph Node Mapping")
    ax.set_axis_off()
    ax.legend(loc="lower left", frameon=True)
    fig.tight_layout()
    out_png = args.results_dir / "od_node_mapping.png"
    fig.savefig(out_png, dpi=180)
    plt.close(fig)

    origin_target.drop(columns=["geometry", "node_geometry"]).to_csv(args.results_dir / "origin_node_mapping.csv", index=False)
    city_target.drop(columns=["geometry", "node_geometry"]).to_csv(args.results_dir / "city_node_mapping.csv", index=False)

    report = {
        "od_node_mapping_png": str(out_png),
        "origin_mapping_csv": str(args.results_dir / "origin_node_mapping.csv"),
        "city_mapping_csv": str(args.results_dir / "city_node_mapping.csv"),
        "min_component_nodes": min_component_nodes,
        "n_origins": int(len(origins)),
        "n_cities": int(len(cities)),
    }
    (args.results_dir / "mapping_plot_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
