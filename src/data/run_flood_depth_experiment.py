"""Run a first-pass flood-depth road-climate experiment for one country.

This experiment is intentionally narrow:

- use only the `Flood Depth` indicator
- keep only `unpaved` and `unknown` road segments in scope
- assign severity from flood-depth thresholds
- produce quick-look maps and summary tables
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
import yaml
from matplotlib.lines import Line2D
from rasterio.merge import merge
from rasterio.transform import rowcol
from shapely.geometry import LineString, MultiLineString, Point, box
from tqdm.auto import tqdm

from src.data.config import load_config


matplotlib.use("Agg")

SEVERITY_ORDER = ["none", "minor", "moderate", "severe", "catastrophic"]
SEVERITY_COLORS = {
    "none": "#bdbdbd",
    "minor": "#fee08b",
    "moderate": "#fdae61",
    "severe": "#f46d43",
    "catastrophic": "#a50026",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run flood-depth road experiment for one country.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"))
    parser.add_argument("--damage-config", type=Path, default=Path("config/road_climate_damage.yaml"))
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _country_layer(project_root: Path, iso3: str) -> gpd.GeoDataFrame:
    gadm_path = project_root / "data" / "raw" / "gadm" / iso3 / f"gadm41_{iso3}.gpkg"
    return gpd.read_file(gadm_path, layer="ADM_ADM_0").to_crs("EPSG:4326")


def _road_surface_class(frame: gpd.GeoDataFrame) -> pd.Series:
    preferred = [
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
    ]
    values = pd.Series("unknown", index=frame.index, dtype="object")
    for column in preferred:
        if column not in frame.columns:
            continue
        raw = frame[column].astype("string").str.lower()
        values = values.where(~raw.isin(["paved", "unpaved"]), raw.fillna(values))
    return values.fillna("unknown")


def _load_roads(project_root: Path, iso3: str, country: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    path = project_root / "data" / "raw" / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    roads = gpd.read_file(path).to_crs("EPSG:4326").clip(country)
    roads = roads.loc[roads.geometry.notna()].copy()
    roads["surface_group"] = _road_surface_class(roads)
    roads["road_row_id"] = np.arange(len(roads))
    roads["length_km"] = roads.to_crs(str(country.estimate_utm_crs())).length / 1000.0
    return roads


def _load_damage_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["road_climate_damage"]


def _load_flood_mosaic(project_root: Path, country: gpd.GeoDataFrame) -> tuple[np.ndarray, rasterio.Affine]:
    rp_dir = project_root / "data" / "raw" / "flood" / "jrc_glofas" / "RP100"
    bbox_geom = box(*country.total_bounds)
    candidate_paths: list[Path] = []
    for tif_path in sorted(rp_dir.glob("*.tif")):
        with rasterio.open(tif_path) as src:
            if box(*src.bounds).intersects(bbox_geom):
                candidate_paths.append(tif_path)
    if not candidate_paths:
        raise FileNotFoundError("No RP100 flood tiles intersect the selected country.")

    datasets = [rasterio.open(path) for path in candidate_paths]
    try:
        merged, transform = merge(datasets)
    finally:
        for dataset in datasets:
            dataset.close()
    return merged[0], transform


def _sample_raster_value(data: np.ndarray, transform, point: Point) -> float:
    row, col = rowcol(transform, point.x, point.y)
    if row < 0 or col < 0 or row >= data.shape[0] or col >= data.shape[1]:
        return 0.0
    value = float(data[row, col])
    if not np.isfinite(value) or value <= 0:
        return 0.0
    return value


def _geometry_probe_point(geometry) -> Point:
    """Return one fast probe point for first-pass road/raster overlay.

    This intentionally trades some spatial fidelity for speed so that
    country-scale experiments finish quickly enough to produce preview figures.
    """

    if isinstance(geometry, (LineString, MultiLineString)):
        try:
            point = geometry.interpolate(0.5, normalized=True)
            if isinstance(point, Point):
                return point
        except Exception:
            pass
    point = geometry.representative_point()
    if isinstance(point, Point):
        return point
    return geometry.centroid


def _depth_for_geometry(geometry, data: np.ndarray, transform) -> float:
    return _sample_raster_value(data, transform, _geometry_probe_point(geometry))


def _severity_from_depth(depth: float, thresholds: dict[str, float | None]) -> str:
    if not np.isfinite(depth) or depth <= 0:
        return "none"
    if thresholds.get("catastrophic") is not None and depth >= float(thresholds["catastrophic"]):
        return "catastrophic"
    if thresholds.get("severe") is not None and depth >= float(thresholds["severe"]):
        return "severe"
    if thresholds.get("moderate") is not None and depth >= float(thresholds["moderate"]):
        return "moderate"
    if thresholds.get("minor") is not None and depth >= float(thresholds["minor"]):
        return "minor"
    return "none"


def _effect_columns(frame: gpd.GeoDataFrame, cfg: dict) -> gpd.GeoDataFrame:
    severity_cfg = cfg["severity_effects"]
    frame = frame.copy()
    frame["effect_type"] = "none"
    frame["speed_penalty_fraction"] = 0.0
    frame["closure_duration_days"] = 0
    for severity_name, effect in severity_cfg.items():
        mask = frame["severity"] == severity_name
        frame.loc[mask, "effect_type"] = str(effect["effect_type"])
        frame.loc[mask, "speed_penalty_fraction"] = float(effect["speed_penalty_fraction"])
        frame.loc[mask, "closure_duration_days"] = int(effect["closure_duration_days"])
    return frame


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _setup_axes(country: gpd.GeoDataFrame, title: str) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(8, 8))
    minx, miny, maxx, maxy = country.total_bounds
    dx = max(maxx - minx, 0.2)
    dy = max(maxy - miny, 0.2)
    ax.set_xlim(minx - dx * 0.08, maxx + dx * 0.08)
    ax.set_ylim(miny - dy * 0.08, maxy + dy * 0.08)
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    country.boundary.plot(ax=ax, color="black", linewidth=1.2, zorder=10)
    return fig, ax


def _render_severity_map(country: gpd.GeoDataFrame, roads: gpd.GeoDataFrame, out_path: Path) -> Path:
    fig, ax = _setup_axes(country, "Flood Depth Experiment: Target Roads By Severity")
    roads.loc[roads["severity"] == "none"].plot(ax=ax, color=SEVERITY_COLORS["none"], linewidth=0.45, alpha=0.5, zorder=2)
    for severity in ["minor", "moderate", "severe", "catastrophic"]:
        subset = roads.loc[roads["severity"] == severity]
        if not subset.empty:
            subset.plot(ax=ax, color=SEVERITY_COLORS[severity], linewidth=1.4, alpha=0.9, zorder=4)
    handles = [Line2D([0], [0], color=SEVERITY_COLORS[s], lw=3, label=s) for s in SEVERITY_ORDER]
    ax.legend(handles=handles, loc="lower left", fontsize=8, title="severity")
    _save(fig, out_path)
    return out_path


def _render_length_bar(summary: pd.DataFrame, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 4.8))
    ax.bar(summary["severity"], summary["length_km"], color=[SEVERITY_COLORS[s] for s in summary["severity"]])
    ax.set_title("Target Road Length By Flood Severity")
    ax.set_xlabel("Severity")
    ax.set_ylabel("Road length (km)")
    _save(fig, out_path)
    return out_path


def _render_affected_length_bar(summary: pd.DataFrame, out_path: Path) -> Path:
    affected = summary.loc[summary["severity"] != "none"].copy()
    fig, ax = plt.subplots(figsize=(8, 4.8))
    if affected.empty:
        ax.text(0.5, 0.5, "No affected target roads", transform=ax.transAxes, ha="center", va="center")
    else:
        ax.bar(affected["severity"], affected["length_km"], color=[SEVERITY_COLORS[s] for s in affected["severity"]])
    ax.set_title("Affected Target Road Length By Flood Severity")
    ax.set_xlabel("Severity")
    ax.set_ylabel("Road length (km)")
    _save(fig, out_path)
    return out_path


def _render_depth_histogram(roads: gpd.GeoDataFrame, out_path: Path) -> Path:
    values = roads.loc[roads["flood_depth_m"] > 0, "flood_depth_m"]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    if values.empty:
        ax.text(0.5, 0.5, "No positive flood-depth values on target roads", transform=ax.transAxes, ha="center", va="center")
    else:
        ax.hist(values, bins=30, color="#3182bd", edgecolor="white", alpha=0.85)
    ax.set_title("Flood Depth Along Target Roads")
    ax.set_xlabel("Flood depth (m)")
    ax.set_ylabel("Road segment count")
    _save(fig, out_path)
    return out_path


def main() -> None:
    args = parse_args()
    config = load_config(args.config, country_code_override=args.country_code.upper())
    project_root = _project_root()
    iso3 = str(config.get("study_area", {}).get("country_code", args.country_code)).upper()

    damage_cfg = _load_damage_config(args.damage_config)
    indicator_cfg = damage_cfg["indicators"]["flood_depth"]
    thresholds = indicator_cfg["thresholds"]
    in_scope_surfaces = set(damage_cfg["road_selection"]["include_surface_groups"])

    country = _country_layer(project_root, iso3)
    roads = _load_roads(project_root, iso3, country)
    roads["in_scope"] = roads["surface_group"].isin(in_scope_surfaces)
    scoped_roads = roads.loc[roads["in_scope"]].copy()
    if scoped_roads.empty:
        raise RuntimeError(f"No in-scope roads found for {iso3}.")
    print(f"[flood-depth] country={iso3} total_roads={len(roads)} scoped_roads={len(scoped_roads)}", flush=True)

    flood_data, flood_transform = _load_flood_mosaic(project_root, country)
    print("[flood-depth] sampling flood depth on scoped roads", flush=True)
    scoped_roads["flood_depth_m"] = [
        _depth_for_geometry(geom, flood_data, flood_transform)
        for geom in tqdm(scoped_roads.geometry, total=len(scoped_roads), desc="flood_depth_overlay", leave=False)
    ]
    scoped_roads["severity"] = scoped_roads["flood_depth_m"].apply(lambda depth: _severity_from_depth(depth, thresholds))
    scoped_roads = _effect_columns(scoped_roads, damage_cfg)

    out_dir = project_root / "outputs" / "road_climate_experiments" / iso3 / "flood_depth"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = (
        scoped_roads.groupby("severity", dropna=False)
        .agg(road_segments=("road_row_id", "count"), length_km=("length_km", "sum"), max_depth_m=("flood_depth_m", "max"))
        .reset_index()
    )
    summary["severity"] = pd.Categorical(summary["severity"], categories=SEVERITY_ORDER, ordered=True)
    summary = summary.sort_values("severity")
    summary["length_km"] = summary["length_km"].round(3)
    summary["max_depth_m"] = summary["max_depth_m"].round(3)
    summary.to_csv(out_dir / "severity_summary.csv", index=False)

    surface_summary = (
        scoped_roads.groupby(["surface_group", "severity"], dropna=False)
        .agg(road_segments=("road_row_id", "count"), length_km=("length_km", "sum"))
        .reset_index()
        .sort_values(["surface_group", "severity"])
    )
    surface_summary["length_km"] = surface_summary["length_km"].round(3)
    surface_summary.to_csv(out_dir / "surface_severity_summary.csv", index=False)

    scoped_roads.to_file(out_dir / "target_roads_with_flood_damage.gpkg", driver="GPKG")
    print(f"[flood-depth] wrote outputs to {out_dir}", flush=True)

    created = {
        "severity_map": str(_render_severity_map(country, scoped_roads, out_dir / "flood_depth_severity_map.png").relative_to(project_root)),
        "length_bar": str(_render_length_bar(summary, out_dir / "flood_depth_length_by_severity.png").relative_to(project_root)),
        "affected_length_bar": str(_render_affected_length_bar(summary, out_dir / "flood_depth_length_by_severity_affected_only.png").relative_to(project_root)),
        "depth_histogram": str(_render_depth_histogram(scoped_roads, out_dir / "flood_depth_histogram.png").relative_to(project_root)),
        "severity_summary_csv": str((out_dir / "severity_summary.csv").relative_to(project_root)),
        "surface_summary_csv": str((out_dir / "surface_severity_summary.csv").relative_to(project_root)),
        "roads_gpkg": str((out_dir / "target_roads_with_flood_damage.gpkg").relative_to(project_root)),
    }

    report = {
        "country_code": iso3,
        "indicator": "flood_depth",
        "sampling_strategy": "single_probe_point_midpoint_first_pass",
        "implementation_scope": damage_cfg["implementation_scope"],
        "weekly_aggregation": indicator_cfg.get("weekly_aggregation"),
        "thresholds_m": thresholds,
        "n_total_roads": int(len(roads)),
        "n_scoped_roads": int(len(scoped_roads)),
        "scoped_surface_groups": sorted(in_scope_surfaces),
        "n_affected_roads": int((scoped_roads["severity"] != "none").sum()),
        "max_depth_m": None if scoped_roads.empty else float(scoped_roads["flood_depth_m"].max()),
        "outputs": created,
    }
    (out_dir / "experiment_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
