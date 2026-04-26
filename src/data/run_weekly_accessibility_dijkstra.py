"""Run weekly road accessibility scenarios with scipy sparse Dijkstra."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from src.data.run_weekly_accessibility_pandana import (
    Scenario,
    _compare_values,
    _condition_mask,
    _continuous_effect_values,
    _dense_factor_values,
    _discrete_closure_weeks,
    _effective_surface,
    _filter_rules_with_empty_required_data,
    _filter_small_components,
    _load_thresholds_from_csv,
    _load_thresholds_from_yaml,
    _numeric_factor_columns,
    _parse_iso_date,
    _project_root,
    _resolve_cities,
    _resolve_origins,
    _resolve_overlay_path,
    _round_output_frame,
    _road_factor_values,
    _rule_effect,
    _surface_scope_mask,
    _validate_threshold_factor_inputs,
    _week_starts,
    _build_edges,
    THRESHOLD_LEVELS,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run weekly accessibility scenarios with scipy Dijkstra.")
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--start-date", type=str, default="2024-07-01")
    parser.add_argument("--end-date", type=str, default="2024-09-30")
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--n-origins", type=int, default=5)
    parser.add_argument("--origins-seed", type=int, default=42)
    parser.add_argument("--origins-file", type=Path, default=None)
    parser.add_argument("--cities-file", type=Path, default=None)
    parser.add_argument("--overlay-gpkg", type=Path, default=None)
    parser.add_argument("--isolation-minutes", type=float, default=100000.0)
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    parser.add_argument("--min-component-nodes", type=int, default=500)
    parser.add_argument("--thresholds-csv", type=Path, default=None)
    parser.add_argument("--thresholds-yaml", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser.parse_args()


def _nearest_node_ids(nodes: pd.DataFrame, points: gpd.GeoDataFrame) -> np.ndarray:
    node_ids = nodes["node_id"].to_numpy(dtype=int)
    coords = nodes[["x", "y"]].to_numpy(dtype=float)
    tree = cKDTree(coords)
    query = np.column_stack([points.geometry.x.to_numpy(dtype=float), points.geometry.y.to_numpy(dtype=float)])
    _, idx = tree.query(query, k=1)
    return node_ids[np.asarray(idx, dtype=int)]


def _make_sparse_matrix(edges: pd.DataFrame, minutes: np.ndarray) -> coo_matrix:
    node_count = int(max(edges["u"].max(), edges["v"].max())) + 1
    rows = np.concatenate([edges["u"].to_numpy(dtype=int), edges["v"].to_numpy(dtype=int)])
    cols = np.concatenate([edges["v"].to_numpy(dtype=int), edges["u"].to_numpy(dtype=int)])
    data = np.concatenate([minutes, minutes]).astype(float)
    return coo_matrix((data, (rows, cols)), shape=(node_count, node_count)).tocsr()


def _relpath(path: Path, project_root: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _compute_accessibility_dijkstra(
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
    origins: gpd.GeoDataFrame,
    cities: gpd.GeoDataFrame,
    isolation_minutes: float,
) -> pd.DataFrame:
    if edges.empty:
        result = origins[["origin_id"]].copy()
        result["connected"] = False
        result["access_minutes"] = float(isolation_minutes)
        return result

    origin_nodes = _nearest_node_ids(nodes, origins)
    city_nodes = np.unique(_nearest_node_ids(nodes, cities))
    matrix = _make_sparse_matrix(edges, edges["travel_minutes"].to_numpy(dtype=float))
    dist = dijkstra(matrix, directed=False, indices=origin_nodes, limit=float(isolation_minutes))
    city_dist = dist[:, city_nodes] if city_nodes.size else np.full((len(origin_nodes), 0), np.inf)
    best = np.min(city_dist, axis=1) if city_dist.size else np.full(len(origin_nodes), np.inf)
    connected = np.isfinite(best)

    result = origins[["origin_id"]].copy()
    result["connected"] = connected
    result["access_minutes"] = np.where(connected, best, float(isolation_minutes))
    return result


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    iso3 = args.country_code.upper()
    start_date = _parse_iso_date(args.start_date)
    end_date = _parse_iso_date(args.end_date)
    week_starts = _week_starts(start_date, end_date, args.step_days)

    period_slug = f"{start_date.isoformat()}_to_{end_date.isoformat()}_{args.step_days}d"
    output_dir = (
        args.output_root
        if args.output_root is not None
        else project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"{period_slug}_pop{args.city_threshold}_dijkstra"
    )
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    overlay_path = _resolve_overlay_path(project_root, iso3, args.step_days, args.overlay_gpkg)
    cities_path = _resolve_cities(project_root, iso3, args.city_threshold, args.cities_file)
    origins_path = _resolve_origins(project_root, iso3, args.n_origins, args.origins_seed, output_dir, args.origins_file)

    print(f"[dijkstra] reading overlay={overlay_path}", flush=True)
    roads = gpd.read_file(overlay_path)
    cities = gpd.read_file(cities_path)
    origins = gpd.read_file(origins_path)
    if roads.empty or cities.empty or origins.empty:
        raise RuntimeError("One of required layers is empty (roads/cities/origins).")

    target_crs = roads.estimate_utm_crs()
    if target_crs is None:
        raise RuntimeError("Unable to estimate projected CRS for road network.")
    roads = roads.to_crs(target_crs)
    cities = cities.to_crs(target_crs)
    origins = origins.to_crs(target_crs)
    print(f"[dijkstra] roads={len(roads)} origins={len(origins)} cities={len(cities)} target_crs={target_crs}", flush=True)

    factor_cols = _numeric_factor_columns(roads)
    if args.thresholds_yaml is not None:
        threshold_rules = _load_thresholds_from_yaml(args.thresholds_yaml)
        thresholds_source = f"yaml:{args.thresholds_yaml}"
    elif args.thresholds_csv is not None:
        threshold_rules = _load_thresholds_from_csv(args.thresholds_csv)
        thresholds_source = f"csv:{args.thresholds_csv}"
    else:
        raise ValueError("Provide either --thresholds-yaml or --thresholds-csv.")
    _validate_threshold_factor_inputs(roads, threshold_rules, week_starts)
    threshold_rules, skipped_threshold_rules = _filter_rules_with_empty_required_data(roads, threshold_rules, week_starts)
    if skipped_threshold_rules:
        skipped_factors = ", ".join(f"{row['hazard']}/{row['surface']}:{row['factor']}" for row in skipped_threshold_rules)
        print(f"[dijkstra] skipped_threshold_rules_no_data={skipped_factors}", flush=True)

    print("[dijkstra] building lean edges", flush=True)
    nodes, edges = _build_edges(roads, [])
    nodes, edges, comp_filter_stats = _filter_small_components(nodes, edges, args.min_component_nodes)
    road_ids = edges["road_row_id"].to_numpy(dtype=int)
    unique_road_ids = np.asarray(sorted(roads["road_row_id"].astype(int).unique()), dtype=int)
    road_id_to_dense = {rid: idx for idx, rid in enumerate(unique_road_ids)}
    edge_road_dense = np.asarray([road_id_to_dense[rid] for rid in road_ids], dtype=int)

    road_surface = roads.set_index("road_row_id")["surface_group"]
    edge_surface = pd.Series(np.asarray([road_surface.loc[rid] for rid in road_ids], dtype="object"), index=edges.index, dtype="object")
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unpaved", args.speed_unpaved_kmh, args.speed_paved_kmh)
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unknown", args.speed_unpaved_kmh, base_speed)
    base_minutes = edges["length_m"].to_numpy(dtype=float) / 1000.0 / np.maximum(base_speed, 1.0) * 60.0

    baseline_edges = edges[["u", "v"]].copy()
    baseline_edges["travel_minutes"] = base_minutes
    print(f"[dijkstra] nodes={len(nodes)} edges={len(edges)} baseline", flush=True)
    baseline_access = _compute_accessibility_dijkstra(nodes, baseline_edges, origins, cities, args.isolation_minutes)
    baseline_connected_fraction_before = float(baseline_access["connected"].mean())
    if not bool(baseline_access["connected"].all()):
        n_disconnected = int((~baseline_access["connected"]).sum())
        print(
            f"[dijkstra] baseline_disconnected_origins={n_disconnected}; keeping them at isolation_minutes",
            flush=True,
        )
    baseline_access["week_start"] = pd.NaT
    baseline_access["scenario"] = "baseline"

    scenarios = [Scenario(name="unknown_as_paved", unknown_surface_mode="paved"), Scenario(name="unknown_as_unpaved", unknown_surface_mode="unpaved")]
    weekly_access_rows: list[pd.DataFrame] = []
    weekly_summary_rows: list[dict[str, object]] = []
    factor_rows: list[dict[str, object]] = []
    road_state_rows: list[dict[str, object]] = []

    for scenario in scenarios:
        print(f"[dijkstra] scenario={scenario.name}", flush=True)
        close_until = np.full(len(unique_road_ids), -1, dtype=int)
        effective_surface = _effective_surface(road_surface, scenario.unknown_surface_mode)
        unpaved_road_mask = np.asarray([effective_surface.loc[rid] == "unpaved" for rid in unique_road_ids], dtype=bool)
        road_surface_by_id = road_surface.astype("string").str.lower().fillna("unknown")

        for week_idx, week_start in enumerate(week_starts):
            print(f"[dijkstra] week={week_start.isoformat()} scenario={scenario.name}", flush=True)
            factor_values = _road_factor_values(roads, week_start)
            road_speed_penalty = np.zeros(len(unique_road_ids), dtype=float)
            road_new_closure = np.zeros(len(unique_road_ids), dtype=int)

            for rule in threshold_rules:
                dense_values = _dense_factor_values(roads, factor_values, rule.factor, unique_road_ids, road_id_to_dense)
                applicable_mask = _surface_scope_mask(
                    rule.surface_scope,
                    effective_surface=effective_surface,
                    road_surface=road_surface_by_id,
                    unique_road_ids=unique_road_ids,
                )
                applicable_mask &= _condition_mask(rule, roads, factor_values, unique_road_ids, road_id_to_dense)
                n_applicable = int(applicable_mask.sum())
                interpolated_speed = _continuous_effect_values(rule, dense_values, "speed_penalty_fraction")
                interpolated_damage = _continuous_effect_values(rule, dense_values, "damage_index_fraction")
                interpolated_speed[~applicable_mask] = 0.0
                interpolated_damage[~applicable_mask] = 0.0
                if interpolated_speed.any():
                    road_speed_penalty = np.maximum(road_speed_penalty, interpolated_speed)
                road_new_closure = np.maximum(road_new_closure, _discrete_closure_weeks(rule, dense_values, applicable_mask))
                applicable_speed = interpolated_speed[applicable_mask]
                applicable_damage = interpolated_damage[applicable_mask]
                interpolated_speed_mean = float(np.nanmean(applicable_speed)) if applicable_speed.size else 0.0
                interpolated_speed_max = float(np.nanmax(applicable_speed)) if applicable_speed.size else 0.0
                interpolated_damage_mean = float(np.nanmean(applicable_damage)) if applicable_damage.size else 0.0
                interpolated_damage_max = float(np.nanmax(applicable_damage)) if applicable_damage.size else 0.0

                for level in THRESHOLD_LEVELS:
                    level_threshold = rule.thresholds[level]
                    if not np.isfinite(level_threshold):
                        continue
                    active_mask = applicable_mask & _compare_values(dense_values, rule.direction, level_threshold)
                    n_active = int(active_mask.sum())
                    speed_penalty, closure_weeks = _rule_effect(rule, level)
                    if speed_penalty is not None:
                        if rule.effect_interpolation != "linear":
                            road_speed_penalty[active_mask] = np.maximum(road_speed_penalty[active_mask], speed_penalty)
                    if closure_weeks is not None:
                        if rule.effect_interpolation != "linear":
                            road_new_closure[active_mask] = np.maximum(road_new_closure[active_mask], closure_weeks)
                    effect_type = "none"
                    if closure_weeks is not None:
                        effect_type = "closure"
                    elif speed_penalty is not None and speed_penalty > 0:
                        effect_type = "speed_penalty"
                    factor_rows.append(
                        {
                            "week_start": week_start.isoformat(),
                            "scenario": scenario.name,
                            "factor": rule.factor,
                            "surface_scope": rule.surface_scope,
                            "condition_factor": rule.condition_factor,
                            "condition_operator": rule.condition_operator if rule.condition_factor else None,
                            "condition_value": rule.condition_value if rule.condition_factor else None,
                            "threshold": level,
                            "threshold_value": float(level_threshold),
                            "effect_type": effect_type,
                            "effect_interpolation": rule.effect_interpolation,
                            "speed_penalty_fraction": None if speed_penalty is None else float(speed_penalty),
                            "closure_weeks": None if closure_weeks is None else int(closure_weeks),
                            "interpolated_speed_penalty_mean": interpolated_speed_mean,
                            "interpolated_speed_penalty_max": interpolated_speed_max,
                            "interpolated_damage_index_mean": interpolated_damage_mean,
                            "interpolated_damage_index_max": interpolated_damage_max,
                            "n_applicable_roads": n_applicable,
                            "n_triggered_roads": n_active,
                            "share_triggered_unpaved_roads": None if unpaved_road_mask.sum() == 0 else float(n_active / unpaved_road_mask.sum()),
                            "share_triggered_applicable_roads": None if n_applicable == 0 else float(n_active / n_applicable),
                        }
                    )

            close_until = np.maximum(close_until, week_idx + road_new_closure - 1)
            road_closed = close_until >= week_idx
            edge_closed = road_closed[edge_road_dense]
            edge_penalty = road_speed_penalty[edge_road_dense]

            edge_speed_factor = np.maximum(0.05, 1.0 - edge_penalty)
            edge_minutes = base_minutes / edge_speed_factor
            active_edges = edges.loc[~edge_closed, ["u", "v"]].copy()
            active_edges["travel_minutes"] = edge_minutes[~edge_closed]
            access = _compute_accessibility_dijkstra(nodes, active_edges, origins, cities, args.isolation_minutes)
            access["week_start"] = week_start.isoformat()
            access["scenario"] = scenario.name
            weekly_access_rows.append(access)

            weekly_summary_rows.append(
                {
                    "week_start": week_start.isoformat(),
                    "scenario": scenario.name,
                    "n_origins": int(len(access)),
                    "connected_share": float(access["connected"].mean()),
                    "median_access_minutes": float(access["access_minutes"].median()),
                    "p90_access_minutes": float(access["access_minutes"].quantile(0.9)),
                    "n_closed_roads": int(road_closed.sum()),
                    "share_closed_roads": float(road_closed.mean()),
                }
            )
            road_state_rows.append(
                {
                    "week_start": week_start.isoformat(),
                    "scenario": scenario.name,
                    "n_unpaved_roads_effective": int(unpaved_road_mask.sum()),
                    "n_closed_roads": int(road_closed.sum()),
                }
            )

    weekly_access = pd.concat(weekly_access_rows, ignore_index=True)
    weekly_summary = pd.DataFrame(weekly_summary_rows).sort_values(["scenario", "week_start"]).reset_index(drop=True)
    factor_counts = pd.DataFrame(factor_rows).sort_values(["scenario", "week_start", "factor", "threshold"]).reset_index(drop=True)
    road_state = pd.DataFrame(road_state_rows).sort_values(["scenario", "week_start"]).reset_index(drop=True)

    _round_output_frame(baseline_access).to_csv(output_dir / "baseline_routes.csv", index=False)
    _round_output_frame(weekly_access).to_csv(output_dir / "weekly_accessibility.csv", index=False)
    _round_output_frame(weekly_summary).to_csv(output_dir / "weekly_summary.csv", index=False)
    _round_output_frame(factor_counts).to_csv(output_dir / "weekly_factor_threshold_counts.csv", index=False)
    _round_output_frame(road_state).to_csv(output_dir / "weekly_road_state.csv", index=False)
    origins.to_file(output_dir / "origins_used.gpkg", driver="GPKG")
    cities.to_file(output_dir / "cities_used.gpkg", driver="GPKG")

    summary = {
        "engine": "scipy_dijkstra",
        "country_code": iso3,
        "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "step_days": args.step_days},
        "n_weeks": len(week_starts),
        "n_roads": int(len(roads)),
        "n_nodes": int(len(nodes)),
        "n_edges": int(len(edges)),
        "min_component_nodes": int(args.min_component_nodes),
        "component_filter": comp_filter_stats,
        "n_origins": int(len(origins)),
        "n_cities": int(len(cities)),
        "fixed_origins_source": str(origins_path),
        "cities_source": str(cities_path),
        "overlay_source": str(overlay_path),
        "isolation_minutes": float(args.isolation_minutes),
        "thresholds_source": thresholds_source,
        "baseline_connected_fraction_before_resample": baseline_connected_fraction_before,
        "origins_resampled_for_connectivity": False,
        "baseline_connected_fraction_final": float(baseline_access["connected"].mean()),
        "scenarios": [scenario.name for scenario in scenarios],
        "threshold_rules_count": int(len(threshold_rules)),
        "skipped_threshold_rules": skipped_threshold_rules,
        "outputs": {
            "baseline_routes_csv": _relpath(output_dir / "baseline_routes.csv", project_root),
            "weekly_accessibility_csv": _relpath(output_dir / "weekly_accessibility.csv", project_root),
            "weekly_summary_csv": _relpath(output_dir / "weekly_summary.csv", project_root),
            "weekly_factor_threshold_counts_csv": _relpath(output_dir / "weekly_factor_threshold_counts.csv", project_root),
            "weekly_road_state_csv": _relpath(output_dir / "weekly_road_state.csv", project_root),
            "origins_used_gpkg": _relpath(output_dir / "origins_used.gpkg", project_root),
            "cities_used_gpkg": _relpath(output_dir / "cities_used.gpkg", project_root),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
