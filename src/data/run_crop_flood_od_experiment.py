"""Run a first-pass crop accessibility experiment under flood-depth disruption.

The experiment stays intentionally narrow:

- one crop at a time
- origins are SPAM production cells above the crop-specific p95 threshold
- destinations are nearest populated places from GeoNames
- the only external factor is flood depth on roads
- product loss is accumulated along the chosen route by road-surface class
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import zipfile
from collections import defaultdict
from heapq import heappop, heappush
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pycountry
import rasterio
import yaml
from rasterio.mask import mask
from rasterio.transform import xy
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Point
from tqdm.auto import tqdm

from src.data.config import load_config
from src.data.run_flood_depth_experiment import (
    _country_layer,
    _depth_for_geometry,
    _effect_columns,
    _load_damage_config,
    _load_flood_mosaic,
    _road_surface_class,
    _severity_from_depth,
)


matplotlib.use("Agg")

GEONAMES_COLUMNS = [
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "latitude",
    "longitude",
    "feature_class",
    "feature_code",
    "country_code",
    "cc2",
    "admin1_code",
    "admin2_code",
    "admin3_code",
    "admin4_code",
    "population",
    "elevation",
    "dem",
    "timezone",
    "modification_date",
]

CROP_NAME_BY_CODE = {
    "BEAN": "bean",
    "COTT": "cotton",
    "MAIZ": "maize",
    "POTA": "potato",
    "RICE": "rice",
    "SORG": "sorghum",
    "SOYB": "soybean",
    "SUGC": "sugarcane",
    "SUNF": "sunflower",
    "WHEA": "wheat",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run crop OD flood experiment for one country.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"))
    parser.add_argument("--damage-config", type=Path, default=Path("config/road_climate_damage.yaml"))
    parser.add_argument("--loss-config", type=Path, default=Path("config/crop_transport_loss.yaml"))
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--crop-code", type=str, default="MAIZ", help="SPAM crop code, for example MAIZ or SUGC.")
    parser.add_argument("--city-population-threshold", type=int, default=10_000)
    parser.add_argument("--max-origins", type=int, default=5, help="Randomly sample up to this many crop origins from the p95 set.")
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--no-cache", action="store_true", help="Recompute even if cached outputs already exist.")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _experiment_slug(city_population_threshold: int, max_origins: int, random_seed: int) -> str:
    return f"pop{city_population_threshold}_n{max_origins}_seed{random_seed}"


def _cached_summary(out_dir: Path) -> dict | None:
    summary_path = out_dir / "summary.json"
    if not summary_path.exists():
        return None
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _load_roads_cached(project_root: Path, iso3: str, country: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    cache_dir = project_root / "outputs" / "cache" / "prepared_roads" / iso3
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{iso3.lower()}_roads_clipped.gpkg"
    if cache_path.exists():
        print(f"[crop-flood-od] reusing prepared roads cache: {cache_path.name}", flush=True)
        return gpd.read_file(cache_path).to_crs("EPSG:4326")

    print("[crop-flood-od] preparing clipped road cache", flush=True)
    source_path = project_root / "data" / "raw" / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    columns = [
        "geometry",
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
    ]
    roads = gpd.read_file(source_path, columns=columns, bbox=tuple(country.total_bounds)).to_crs("EPSG:4326")
    roads = roads.loc[roads.geometry.notna()].copy()
    roads["surface_group"] = _road_surface_class(roads)
    roads["road_row_id"] = np.arange(len(roads))
    roads = roads[["road_row_id", "surface_group", "geometry"]].copy()
    roads.to_file(cache_path, driver="GPKG")
    print(f"[crop-flood-od] wrote prepared roads cache: {cache_path.name}", flush=True)
    return roads


def _country_iso2(iso3: str) -> str:
    country = pycountry.countries.get(alpha_3=iso3.upper())
    if country is None:
        raise ValueError(f"Unknown ISO3 code: {iso3}")
    return str(country.alpha_2)


def _load_loss_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["crop_transport_loss"]


def _reuse_flood_road_overlay(project_root: Path, iso3: str) -> gpd.GeoDataFrame | None:
    path = project_root / "outputs" / "road_climate_experiments" / iso3 / "flood_depth" / "target_roads_with_flood_damage.gpkg"
    if not path.exists():
        return None
    overlay = gpd.read_file(path).to_crs("EPSG:4326")
    expected = {"road_row_id", "flood_depth_m", "severity", "effect_type", "speed_penalty_fraction", "closure_duration_days"}
    if not expected.issubset(set(overlay.columns)):
        return None
    return overlay[list(expected)].copy()


def _load_geonames_cities(project_root: Path, iso3: str, population_threshold: int, target_crs: str) -> gpd.GeoDataFrame:
    iso2 = _country_iso2(iso3)
    zip_path = project_root / "data" / "raw" / "cities" / iso2.upper() / f"{iso2.upper()}.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"Missing GeoNames archive: {zip_path}")

    rows: list[dict[str, object]] = []
    with zipfile.ZipFile(zip_path) as archive:
        member = next(name for name in archive.namelist() if name.upper().endswith(f"{iso2.upper()}.TXT"))
        with archive.open(member) as handle:
            reader = csv.reader((line.decode("utf-8") for line in handle), delimiter="\t")
            for row in reader:
                if len(row) != len(GEONAMES_COLUMNS):
                    continue
                record = dict(zip(GEONAMES_COLUMNS, row, strict=False))
                if record["feature_class"] != "P":
                    continue
                population = int(record["population"] or 0)
                if population < population_threshold:
                    continue
                rows.append(
                    {
                        "geonameid": int(record["geonameid"]),
                        "name": record["name"],
                        "feature_code": record["feature_code"],
                        "population": population,
                        "longitude": float(record["longitude"]),
                        "latitude": float(record["latitude"]),
                    }
                )
    if not rows:
        raise RuntimeError(f"No GeoNames populated places >= {population_threshold:,} found for {iso3}.")

    cities = gpd.GeoDataFrame(
        rows,
        geometry=gpd.points_from_xy([row["longitude"] for row in rows], [row["latitude"] for row in rows]),
        crs="EPSG:4326",
    ).to_crs(target_crs)
    return cities.sort_values(["population", "name"], ascending=[False, True]).reset_index(drop=True)


def _load_crop_origins(project_root: Path, country: gpd.GeoDataFrame, crop_code: str, target_crs: str) -> tuple[gpd.GeoDataFrame, float]:
    tif_path = project_root / "spam_prod_tifs" / f"spam2010V2r0_global_P_{crop_code}_A.tif"
    if not tif_path.exists():
        raise FileNotFoundError(f"Missing SPAM production raster for crop {crop_code}: {tif_path}")

    country_wgs84 = country.to_crs("EPSG:4326")
    with rasterio.open(tif_path) as src:
        clipped, transform = mask(src, country_wgs84.geometry, crop=True, filled=True, nodata=0)
        arr = clipped[0].astype("float32")

    positive = arr[arr > 0]
    if positive.size == 0:
        raise RuntimeError(f"No positive SPAM production values for crop {crop_code} in the selected country.")

    p95 = float(np.quantile(positive, 0.95))
    rows, cols = np.where(arr >= p95)
    values = arr[rows, cols]
    xs, ys = xy(transform, rows, cols, offset="center")

    origins = gpd.GeoDataFrame(
        {
            "origin_id": np.arange(len(values)),
            "crop_code": crop_code,
            "production_tons": values.astype(float),
            "p95_threshold_tons": p95,
        },
        geometry=gpd.points_from_xy(xs, ys),
        crs="EPSG:4326",
    ).to_crs(target_crs)
    return origins.sort_values("production_tons", ascending=False).reset_index(drop=True), p95


def _sample_origins(origins: gpd.GeoDataFrame, max_origins: int, random_seed: int) -> gpd.GeoDataFrame:
    if max_origins <= 0 or len(origins) <= max_origins:
        return origins.reset_index(drop=True)
    sampled = origins.sample(n=max_origins, random_state=random_seed).sort_values("production_tons", ascending=False)
    sampled = sampled.reset_index(drop=True).copy()
    sampled["origin_id"] = np.arange(len(sampled))
    return sampled


def _attach_flood_status(
    project_root: Path,
    iso3: str,
    roads: gpd.GeoDataFrame,
    country: gpd.GeoDataFrame,
    thresholds: dict[str, float | None],
    damage_cfg: dict,
    flood_cfg: dict | None = None,
) -> gpd.GeoDataFrame:
    reused = _reuse_flood_road_overlay(project_root, iso3)
    if reused is not None:
        print("[crop-flood-od] reusing existing flood road overlay", flush=True)
        merged = roads.merge(reused, on="road_row_id", how="left", suffixes=("", "_overlay"))
        merged["flood_depth_m"] = merged["flood_depth_m"].fillna(0.0)
        merged["severity"] = merged["severity"].fillna("none")
        merged["effect_type"] = merged["effect_type"].fillna("none")
        merged["speed_penalty_fraction"] = merged["speed_penalty_fraction"].fillna(0.0)
        merged["closure_duration_days"] = merged["closure_duration_days"].fillna(0).astype(int)
        return merged

    roads = roads.copy()
    flood_data, flood_transform, _ = _load_flood_mosaic(project_root, country, flood_cfg=flood_cfg)
    roads["flood_depth_m"] = 0.0
    scope_index = roads.index[roads["in_scope"]]
    print(f"[crop-flood-od] sampling_flood_depth_for_roads={len(scope_index)}", flush=True)
    sampled_depths = [
        _depth_for_geometry(geom, flood_data, flood_transform)
        for geom in tqdm(roads.loc[scope_index, "geometry"], total=len(scope_index), desc="flood_depth_overlay", leave=False)
    ]
    roads.loc[scope_index, "flood_depth_m"] = sampled_depths
    roads["severity"] = roads["flood_depth_m"].apply(lambda depth: _severity_from_depth(depth, thresholds))
    return _effect_columns(roads, damage_cfg)


def _iter_lines(geometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [line for line in geometry.geoms if isinstance(line, LineString)]
    return []


def _round_node(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), 1), round(float(y), 1))


def _edge_surface(surface_group: str) -> str:
    surface = str(surface_group or "unknown").lower()
    if surface not in {"paved", "unpaved", "unknown"}:
        return "unknown"
    return surface


def _build_graph(
    roads_proj: gpd.GeoDataFrame, scenario_name: str
) -> tuple[dict[int, list[tuple[int, float, float, str]]], STRtree, dict[tuple[float, float], int], list[Point]]:
    adjacency: dict[int, list[tuple[int, float, float, str]]] = defaultdict(list)
    node_lookup: dict[tuple[float, float], int] = {}
    node_coords: dict[int, tuple[float, float]] = {}
    next_node_id = 0

    def ensure_node(coord: tuple[float, float]) -> int:
        nonlocal next_node_id
        if coord not in node_lookup:
            node_lookup[coord] = next_node_id
            node_coords[next_node_id] = coord
            next_node_id += 1
        return node_lookup[coord]

    iterator = tqdm(
        roads_proj.itertuples(),
        total=len(roads_proj),
        desc=f"build_graph[{scenario_name}]",
        leave=False,
    )
    for row in iterator:
        if scenario_name == "flood" and int(row.closure_duration_days) > 0:
            continue
        surface = _edge_surface(row.surface_group)
        penalty = float(row.speed_penalty_fraction) if scenario_name == "flood" else 0.0
        penalty = min(max(penalty, 0.0), 0.95)
        for line in _iter_lines(row.geometry):
            coords = list(line.coords)
            for start, end in zip(coords[:-1], coords[1:], strict=False):
                start_key = _round_node(start[0], start[1])
                end_key = _round_node(end[0], end[1])
                start_id = ensure_node(start_key)
                end_id = ensure_node(end_key)
                length_m = math.hypot(end_key[0] - start_key[0], end_key[1] - start_key[1])
                if length_m == 0:
                    continue
                travel_cost_m = length_m / (1.0 - penalty)
                adjacency[start_id].append((end_id, travel_cost_m, length_m, surface))
                adjacency[end_id].append((start_id, travel_cost_m, length_m, surface))

    node_geoms = [Point(node_coords[node_id]) for node_id in sorted(node_coords)]
    tree = STRtree(node_geoms)
    point_to_node = {(round(float(point.x), 1), round(float(point.y), 1)): node_id for point, node_id in zip(node_geoms, sorted(node_coords), strict=False)}
    return adjacency, tree, point_to_node, node_geoms


def _nearest_node_id(point: Point, tree: STRtree, point_to_node: dict[tuple[float, float], int], node_geoms: list[Point]) -> tuple[int, float]:
    nearest = tree.nearest(point)
    if isinstance(nearest, (int, np.integer)):
        nearest = node_geoms[int(nearest)]
    node_key = (round(float(nearest.x), 1), round(float(nearest.y), 1))
    node_id = point_to_node[node_key]
    return node_id, float(point.distance(nearest))


def _loss_rate_for_surface(loss_cfg: dict, crop_name: str, surface_group: str) -> float:
    crop_rate = float(loss_cfg["crop_loss_rate_per_10km"][crop_name]["loss_fraction_per_10km"])
    paved_multiplier = float(loss_cfg["road_surface_adjustment"]["paved_multiplier_relative_to_unpaved"])
    if surface_group == "paved":
        return crop_rate * paved_multiplier
    return crop_rate


def _crop_loss_fraction(loss_cfg: dict, crop_name: str, surface_lengths_m: dict[str, float]) -> float:
    distance_unit_km = float(loss_cfg["methodology"]["distance_unit_km"])
    cap = float(loss_cfg["methodology"]["cumulative_loss_cap_fraction"])
    total_loss = 0.0
    for surface, length_m in surface_lengths_m.items():
        rate = _loss_rate_for_surface(loss_cfg, crop_name, surface)
        total_loss += rate * ((length_m / 1000.0) / distance_unit_km)
    return float(min(cap, total_loss))


def _multi_source_tree_to_cities(
    adjacency: dict[int, list[tuple[int, float, float, str]]],
    destination_node_ids: set[int],
) -> tuple[dict[int, float], dict[int, tuple[int, float, str]], dict[int, int]]:
    distances: dict[int, float] = {}
    previous: dict[int, tuple[int, float, str]] = {}
    nearest_destination: dict[int, int] = {}
    heap: list[tuple[float, int]] = []

    for node_id in destination_node_ids:
        distances[node_id] = 0.0
        nearest_destination[node_id] = node_id
        heappush(heap, (0.0, node_id))

    while heap:
        dist, node_id = heappop(heap)
        if dist > distances.get(node_id, math.inf):
            continue
        for neighbor, cost_m, length_m, surface in adjacency.get(node_id, []):
            next_dist = dist + cost_m
            if next_dist < distances.get(neighbor, math.inf):
                distances[neighbor] = next_dist
                previous[neighbor] = (node_id, length_m, surface)
                nearest_destination[neighbor] = nearest_destination[node_id]
                heappush(heap, (next_dist, neighbor))

    return distances, previous, nearest_destination


def _reconstruct_surface_lengths(
    origin_node_id: int,
    destination_node_id: int,
    previous: dict[int, tuple[int, float, str]],
) -> dict[str, float]:
    surface_lengths_m: dict[str, float] = defaultdict(float)
    cursor = origin_node_id
    while cursor != destination_node_id:
        if cursor not in previous:
            return {}
        next_node_id, length_m, surface = previous[cursor]
        surface_lengths_m[surface] += float(length_m)
        cursor = next_node_id

    return dict(surface_lengths_m)


def _choose_city_for_node(node_id: int, cities_at_node: dict[int, list[dict[str, object]]]) -> dict[str, object]:
    options = cities_at_node[node_id]
    options = sorted(options, key=lambda row: (-int(row["city_population"]), float(row["snap_distance_m"])))
    return options[0]


def _run_scenario(
    scenario_name: str,
    roads_proj: gpd.GeoDataFrame,
    origins_proj: gpd.GeoDataFrame,
    cities_proj: gpd.GeoDataFrame,
    loss_cfg: dict,
    crop_name: str,
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    adjacency, tree, point_to_node, node_geoms = _build_graph(roads_proj, scenario_name)
    if not adjacency:
        raise RuntimeError(f"Scenario `{scenario_name}` produced an empty traversable graph.")

    destination_node_ids: set[int] = set()
    cities_at_node: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in tqdm(cities_proj.itertuples(), total=len(cities_proj), desc=f"snap_cities[{scenario_name}]", leave=False):
        node_id, snap_distance_m = _nearest_node_id(row.geometry, tree, point_to_node, node_geoms)
        destination_node_ids.add(node_id)
        cities_at_node[node_id].append(
            {
                "city_name": row.name,
                "city_population": int(row.population),
                "city_feature_code": row.feature_code,
                "city_node_id": int(node_id),
                "snap_distance_m": float(snap_distance_m),
            }
        )

    print(f"[crop-flood-od] scenario={scenario_name} graph_nodes={len(adjacency)} destination_nodes={len(destination_node_ids)}", flush=True)
    distances, previous, nearest_destination = _multi_source_tree_to_cities(adjacency, destination_node_ids)
    print(f"[crop-flood-od] scenario={scenario_name} multi_source_tree_ready", flush=True)

    records: list[dict[str, object]] = []
    for row in tqdm(origins_proj.itertuples(), total=len(origins_proj), desc=f"route_origins[{scenario_name}]", leave=False):
        origin_node_id, origin_snap_m = _nearest_node_id(row.geometry, tree, point_to_node, node_geoms)
        destination_node_id = nearest_destination.get(origin_node_id)
        graph_cost_m = distances.get(origin_node_id)
        surface_lengths_m = {}
        if destination_node_id is not None and graph_cost_m is not None:
            surface_lengths_m = _reconstruct_surface_lengths(origin_node_id, destination_node_id, previous)
        connected = destination_node_id is not None and graph_cost_m is not None
        city_record = _choose_city_for_node(destination_node_id, cities_at_node) if connected else None
        route_length_m = float(sum(surface_lengths_m.values())) if connected else None
        total_access_cost_m = None if not connected else float(graph_cost_m + origin_snap_m + float(city_record["snap_distance_m"]))
        product_loss_fraction = None if not connected else _crop_loss_fraction(loss_cfg, crop_name, surface_lengths_m)
        records.append(
            {
                "origin_id": int(row.origin_id),
                "crop_code": row.crop_code,
                "production_tons": float(row.production_tons),
                "origin_snap_distance_m": float(origin_snap_m),
                "connected": bool(connected),
                "destination_city": None if city_record is None else str(city_record["city_name"]),
                "destination_population": None if city_record is None else int(city_record["city_population"]),
                "destination_feature_code": None if city_record is None else str(city_record["city_feature_code"]),
                "graph_cost_m": total_access_cost_m,
                "route_length_m": route_length_m,
                "paved_length_m": float(surface_lengths_m.get("paved", 0.0)),
                "unpaved_length_m": float(surface_lengths_m.get("unpaved", 0.0)),
                "unknown_length_m": float(surface_lengths_m.get("unknown", 0.0)),
                "product_loss_fraction": product_loss_fraction,
                "product_loss_tons": None if product_loss_fraction is None else float(product_loss_fraction * float(row.production_tons)),
                "scenario": scenario_name,
            }
        )

    roads_status = roads_proj.copy()
    roads_status["scenario"] = scenario_name
    roads_status["closed"] = False
    if scenario_name == "flood":
        roads_status["closed"] = roads_status["closure_duration_days"] > 0
    return pd.DataFrame(records), roads_status


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _render_access_change_map(
    country: gpd.GeoDataFrame,
    cities_wgs84: gpd.GeoDataFrame,
    origins_map: gpd.GeoDataFrame,
    roads_flood_wgs84: gpd.GeoDataFrame,
    out_path: Path,
    crop_code: str,
) -> Path:
    fig, ax = plt.subplots(figsize=(8.6, 8.6))
    country.boundary.plot(ax=ax, color="black", linewidth=1.2, zorder=10)

    flood_closed = roads_flood_wgs84.loc[roads_flood_wgs84["closed"]]
    if not flood_closed.empty:
        flood_closed.plot(ax=ax, color="#b10026", linewidth=1.0, alpha=0.8, zorder=3)

    connected = origins_map.loc[origins_map["delta_access_km"].notna()].copy()
    disconnected = origins_map.loc[origins_map["became_disconnected"]].copy()
    if not connected.empty:
        connected.plot(
            ax=ax,
            column="delta_access_km",
            cmap="YlOrRd",
            markersize=18,
            alpha=0.85,
            legend=True,
            legend_kwds={"label": "Access change (km)", "shrink": 0.7},
            zorder=6,
        )
    if not disconnected.empty:
        disconnected.plot(ax=ax, color="black", marker="x", markersize=28, linewidth=1.2, zorder=7)
    cities_wgs84.plot(ax=ax, color="#2b8cbe", marker="^", markersize=48, alpha=0.9, zorder=8)

    ax.set_title(f"{crop_code}: nearest-city access change under flood")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_aspect("equal")
    _save(fig, out_path)
    return out_path


def _render_boxplot(comparison: pd.DataFrame, column: str, title: str, ylabel: str, out_path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    baseline = comparison.loc[comparison["baseline_connected"], column.replace("flood_", "baseline_")].dropna()
    flood = comparison.loc[comparison["flood_connected"], column].dropna()
    ax.boxplot([baseline, flood], labels=["baseline", "flood"], showfliers=True)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    _save(fig, out_path)
    return out_path


def _render_destination_bar(comparison: pd.DataFrame, out_path: Path) -> Path:
    top = (
        comparison.loc[comparison["flood_connected"]]
        .groupby("destination_city_flood")
        .size()
        .sort_values(ascending=False)
        .head(10)
        .reset_index(name="origins")
    )
    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    if top.empty:
        ax.text(0.5, 0.5, "No connected origins in flood scenario", transform=ax.transAxes, ha="center", va="center")
    else:
        ax.bar(top["destination_city_flood"], top["origins"], color="#3182bd")
        ax.tick_params(axis="x", rotation=30)
    ax.set_title("Flood scenario: nearest destination cities")
    ax.set_ylabel("Origin count")
    _save(fig, out_path)
    return out_path


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    config = load_config(args.config, country_code_override=args.country_code.upper())
    iso3 = str(config.get("study_area", {}).get("country_code", args.country_code)).upper()
    crop_code = args.crop_code.upper()
    crop_name = CROP_NAME_BY_CODE.get(crop_code)
    if crop_name is None:
        raise ValueError(f"Unsupported crop code `{crop_code}`. Known codes: {sorted(CROP_NAME_BY_CODE)}")
    run_slug = _experiment_slug(args.city_population_threshold, args.max_origins, args.random_seed)
    out_dir = project_root / "outputs" / "crop_flood_od_experiments" / iso3 / crop_code / run_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = None if args.no_cache else _cached_summary(out_dir)
    if cached is not None:
        print(f"[crop-flood-od] cache hit: {out_dir}", flush=True)
        print(json.dumps(cached, indent=2))
        return

    print(f"[crop-flood-od] country={iso3} crop={crop_code}", flush=True)
    country = _country_layer(project_root, iso3)
    target_crs = str(country.estimate_utm_crs())

    damage_cfg = _load_damage_config(args.damage_config)
    loss_cfg = _load_loss_config(args.loss_config)
    flood_cfg = config.get("datasets", {}).get("flood", {})
    indicator_cfg = damage_cfg["indicators"]["flood_depth"]
    thresholds = indicator_cfg["thresholds"]
    in_scope_surfaces = set(damage_cfg["road_selection"]["include_surface_groups"])

    roads = _load_roads_cached(project_root, iso3, country)
    roads["in_scope"] = roads["surface_group"].isin(in_scope_surfaces)
    print(f"[crop-flood-od] roads_loaded={len(roads)} in_scope={int(roads['in_scope'].sum())}", flush=True)
    roads = _attach_flood_status(project_root, iso3, roads, country, thresholds, damage_cfg, flood_cfg=flood_cfg)
    roads_proj = roads.to_crs(target_crs)

    cities_proj = _load_geonames_cities(project_root, iso3, args.city_population_threshold, target_crs)
    cities_proj = cities_proj.clip(country.to_crs(target_crs))
    print(f"[crop-flood-od] candidate_cities={len(cities_proj)}", flush=True)
    origins_proj, crop_p95 = _load_crop_origins(project_root, country, crop_code, target_crs)
    print(f"[crop-flood-od] crop_origins_p95_total={len(origins_proj)} threshold_tons={crop_p95:.3f}", flush=True)
    origins_proj = _sample_origins(origins_proj, args.max_origins, args.random_seed)
    print(f"[crop-flood-od] crop_origins_sampled={len(origins_proj)} seed={args.random_seed}", flush=True)

    baseline_df, baseline_roads = _run_scenario("baseline", roads_proj, origins_proj, cities_proj, loss_cfg, crop_name)
    print("[crop-flood-od] baseline routes ready", flush=True)
    flood_df, flood_roads = _run_scenario("flood", roads_proj, origins_proj, cities_proj, loss_cfg, crop_name)
    print("[crop-flood-od] flood routes ready", flush=True)

    comparison = baseline_df.merge(
        flood_df,
        on="origin_id",
        suffixes=("_baseline", "_flood"),
    )
    comparison["baseline_connected"] = comparison["connected_baseline"]
    comparison["flood_connected"] = comparison["connected_flood"]
    comparison["delta_access_km"] = (comparison["graph_cost_m_flood"] - comparison["graph_cost_m_baseline"]) / 1000.0
    comparison["delta_route_km"] = (comparison["route_length_m_flood"] - comparison["route_length_m_baseline"]) / 1000.0
    comparison["delta_loss_fraction"] = comparison["product_loss_fraction_flood"] - comparison["product_loss_fraction_baseline"]
    comparison["delta_loss_tons"] = comparison["product_loss_tons_flood"] - comparison["product_loss_tons_baseline"]
    comparison["became_disconnected"] = comparison["baseline_connected"] & (~comparison["flood_connected"])
    comparison["switched_city"] = (
        comparison["destination_city_baseline"].notna()
        & comparison["destination_city_flood"].notna()
        & (comparison["destination_city_baseline"] != comparison["destination_city_flood"])
    )

    baseline_df.to_csv(out_dir / "baseline_routes.csv", index=False)
    flood_df.to_csv(out_dir / "flood_routes.csv", index=False)
    comparison.to_csv(out_dir / "comparison.csv", index=False)
    origins_proj.to_crs("EPSG:4326").to_file(out_dir / "crop_origins_p95.gpkg", driver="GPKG")
    cities_proj.to_crs("EPSG:4326").to_file(out_dir / "destination_cities.gpkg", driver="GPKG")
    flood_roads.to_crs("EPSG:4326").to_file(out_dir / "flood_road_status.gpkg", driver="GPKG")

    origins_map = origins_proj.to_crs("EPSG:4326").merge(
        comparison[
            [
                "origin_id",
                "delta_access_km",
                "delta_route_km",
                "delta_loss_fraction",
                "delta_loss_tons",
                "became_disconnected",
                "destination_city_baseline",
                "destination_city_flood",
            ]
        ],
        on="origin_id",
        how="left",
    )
    cities_wgs84 = cities_proj.to_crs("EPSG:4326")
    flood_roads_wgs84 = flood_roads.to_crs("EPSG:4326")

    created = {
        "access_change_map": str(
            _render_access_change_map(
                country.to_crs("EPSG:4326"),
                cities_wgs84,
                origins_map,
                flood_roads_wgs84,
                out_dir / f"{crop_code.lower()}_access_change_map.png",
                crop_code,
            ).relative_to(project_root)
        ),
        "access_boxplot": str(
            _render_boxplot(
                comparison.rename(
                    columns={
                        "graph_cost_m_baseline": "baseline_graph_cost_m",
                        "graph_cost_m_flood": "flood_graph_cost_m",
                    }
                ),
                "flood_graph_cost_m",
                f"{crop_code}: nearest-city access cost",
                "Access cost (m)",
                out_dir / f"{crop_code.lower()}_access_boxplot.png",
            ).relative_to(project_root)
        ),
        "loss_boxplot": str(
            _render_boxplot(
                comparison.rename(
                    columns={
                        "product_loss_fraction_baseline": "baseline_product_loss_fraction",
                        "product_loss_fraction_flood": "flood_product_loss_fraction",
                    }
                ),
                "flood_product_loss_fraction",
                f"{crop_code}: route-level product loss fraction",
                "Loss fraction",
                out_dir / f"{crop_code.lower()}_loss_boxplot.png",
            ).relative_to(project_root)
        ),
        "destination_bar": str(
            _render_destination_bar(
                comparison,
                out_dir / f"{crop_code.lower()}_destination_bar.png",
            ).relative_to(project_root)
        ),
        "comparison_csv": str((out_dir / "comparison.csv").relative_to(project_root)),
        "flood_roads_gpkg": str((out_dir / "flood_road_status.gpkg").relative_to(project_root)),
    }

    summary = {
        "country_code": iso3,
        "crop_code": crop_code,
        "crop_name": crop_name,
        "city_population_threshold": args.city_population_threshold,
        "n_candidate_cities": int(len(cities_proj)),
        "n_origins": int(len(origins_proj)),
        "run_slug": run_slug,
        "sample_mode": "random_from_crop_p95",
        "random_seed": int(args.random_seed),
        "crop_p95_threshold_tons": float(crop_p95),
        "baseline_connected_origins": int(comparison["baseline_connected"].sum()),
        "flood_connected_origins": int(comparison["flood_connected"].sum()),
        "became_disconnected": int(comparison["became_disconnected"].sum()),
        "switched_city": int(comparison["switched_city"].sum()),
        "median_baseline_access_km": None
        if comparison.loc[comparison["baseline_connected"], "graph_cost_m_baseline"].dropna().empty
        else float(comparison.loc[comparison["baseline_connected"], "graph_cost_m_baseline"].median() / 1000.0),
        "median_flood_access_km": None
        if comparison.loc[comparison["flood_connected"], "graph_cost_m_flood"].dropna().empty
        else float(comparison.loc[comparison["flood_connected"], "graph_cost_m_flood"].median() / 1000.0),
        "median_delta_access_km": None
        if comparison["delta_access_km"].dropna().empty
        else float(comparison["delta_access_km"].dropna().median()),
        "median_baseline_loss_fraction": None
        if comparison.loc[comparison["baseline_connected"], "product_loss_fraction_baseline"].dropna().empty
        else float(comparison.loc[comparison["baseline_connected"], "product_loss_fraction_baseline"].median()),
        "median_flood_loss_fraction": None
        if comparison.loc[comparison["flood_connected"], "product_loss_fraction_flood"].dropna().empty
        else float(comparison.loc[comparison["flood_connected"], "product_loss_fraction_flood"].median()),
        "median_delta_loss_fraction": None
        if comparison["delta_loss_fraction"].dropna().empty
        else float(comparison["delta_loss_fraction"].dropna().median()),
        "outputs": created,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
