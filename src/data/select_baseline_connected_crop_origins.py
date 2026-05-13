"""Select baseline-connected SPAM crop origins for the active accessibility run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

matplotlib.use("Agg")

from src.data.run_road_monthly_scenarios import _country_layers
from src.data.run_weekly_accessibility_dijkstra import (
    _build_edges,
    _compute_accessibility_dijkstra,
    _filter_points_strictly_inside_country,
    _filter_small_components,
)
from src.data.run_weekly_accessibility_pandana import _resolve_cities, _round_output_frame, _load_overlay_frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select baseline-connected crop origins from SPAM candidates.")
    parser.add_argument("--country-code", type=str, required=True)
    parser.add_argument("--candidate-gpkg", type=Path, required=True)
    parser.add_argument("--overlay-gpkg", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--top-n-per-crop", type=int, default=3)
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    parser.add_argument("--min-component-nodes", type=int, default=500)
    parser.add_argument("--isolation-minutes", type=float, default=100000.0)
    return parser.parse_args()


def _relpath(path: Path, project_root: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def select_baseline_connected_origins(
    *,
    project_root: Path,
    iso3: str,
    candidate_gpkg: Path,
    overlay_gpkg: Path,
    out_dir: Path,
    city_threshold: int,
    top_n_per_crop: int,
    speed_paved_kmh: float,
    speed_unpaved_kmh: float,
    min_component_nodes: int,
    isolation_minutes: float,
) -> Path:
    print("[origin-select] selecting baseline-connected crop origins", flush=True)
    candidates = gpd.read_file(candidate_gpkg)
    roads = _load_overlay_frame(
        overlay_gpkg,
        project_root=project_root,
        iso3=iso3,
        require_linear_geometry=True,
    )
    cities = gpd.read_file(_resolve_cities(project_root, iso3, city_threshold, None))
    candidates, candidate_geometry_report = _filter_points_strictly_inside_country(
        project_root=project_root,
        iso3=iso3,
        points=candidates,
        label="candidate_origins",
        output_dir=out_dir,
    )
    cities, city_geometry_report = _filter_points_strictly_inside_country(
        project_root=project_root,
        iso3=iso3,
        points=cities,
        label="candidate_cities",
        output_dir=out_dir,
    )
    target_crs = roads.estimate_utm_crs()
    if target_crs is None:
        raise RuntimeError("Unable to estimate projected CRS for road network.")

    roads_proj = roads.to_crs(target_crs)
    candidates_proj = candidates.to_crs(target_crs)
    cities_proj = cities.to_crs(target_crs)
    nodes, edges = _build_edges(roads_proj, [])
    nodes, edges, component_stats = _filter_small_components(nodes, edges, min_component_nodes)

    road_surface = roads_proj.set_index("road_row_id")["surface_group"]
    road_ids = edges["road_row_id"].to_numpy(dtype=int)
    edge_surface = pd.Series(np.asarray([road_surface.loc[rid] for rid in road_ids], dtype="object"), index=edges.index, dtype="object")
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unpaved", speed_unpaved_kmh, speed_paved_kmh)
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unknown", speed_unpaved_kmh, base_speed)
    baseline_edges = edges[["u", "v"]].copy()
    baseline_edges["travel_minutes"] = edges["length_m"].to_numpy(dtype=float) / 1000.0 / np.maximum(base_speed, 1.0) * 60.0

    access = _compute_accessibility_dijkstra(nodes, baseline_edges, candidates_proj, cities_proj, isolation_minutes)
    candidates_access = candidates.copy().merge(access[["origin_id", "connected", "access_minutes"]], on="origin_id", how="left")

    selected_parts: list[gpd.GeoDataFrame] = []
    warnings: list[dict[str, object]] = []
    for crop_code, part in candidates_access.sort_values(["crop_code", "crop_rank"]).groupby("crop_code", sort=True):
        connected = part.loc[part["connected"].astype(bool)].head(top_n_per_crop).copy()
        if len(connected) < top_n_per_crop:
            warnings.append({"crop_code": crop_code, "connected_candidates": int(len(connected)), "target": int(top_n_per_crop)})
            print(f"[origin-select] warning crop={crop_code} connected={len(connected)} target={top_n_per_crop}", flush=True)
        selected_parts.append(connected)

    selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else candidates_access.iloc[0:0].copy()
    if selected.empty:
        raise RuntimeError("No baseline-connected crop origins were selected.")
    selected["source_crop_rank"] = selected["crop_rank"].astype(int)
    selected["crop_rank"] = selected.groupby("crop_code").cumcount() + 1
    selected["origin_id"] = np.arange(len(selected), dtype=int)
    selected = gpd.GeoDataFrame(selected, geometry="geometry", crs=candidates.crs)

    out_dir.mkdir(parents=True, exist_ok=True)
    gpkg_path = out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins.gpkg"
    csv_path = out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins.csv"
    candidates_csv = out_dir / f"spam_crop_top{len(candidates)}_candidate_baseline_access.csv"
    selected.to_file(gpkg_path, driver="GPKG")
    _round_output_frame(pd.DataFrame(selected.drop(columns="geometry"))).to_csv(csv_path, index=False)
    _round_output_frame(pd.DataFrame(candidates_access.drop(columns="geometry"))).to_csv(candidates_csv, index=False)

    country, _ = _country_layers(project_root, iso3)
    fig, ax = plt.subplots(figsize=(9.0, 9.0))
    country.to_crs("EPSG:4326").boundary.plot(ax=ax, color="black", linewidth=1.2)
    crops = sorted(selected["crop_code"].unique())
    cmap = plt.get_cmap("tab10", len(crops))
    for idx, crop_code in enumerate(crops):
        subset = selected.loc[selected["crop_code"].eq(crop_code)]
        subset.plot(ax=ax, color=cmap(idx), markersize=55, edgecolor="white", linewidth=0.8, label=crop_code)
        for row in subset.itertuples():
            ax.text(row.geometry.x, row.geometry.y, f"{row.crop_code}{int(row.crop_rank)}", fontsize=7, ha="left", va="bottom")
    ax.set_title(f"{iso3} SPAM Top-{top_n_per_crop} Baseline-Connected Origins Per Crop")
    ax.set_axis_off()
    ax.legend(loc="lower left", ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins_map.png", dpi=180)
    plt.close(fig)

    summary = {
        "country_code": iso3,
        "candidate_gpkg": _relpath(candidate_gpkg, project_root),
        "selected_gpkg": _relpath(gpkg_path, project_root),
        "selected_csv": _relpath(csv_path, project_root),
        "n_candidates": int(len(candidates_access)),
        "n_selected": int(len(selected)),
        "n_crops": int(selected["crop_code"].nunique()),
        "component_filter": component_stats,
        "geometry_validation": {
            "checks": [candidate_geometry_report, city_geometry_report],
        },
        "warnings": warnings,
    }
    (out_dir / "baseline_connected_origin_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return gpkg_path


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    iso3 = args.country_code.upper()
    output_dir = args.output_dir if args.output_dir.is_absolute() else project_root / args.output_dir
    result = select_baseline_connected_origins(
        project_root=project_root,
        iso3=iso3,
        candidate_gpkg=args.candidate_gpkg,
        overlay_gpkg=args.overlay_gpkg,
        out_dir=output_dir,
        city_threshold=args.city_threshold,
        top_n_per_crop=args.top_n_per_crop,
        speed_paved_kmh=args.speed_paved_kmh,
        speed_unpaved_kmh=args.speed_unpaved_kmh,
        min_component_nodes=args.min_component_nodes,
        isolation_minutes=args.isolation_minutes,
    )
    print(json.dumps({"selected_origins_gpkg": _relpath(result, project_root)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
