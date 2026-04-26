"""Plot weekly road impacts and the subset used by OD shortest paths."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")

from matplotlib.lines import Line2D
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
import yaml
from shapely.geometry import LineString, MultiLineString


THRESHOLD_LEVELS = (
    "speed_reduction_1",
    "speed_reduction_2",
    "speed_reduction_3",
    "catastrophic_temporary",
    "catastrophic_permanent",
)
SPEED_PENALTY_BY_LEVEL = {"speed_reduction_1": 0.10, "speed_reduction_2": 0.25, "speed_reduction_3": 0.40}
CLOSURE_WEEKS_BY_LEVEL = {"catastrophic_temporary": 1, "catastrophic_permanent": 5200}
COLORS = {
    "base": "#d6d6d6",
    "speed": "#f39c12",
    "closure": "#c0392b",
    "baseline_od": "#2c7fb8",
    "weekly_od": "#008b8b",
    "changed_od": "#7b3294",
    "od_detour": "#7b3294",
    "origin": "#1a9850",
    "city": "#111111",
}


@dataclass(slots=True)
class Rule:
    factor: str
    direction: str
    surface_scope: str
    thresholds: dict[str, float]
    condition_factor: str | None = None
    condition_operator: str = "gte"
    condition_value: float | str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot weekly speed-loss and closure impacts on all roads and OD paths.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--overlay-gpkg", type=Path, required=True)
    parser.add_argument("--thresholds-yaml", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    return parser.parse_args()


def _parse_iso(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _week_starts(start: date, end: date, step_days: int) -> list[date]:
    weeks: list[date] = []
    cursor = start
    while cursor <= end:
        weeks.append(cursor)
        cursor += timedelta(days=step_days)
    return weeks


def _week_token(week: date) -> str:
    return week.isoformat().replace("-", "_")


def _weekly_col(factor: str, week: date) -> str | None:
    token = _week_token(week)
    mapping = {
        "chirps_24h_max_weekly_mm": f"chirps_24h_max_week_{token}_mm",
        "era5_tp_daily_sum_weekly_max_mm": f"era5_tp_daily_sum_max_week_{token}_mm",
        "era5_tp_1h_max_weekly_mm_per_h": f"era5_tp_1h_max_week_{token}_mm_per_h",
        "era5_crosswind_10m_weekly_max_m_s": f"era5_crosswind_10m_week_{token}_max",
    }
    return mapping.get(factor)


def _load_rules(path: Path) -> list[Rule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_rules = payload.get("road_hazard_thresholds", payload).get("rules", [])
    rules: list[Rule] = []
    for item in raw_rules:
        thresholds = {}
        for level in THRESHOLD_LEVELS:
            value = item.get("thresholds", {}).get(level)
            thresholds[level] = np.nan if value is None else float(value)
        condition = item.get("condition") or {}
        rules.append(
            Rule(
                factor=str(item["factor"]),
                direction=str(item.get("direction", "gte")).lower(),
                surface_scope=str(item.get("surface_scope", "both")).lower(),
                thresholds=thresholds,
                condition_factor=condition.get("factor"),
                condition_operator=str(condition.get("operator", "gte")).lower(),
                condition_value=condition.get("value"),
            )
        )
    return rules


def _compare(values: np.ndarray, direction: str, threshold: float | str | None) -> np.ndarray:
    if threshold is None:
        return np.zeros(values.shape, dtype=bool)
    numeric = float(threshold)
    finite = np.isfinite(values)
    if direction == "gte":
        return finite & (values >= numeric)
    if direction == "gt":
        return finite & (values > numeric)
    if direction == "lte":
        return finite & (values <= numeric)
    if direction == "lt":
        return finite & (values < numeric)
    if direction == "eq":
        return finite & (values == numeric)
    raise ValueError(f"Unsupported comparison: {direction}")


def _effective_surface(surface: pd.Series, unknown_mode: str) -> pd.Series:
    values = surface.astype("string").str.lower().fillna("unknown")
    return values.where(values != "unknown", unknown_mode)


def _surface_mask(scope: str, effective_surface: pd.Series, road_surface: pd.Series) -> np.ndarray:
    normalized = scope.lower().replace("-", "_")
    if normalized in {"both", "all", "any", "*"}:
        return np.ones(len(effective_surface), dtype=bool)
    source = effective_surface
    target = normalized
    if normalized.startswith("actual_"):
        source = road_surface.astype("string").str.lower().fillna("unknown")
        target = normalized.removeprefix("actual_")
    if normalized.startswith("effective_"):
        target = normalized.removeprefix("effective_")
    return source.eq(target).to_numpy(dtype=bool)


def _impact_for_week(roads: gpd.GeoDataFrame, rules: list[Rule], week: date, scenario: str) -> pd.DataFrame:
    unknown_mode = "paved" if scenario == "unknown_as_paved" else "unpaved"
    road_surface = roads["surface_group"].astype("string").str.lower().fillna("unknown")
    effective = _effective_surface(road_surface, unknown_mode)
    speed_penalty = np.zeros(len(roads), dtype=float)
    closed = np.zeros(len(roads), dtype=bool)

    for rule in rules:
        col = _weekly_col(rule.factor, week)
        if col is None or col not in roads.columns:
            continue
        values = pd.to_numeric(roads[col], errors="coerce").to_numpy(dtype=float)
        applicable = _surface_mask(rule.surface_scope, effective, road_surface)
        if rule.condition_factor:
            cond_values = pd.to_numeric(roads[rule.condition_factor], errors="coerce").to_numpy(dtype=float)
            applicable &= _compare(cond_values, rule.condition_operator, rule.condition_value)
        for level, threshold in rule.thresholds.items():
            if not np.isfinite(threshold):
                continue
            active = applicable & _compare(values, rule.direction, threshold)
            if level in SPEED_PENALTY_BY_LEVEL:
                speed_penalty[active] = np.maximum(speed_penalty[active], SPEED_PENALTY_BY_LEVEL[level])
            if level in CLOSURE_WEEKS_BY_LEVEL:
                closed |= active

    return pd.DataFrame(
        {
            "road_row_id": roads["road_row_id"].astype(int).to_numpy(),
            "speed_penalty": speed_penalty,
            "closed": closed,
            "impact_class": np.where(closed, "closure", np.where(speed_penalty > 0, "speed", "none")),
        }
    )


def _iter_lines(geometry: object) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [g for g in geometry.geoms if isinstance(g, LineString)]
    return []


def _round_xy(x: float, y: float) -> tuple[float, float]:
    return round(float(x), 1), round(float(y), 1)


def _build_graph_edges(roads: gpd.GeoDataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    node_lookup: dict[tuple[float, float], int] = {}
    nodes: list[dict[str, float | int]] = []
    edges: list[dict[str, float | int | str]] = []

    def node_id(x: float, y: float) -> int:
        key = _round_xy(x, y)
        existing = node_lookup.get(key)
        if existing is not None:
            return existing
        idx = len(node_lookup)
        node_lookup[key] = idx
        nodes.append({"node_id": idx, "x": key[0], "y": key[1]})
        return idx

    for row in roads.itertuples(index=False):
        road_id = int(row.road_row_id)
        surface = str(row.surface_group).lower()
        for line in _iter_lines(row.geometry):
            coords = list(line.coords)
            for start, end in zip(coords[:-1], coords[1:], strict=False):
                u = node_id(start[0], start[1])
                v = node_id(end[0], end[1])
                length_m = float(((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5)
                if length_m > 0:
                    edges.append({"u": u, "v": v, "length_m": length_m, "road_row_id": road_id, "surface_group": surface})
    return pd.DataFrame(nodes), pd.DataFrame(edges)


def _filter_small_components(nodes: pd.DataFrame, edges: pd.DataFrame, min_component_nodes: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if min_component_nodes <= 1:
        return nodes, edges
    parent = np.arange(int(nodes["node_id"].max()) + 1, dtype=np.int64)
    rank = np.zeros(len(parent), dtype=np.int8)

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
    node_ids = nodes["node_id"].to_numpy(dtype=int)
    roots = np.asarray([find(int(node_id)) for node_id in node_ids], dtype=np.int64)
    counts = pd.Series(roots).value_counts()
    keep_roots = set(counts.loc[counts >= min_component_nodes].index.astype(int).tolist())
    keep_nodes = nodes.loc[[int(root) in keep_roots for root in roots]].copy()
    keep_node_ids = set(keep_nodes["node_id"].astype(int).tolist())
    keep_edges = edges.loc[edges["u"].astype(int).isin(keep_node_ids) & edges["v"].astype(int).isin(keep_node_ids)].copy()
    return keep_nodes.reset_index(drop=True), keep_edges.reset_index(drop=True)


def _nearest_nodes(points: gpd.GeoDataFrame, nodes: pd.DataFrame) -> np.ndarray:
    from scipy.spatial import cKDTree

    tree = cKDTree(nodes[["x", "y"]].to_numpy(dtype=float))
    _, idx = tree.query(np.column_stack([points.geometry.x.to_numpy(), points.geometry.y.to_numpy()]), k=1)
    return nodes.iloc[idx]["node_id"].to_numpy(dtype=int)


def _make_sparse(edges: pd.DataFrame, minutes: np.ndarray, closed: np.ndarray | None = None) -> tuple[coo_matrix, dict[tuple[int, int], tuple[int, float]]]:
    keep = np.ones(len(edges), dtype=bool) if closed is None else ~closed
    kept = edges.loc[keep].copy()
    kept_minutes = minutes[keep]
    n = int(max(edges["u"].max(), edges["v"].max())) + 1
    rows = np.concatenate([kept["u"].to_numpy(dtype=int), kept["v"].to_numpy(dtype=int)])
    cols = np.concatenate([kept["v"].to_numpy(dtype=int), kept["u"].to_numpy(dtype=int)])
    data = np.concatenate([kept_minutes, kept_minutes])
    matrix = coo_matrix((data, (rows, cols)), shape=(n, n)).tocsr()

    edge_lookup: dict[tuple[int, int], tuple[int, float]] = {}
    for r, minute in zip(kept.itertuples(index=False), kept_minutes, strict=False):
        for key in ((int(r.u), int(r.v)), (int(r.v), int(r.u))):
            current = edge_lookup.get(key)
            if current is None or minute < current[1]:
                edge_lookup[key] = (int(r.road_row_id), float(r.length_m))
    return matrix, edge_lookup


def _path_to_roads(predecessors: np.ndarray, origin_pos: int, target: int, edge_lookup: dict[tuple[int, int], tuple[int, float]]) -> tuple[set[int], float]:
    roads: set[int] = set()
    length_m = 0.0
    cursor = int(target)
    while True:
        prev = int(predecessors[origin_pos, cursor])
        if prev < 0:
            break
        item = edge_lookup.get((prev, cursor))
        if item is not None:
            road_id, segment_m = item
            roads.add(road_id)
            length_m += segment_m
        cursor = prev
    return roads, length_m / 1000.0


def _od_paths(
    edges: pd.DataFrame,
    minutes: np.ndarray,
    origin_nodes: np.ndarray,
    city_nodes: np.ndarray,
    closed_edge_mask: np.ndarray | None = None,
) -> tuple[pd.DataFrame, set[int]]:
    matrix, edge_lookup = _make_sparse(edges, minutes, closed_edge_mask)
    dist, pred = dijkstra(matrix, directed=False, indices=origin_nodes, return_predecessors=True)
    rows: list[dict[str, object]] = []
    all_roads: set[int] = set()
    for i, origin_node in enumerate(origin_nodes):
        city_dist = dist[i, city_nodes]
        finite = np.isfinite(city_dist)
        if not finite.any():
            rows.append({"origin_pos": i, "origin_node": int(origin_node), "city_node": -1, "minutes": np.inf, "length_km": np.inf, "path_road_ids": set()})
            continue
        city_node = int(city_nodes[np.nanargmin(city_dist)])
        path_roads, length_km = _path_to_roads(pred, i, city_node, edge_lookup)
        all_roads |= path_roads
        rows.append(
            {
                "origin_pos": i,
                "origin_node": int(origin_node),
                "city_node": city_node,
                "minutes": float(np.nanmin(city_dist)),
                "length_km": float(length_km),
                "path_road_ids": path_roads,
            }
        )
    return pd.DataFrame(rows), all_roads


def _plot_map(
    roads: gpd.GeoDataFrame,
    origins: gpd.GeoDataFrame,
    cities: gpd.GeoDataFrame,
    impact: pd.DataFrame,
    out_path: Path,
    title: str,
    *,
    baseline_roads: set[int] | None = None,
    weekly_roads: set[int] | None = None,
    changed_roads: set[int] | None = None,
    detour_roads: set[int] | None = None,
) -> None:
    plot_roads = roads.merge(impact[["road_row_id", "impact_class"]], on="road_row_id", how="left")
    fig, ax = plt.subplots(figsize=(9.2, 8.4))
    roads.plot(ax=ax, color=COLORS["base"], linewidth=0.12, alpha=0.42, zorder=1)
    for cls, color, width, label in [
        ("speed", COLORS["speed"], 0.70, "speed reduction"),
        ("closure", COLORS["closure"], 1.05, "closed"),
    ]:
        part = plot_roads.loc[plot_roads["impact_class"].eq(cls)]
        if not part.empty:
            part.plot(ax=ax, color=color, linewidth=width, alpha=0.95, zorder=3 if cls == "speed" else 4)

    if baseline_roads is not None:
        baseline_part = roads.loc[roads["road_row_id"].astype(int).isin(baseline_roads)]
        if not baseline_part.empty:
            baseline_part.plot(ax=ax, color=COLORS["baseline_od"], linewidth=1.05, alpha=0.82, zorder=5)
    if weekly_roads is not None:
        weekly_part = roads.loc[roads["road_row_id"].astype(int).isin(weekly_roads)]
        if not weekly_part.empty:
            weekly_part.plot(ax=ax, color=COLORS["weekly_od"], linewidth=1.28, alpha=0.94, zorder=6)
    if changed_roads:
        changed_part = roads.loc[roads["road_row_id"].astype(int).isin(changed_roads)]
        if not changed_part.empty:
            changed_part.plot(ax=ax, color=COLORS["changed_od"], linewidth=1.65, alpha=0.98, zorder=7)
    if detour_roads:
        detour_part = roads.loc[roads["road_row_id"].astype(int).isin(detour_roads)]
        if not detour_part.empty:
            detour_part.plot(ax=ax, color=COLORS["closure"], linewidth=1.95, alpha=0.98, zorder=8)

    origins.plot(ax=ax, color=COLORS["origin"], markersize=28, edgecolor="white", linewidth=0.45, zorder=7)
    cities.plot(ax=ax, color=COLORS["city"], markersize=36, marker="s", edgecolor="white", linewidth=0.45, zorder=8)
    ax.set_axis_off()
    ax.set_title(title, fontsize=12)
    handles = [
        Line2D([0], [0], color=COLORS["speed"], lw=3, label="speed reduction"),
        Line2D([0], [0], color=COLORS["closure"], lw=3, label="closed road"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor=COLORS["origin"], markeredgecolor="white", markersize=7, label="origin"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor=COLORS["city"], markeredgecolor="white", markersize=7, label="destination"),
    ]
    if baseline_roads is not None:
        handles.append(Line2D([0], [0], color=COLORS["baseline_od"], lw=3, label="baseline OD path"))
    if weekly_roads is not None:
        handles.append(Line2D([0], [0], color=COLORS["weekly_od"], lw=3, label="weekly OD path"))
    if changed_roads:
        handles.append(Line2D([0], [0], color=COLORS["changed_od"], lw=3, label="path changed"))
    if detour_roads:
        handles.append(Line2D([0], [0], color=COLORS["closure"], lw=4, label="longer because of closure"))
    ax.legend(handles=handles, loc="lower left", fontsize=8, frameon=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _format_week_axis(ax: plt.Axes, labels: list[str]) -> None:
    x = np.arange(len(labels))
    ax.set_xticks(x, labels, rotation=30, ha="right")
    ax.grid(axis="y", color="#dddddd", linewidth=0.7, alpha=0.7)


def _plot_count_metric(
    summary: pd.DataFrame,
    out_path: Path,
    *,
    metric: str,
    ylabel: str,
    title: str,
    color: str,
    line_metric: str | None = None,
    line_label: str | None = None,
) -> None:
    scenarios = summary["scenario"].drop_duplicates().tolist()
    labels = summary["week_start"].drop_duplicates().tolist()
    fig, axes = plt.subplots(len(scenarios), 1, figsize=(10.5, 3.2 * len(scenarios)), sharex=True)
    if len(scenarios) == 1:
        axes = [axes]
    ymax = float(summary[[metric] + ([line_metric] if line_metric else [])].max().max())
    ymax = 1.0 if not np.isfinite(ymax) or ymax <= 0 else ymax * 1.12
    for ax, scenario in zip(axes, scenarios, strict=False):
        sub = summary.loc[summary["scenario"].eq(scenario)].copy()
        x = np.arange(len(sub))
        ax.bar(x, sub[metric], color=color, label=ylabel)
        if line_metric:
            ax.plot(x, sub[line_metric], color="#023047", marker="o", linewidth=2.0, label=line_label or line_metric)
        ax.set_title(scenario)
        ax.set_ylabel(ylabel)
        ax.set_ylim(0, ymax)
        _format_week_axis(ax, labels)
        ax.legend(loc="upper left")
    fig.suptitle(title, y=0.995, fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _boxplot_values(route_df: pd.DataFrame, scenario: str, week: str, metric: str, *, changed_only: bool = False) -> np.ndarray:
    subset = route_df.loc[route_df["scenario"].eq(scenario) & route_df["week_start"].eq(week)].copy()
    if changed_only:
        subset = subset.loc[subset["path_changed"].astype(bool)]
    values = pd.to_numeric(subset[metric], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna().to_numpy(dtype=float)
    return values if values.size else np.asarray([0.0], dtype=float)


def _plot_boxplot_metric(
    route_df: pd.DataFrame,
    out_path: Path,
    *,
    metric: str,
    ylabel: str,
    title: str,
    color: str,
    changed_only: bool = False,
) -> None:
    scenarios = route_df["scenario"].drop_duplicates().tolist()
    labels = route_df["week_start"].drop_duplicates().tolist()
    fig, axes = plt.subplots(len(scenarios), 1, figsize=(10.5, 3.4 * len(scenarios)), sharex=True)
    if len(scenarios) == 1:
        axes = [axes]
    all_values = [
        _boxplot_values(route_df, scenario, week, metric, changed_only=changed_only)
        for scenario in scenarios
        for week in labels
    ]
    ymax = max(float(np.nanmax(values)) for values in all_values if values.size)
    ymin = min(float(np.nanmin(values)) for values in all_values if values.size)
    if not np.isfinite(ymax) or not np.isfinite(ymin):
        ymin, ymax = 0.0, 1.0
    pad = max((ymax - ymin) * 0.12, 1.0)
    for ax, scenario in zip(axes, scenarios, strict=False):
        data = [_boxplot_values(route_df, scenario, week, metric, changed_only=changed_only) for week in labels]
        bp = ax.boxplot(data, patch_artist=True, showfliers=True, tick_labels=labels)
        for box in bp["boxes"]:
            box.set(facecolor=color, alpha=0.72, edgecolor="#333333")
        for median in bp["medians"]:
            median.set(color="#111111", linewidth=1.6)
        ax.set_title(scenario)
        ax.set_ylabel(ylabel)
        ax.set_ylim(ymin - pad, ymax + pad)
        _format_week_axis(ax, labels)
    fig.suptitle(title, y=0.995, fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _plot_timeline(summary: pd.DataFrame, route_df: pd.DataFrame, out_path: Path) -> None:
    scenarios = summary["scenario"].drop_duplicates().tolist()
    labels = summary["week_start"].drop_duplicates().tolist()
    fig, axes = plt.subplots(4, len(scenarios), figsize=(6.6 * len(scenarios), 12.2), sharex="col")
    if len(scenarios) == 1:
        axes = np.asarray(axes).reshape(4, 1)

    all_time = pd.to_numeric(route_df["delta_minutes"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    changed_time = pd.to_numeric(route_df.loc[route_df["path_changed"].astype(bool), "delta_minutes"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    changed_length = pd.to_numeric(route_df.loc[route_df["path_changed"].astype(bool), "length_delta_km"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    time_ymax = float(pd.concat([all_time, changed_time]).max()) if not all_time.empty or not changed_time.empty else 1.0
    length_ymax = float(changed_length.max()) if not changed_length.empty else 1.0
    length_ymin = float(changed_length.min()) if not changed_length.empty else 0.0
    count_ymax = float(summary[["n_path_changed_od", "n_closure_length_increase_od"]].max().max())
    time_ymax = 1.0 if not np.isfinite(time_ymax) or time_ymax <= 0 else time_ymax * 1.12
    length_pad = max((length_ymax - length_ymin) * 0.12, 1.0)
    count_ymax = 1.0 if not np.isfinite(count_ymax) or count_ymax <= 0 else max(count_ymax * 1.25, 1.0)

    for col, scenario in enumerate(scenarios):
        sub = summary.loc[summary["scenario"].eq(scenario)].copy()
        x = np.arange(len(sub))

        ax = axes[0, col]
        data = [_boxplot_values(route_df, scenario, week, "delta_minutes") for week in labels]
        bp = ax.boxplot(data, patch_artist=True, showfliers=True, tick_labels=labels)
        for box in bp["boxes"]:
            box.set(facecolor="#8ecae6", alpha=0.72, edgecolor="#333333")
        for median in bp["medians"]:
            median.set(color="#111111", linewidth=1.6)
        ax.set_title(scenario)
        ax.set_ylabel("OD time increase, min")
        ax.set_ylim(0, time_ymax)

        ax = axes[1, col]
        data = [_boxplot_values(route_df, scenario, week, "delta_minutes", changed_only=True) for week in labels]
        bp = ax.boxplot(data, patch_artist=True, showfliers=True, tick_labels=labels)
        for box in bp["boxes"]:
            box.set(facecolor="#b07cc6", alpha=0.72, edgecolor="#333333")
        for median in bp["medians"]:
            median.set(color="#111111", linewidth=1.6)
        ax.set_ylabel("changed-path time increase, min")
        ax.set_ylim(0, time_ymax)

        ax = axes[2, col]
        data = [_boxplot_values(route_df, scenario, week, "length_delta_km", changed_only=True) for week in labels]
        bp = ax.boxplot(data, patch_artist=True, showfliers=True, tick_labels=labels)
        for box in bp["boxes"]:
            box.set(facecolor="#b07cc6", alpha=0.72, edgecolor="#333333")
        for median in bp["medians"]:
            median.set(color="#111111", linewidth=1.6)
        ax.set_ylabel("changed-path length delta, km")
        ax.set_ylim(length_ymin - length_pad, length_ymax + length_pad)

        ax = axes[3, col]
        ax.bar(x, sub["n_path_changed_od"], color=COLORS["changed_od"], label="path changed")
        ax.bar(x, sub["n_closure_length_increase_od"], color=COLORS["closure"], label="longer because of closure")
        ax.set_ylabel("OD pair count")
        ax.set_ylim(0, count_ymax)
        ax.legend(loc="upper left")
        _format_week_axis(ax, labels)

        for row in range(4):
            axes[row, col].grid(axis="y", color="#dddddd", linewidth=0.7, alpha=0.7)

    fig.suptitle("OD impact distributions", y=0.995, fontsize=14)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)

    _plot_boxplot_metric(
        route_df,
        out_path.with_name("impact_timeline_time_increase.png"),
        metric="delta_minutes",
        ylabel="minutes",
        title="OD time increase distribution",
        color="#8ecae6",
    )
    _plot_count_metric(
        summary,
        out_path.with_name("impact_timeline_path_changed.png"),
        metric="n_path_changed_od",
        ylabel="OD pair count",
        title="OD paths changed",
        color=COLORS["changed_od"],
    )
    _plot_boxplot_metric(
        route_df,
        out_path.with_name("impact_timeline_path_changed_time_delta.png"),
        metric="delta_minutes",
        ylabel="minutes",
        title="Time increase distribution where OD path changed",
        color="#b07cc6",
        changed_only=True,
    )
    _plot_boxplot_metric(
        route_df,
        out_path.with_name("impact_timeline_path_changed_length_delta.png"),
        metric="length_delta_km",
        ylabel="km",
        title="Length delta distribution where OD path changed",
        color="#b07cc6",
        changed_only=True,
    )
    _plot_count_metric(
        summary,
        out_path.with_name("impact_timeline_closure_length_increase.png"),
        metric="n_closure_length_increase_od",
        ylabel="OD pair count",
        title="OD routes longer because of closures",
        color=COLORS["closure"],
    )


def _round_numeric(frame: pd.DataFrame) -> pd.DataFrame:
    rounded = frame.copy()
    numeric_cols = rounded.select_dtypes(include=[np.number]).columns
    rounded[numeric_cols] = rounded[numeric_cols].round(1)
    return rounded


def main() -> None:
    args = parse_args()
    summary_json = json.loads((args.results_dir / "summary.json").read_text(encoding="utf-8"))
    period = summary_json["period"]
    weeks = _week_starts(_parse_iso(period["start_date"]), _parse_iso(period["end_date"]), int(period["step_days"]))
    scenarios = summary_json["scenarios"]
    out_dir = args.out_dir or (args.results_dir / "impact_maps")
    out_dir.mkdir(parents=True, exist_ok=True)

    roads_wgs = gpd.read_file(args.overlay_gpkg)
    target_crs = roads_wgs.estimate_utm_crs()
    if target_crs is None:
        raise RuntimeError("Could not estimate projected CRS for OD path reconstruction.")
    roads = roads_wgs.to_crs(target_crs)
    origins = gpd.read_file(args.results_dir / "origins_used.gpkg").to_crs(target_crs)
    cities = gpd.read_file(args.results_dir / "cities_used.gpkg").to_crs(target_crs)
    rules = _load_rules(args.thresholds_yaml)

    nodes, edges = _build_graph_edges(roads)
    min_component_nodes = int(summary_json.get("min_component_nodes", 500))
    nodes, edges = _filter_small_components(nodes, edges, min_component_nodes)
    origin_nodes = _nearest_nodes(origins, nodes)
    city_nodes = np.unique(_nearest_nodes(cities, nodes))
    base_speed = np.where(edges["surface_group"].astype("string").str.lower().eq("unpaved"), args.speed_unpaved_kmh, args.speed_paved_kmh)
    base_speed = np.where(edges["surface_group"].astype("string").str.lower().eq("unknown"), args.speed_unpaved_kmh, base_speed)
    base_minutes = edges["length_m"].to_numpy(dtype=float) / 1000.0 / np.maximum(base_speed, 1.0) * 60.0
    baseline_routes, baseline_road_ids = _od_paths(edges, base_minutes, origin_nodes, city_nodes)
    baseline_routes = baseline_routes.rename(columns={"minutes": "baseline_minutes", "length_km": "baseline_length_km"})

    route_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    road_ids_arr = edges["road_row_id"].astype(int).to_numpy()

    for scenario in scenarios:
        for week in weeks:
            impact = _impact_for_week(roads, rules, week, scenario)
            impact_by_road = impact.set_index("road_row_id")
            edge_closed = impact_by_road.loc[road_ids_arr, "closed"].to_numpy(dtype=bool)
            edge_penalty = impact_by_road.loc[road_ids_arr, "speed_penalty"].to_numpy(dtype=float)
            weekly_minutes = base_minutes / np.maximum(0.05, 1.0 - edge_penalty)
            weekly_routes, weekly_road_ids = _od_paths(edges, weekly_minutes, origin_nodes, city_nodes, edge_closed)
            joined = weekly_routes.merge(
                baseline_routes[["origin_pos", "baseline_minutes", "baseline_length_km", "path_road_ids"]],
                on="origin_pos",
                how="left",
                suffixes=("", "_baseline"),
            )
            closed_road_ids = set(impact.loc[impact["closed"], "road_row_id"].astype(int).tolist())
            changed_roads: set[int] = set()
            detour_roads: set[int] = set()
            current_route_rows: list[dict[str, object]] = []
            for row in joined.itertuples(index=False):
                length_delta = float(row.length_km - row.baseline_length_km)
                baseline_path = set(row.path_road_ids_baseline)
                weekly_path = set(row.path_road_ids)
                path_changed = baseline_path != weekly_path
                closure_length_increase = bool(length_delta > 0.01 and bool(baseline_path & closed_road_ids))
                if path_changed:
                    changed_roads |= weekly_path ^ baseline_path
                if closure_length_increase:
                    detour_roads |= weekly_path
                current_route_rows.append(
                    {
                        "week_start": week.isoformat(),
                        "scenario": scenario,
                        "origin_pos": int(row.origin_pos),
                        "baseline_minutes": float(row.baseline_minutes),
                        "weekly_minutes": float(row.minutes),
                        "delta_minutes": float(row.minutes - row.baseline_minutes),
                        "baseline_length_km": float(row.baseline_length_km),
                        "weekly_length_km": float(row.length_km),
                        "length_delta_km": length_delta,
                        "path_changed": path_changed,
                        "closure_length_increase": closure_length_increase,
                    }
                )
            route_rows.extend(current_route_rows)
            changed_route_rows = [row for row in current_route_rows if row["path_changed"]]
            summary_rows.append(
                {
                    "week_start": week.isoformat(),
                    "scenario": scenario,
                    "n_origins": int(len(current_route_rows)),
                    "n_speed_roads": int((impact["impact_class"] == "speed").sum()),
                    "n_closed_roads": int((impact["impact_class"] == "closure").sum()),
                    "n_od_path_roads": int(len(weekly_road_ids)),
                    "n_od_speed_roads": int(impact.loc[impact["road_row_id"].isin(weekly_road_ids), "impact_class"].eq("speed").sum()),
                    "n_od_closed_roads": int(impact.loc[impact["road_row_id"].isin(weekly_road_ids), "impact_class"].eq("closure").sum()),
                    "n_path_changed_od": int(sum(row["path_changed"] for row in current_route_rows)),
                    "n_closure_length_increase_od": int(sum(row["closure_length_increase"] for row in current_route_rows)),
                    "median_delta_minutes": float(np.nanmedian([row["delta_minutes"] for row in current_route_rows])),
                    "mean_delta_minutes": float(np.nanmean([row["delta_minutes"] for row in current_route_rows])),
                    "max_delta_minutes": float(np.nanmax([row["delta_minutes"] for row in current_route_rows])),
                    "median_length_delta_km": float(np.nanmedian([row["length_delta_km"] for row in current_route_rows])),
                    "max_length_delta_km": float(np.nanmax([row["length_delta_km"] for row in current_route_rows])),
                    "path_changed_median_delta_minutes": 0.0
                    if not changed_route_rows
                    else float(np.nanmedian([row["delta_minutes"] for row in changed_route_rows])),
                    "path_changed_max_delta_minutes": 0.0
                    if not changed_route_rows
                    else float(np.nanmax([row["delta_minutes"] for row in changed_route_rows])),
                    "path_changed_median_length_delta_km": 0.0
                    if not changed_route_rows
                    else float(np.nanmedian([row["length_delta_km"] for row in changed_route_rows])),
                    "path_changed_max_length_delta_km": 0.0
                    if not changed_route_rows
                    else float(np.nanmax([row["length_delta_km"] for row in changed_route_rows])),
                }
            )

            stem = f"{week.isoformat()}__{scenario}"
            _plot_map(
                roads,
                origins,
                cities,
                impact,
                out_dir / f"all_roads__{stem}.png",
                f"{week.isoformat()} {scenario}: speed reductions vs closures",
            )
            _plot_map(
                roads,
                origins,
                cities,
                impact,
                out_dir / f"od_paths__{stem}.png",
                f"{week.isoformat()} {scenario}: impacts on OD shortest paths",
                baseline_roads=baseline_road_ids,
                weekly_roads=weekly_road_ids,
                changed_roads=changed_roads,
                detour_roads=detour_roads,
            )

    route_df = pd.DataFrame(route_rows)
    summary_df = pd.DataFrame(summary_rows).sort_values(["scenario", "week_start"])
    _round_numeric(route_df).to_csv(out_dir / "od_route_length_delta.csv", index=False)
    _round_numeric(summary_df).to_csv(out_dir / "impact_map_summary.csv", index=False)
    _plot_timeline(summary_df, route_df, out_dir / "impact_timeline.png")
    report = {
        "out_dir": str(out_dir),
        "n_maps": int(len(list(out_dir.glob("*.png")))),
        "route_delta_csv": str(out_dir / "od_route_length_delta.csv"),
        "impact_summary_csv": str(out_dir / "impact_map_summary.csv"),
    }
    (out_dir / "plot_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
