"""Run first-pass monthly road rerouting scenarios for a country.

This script builds a country road graph, snaps cropland-origin cells and
population-threshold cities to the graph, applies a monthly precipitation-driven
road closure rule, and compares baseline vs scenario route lengths.
"""

from __future__ import annotations

import argparse
import json
import math
import zipfile
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pycountry
import rasterio
from rasterio.mask import mask
from rasterio.transform import xy
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Point

from src.data.config import load_config
from src.data.utils import FetchContext, ensure_directory, ensure_local_copy


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

SPAM_GLOB = "spam2010V2r0_global_H_*_A.tif"
CHIRPS_VERSION = "v3.0"


@dataclass(slots=True)
class ScenarioConfig:
    name: str
    unknown_surface_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run monthly road rerouting scenarios for one country.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"))
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--month", type=int, default=7, help="Calendar month used for dynamic inputs, default July.")
    parser.add_argument("--city-population-threshold", type=int, default=500_000)
    parser.add_argument("--road-quantile-unpaved", type=float, default=0.5)
    parser.add_argument("--road-quantile-damaged", type=float, default=0.7)
    parser.add_argument("--max-origins", type=int, default=1500, help="Keep the top harvested-area cropland cells to control runtime.")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _country_iso2(iso3: str) -> str:
    country = pycountry.countries.get(alpha_3=iso3.upper())
    if country is None:
        raise ValueError(f"Unknown ISO3 code: {iso3}")
    return str(country.alpha_2)


def _fetch_context(project_root: Path) -> FetchContext:
    data_root = project_root / "data"
    return FetchContext(
        project_root=project_root,
        data_root=data_root,
        raw_root=data_root / "raw",
        metadata_root=data_root / "metadata",
        manual_steps_root=data_root / "metadata" / "manual_steps",
        logs_root=data_root / "logs",
    )


def _country_layers(project_root: Path, iso3: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    gadm_path = project_root / "data" / "raw" / "gadm" / iso3 / f"gadm41_{iso3}.gpkg"
    country = gpd.read_file(gadm_path, layer="ADM_ADM_0").to_crs("EPSG:4326")
    admin = gpd.read_file(gadm_path, layer="ADM_ADM_2").to_crs("EPSG:4326")
    return country, admin


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
    return roads


def _chirps_path(project_root: Path, year: int, month: int) -> Path:
    return project_root / "data" / "raw" / "chirps" / "global" / "monthly" / f"chirps-{CHIRPS_VERSION}.{year}.{month:02d}.tif"


def _ensure_chirps(project_root: Path, year: int, month: int) -> Path:
    target = _chirps_path(project_root, year, month)
    context = _fetch_context(project_root)
    url = f"https://data.chc.ucsb.edu/products/CHIRPS/{CHIRPS_VERSION}/monthly/global/tifs/{target.name}"
    path, _ = ensure_local_copy(url, target, context)
    return path


def _ensure_geonames_country(project_root: Path, iso2: str) -> Path:
    target = project_root / "data" / "raw" / "cities" / iso2.upper() / f"{iso2.upper()}.zip"
    context = _fetch_context(project_root)
    url = f"https://download.geonames.org/export/dump/{iso2.upper()}.zip"
    path, _ = ensure_local_copy(url, target, context)
    return path


def _ensure_geonames_global(project_root: Path) -> Path:
    target = project_root / "data" / "raw" / "cities" / "global" / "cities500.zip"
    context = _fetch_context(project_root)
    url = "https://download.geonames.org/export/dump/cities500.zip"
    path, _ = ensure_local_copy(url, target, context)
    return path


def _read_geonames_zip(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as archive:
        member = next(name for name in archive.namelist() if name.lower().endswith(".txt"))
        with archive.open(member) as handle:
            frame = pd.read_csv(handle, sep="\t", header=None, names=GEONAMES_COLUMNS, dtype={"country_code": "string"})
    frame["population"] = pd.to_numeric(frame["population"], errors="coerce").fillna(0)
    return frame


def _load_cities(project_root: Path, iso3: str, population_threshold: int, target_crs: str) -> gpd.GeoDataFrame:
    iso2 = _country_iso2(iso3)
    country_zip = _ensure_geonames_country(project_root, iso2)
    cities = _read_geonames_zip(country_zip)
    cities = cities[(cities["feature_class"] == "P") & (cities["population"] >= population_threshold)].copy()
    if cities.empty:
        global_zip = _ensure_geonames_global(project_root)
        fallback = _read_geonames_zip(global_zip)
        fallback = fallback[
            (fallback["country_code"].astype("string").str.upper() == iso2.upper())
            & (fallback["feature_class"] == "P")
            & (fallback["population"] >= population_threshold)
        ].copy()
        cities = fallback
    if cities.empty:
        raise RuntimeError(f"No GeoNames cities >= {population_threshold:,} found for {iso3}.")

    gdf = gpd.GeoDataFrame(
        cities[["geonameid", "name", "population"]].copy(),
        geometry=gpd.points_from_xy(cities["longitude"], cities["latitude"]),
        crs="EPSG:4326",
    ).to_crs(target_crs)
    return gdf


def _load_cropland_origins(project_root: Path, country: gpd.GeoDataFrame, target_crs: str, max_origins: int) -> gpd.GeoDataFrame:
    spam_dir = project_root / "spam_tifs"
    tif_paths = sorted(spam_dir.glob(SPAM_GLOB))
    if not tif_paths:
        raise FileNotFoundError(f"No SPAM GeoTIFFs found under {spam_dir}")

    country_wgs84 = country.to_crs("EPSG:4326")
    total = None
    final_transform = None
    for tif_path in tif_paths:
        with rasterio.open(tif_path) as src:
            clipped, transform = mask(src, country_wgs84.geometry, crop=True, filled=True, nodata=0)
            arr = clipped[0].astype("float32")
            total = arr if total is None else total + arr
            final_transform = transform

    assert total is not None and final_transform is not None
    rows, cols = np.where(total > 0)
    if rows.size == 0:
        raise RuntimeError("No cropland cells with harvested area > 0 intersect the selected country.")

    values = total[rows, cols]
    order = np.argsort(values)[::-1]
    if max_origins > 0:
        order = order[:max_origins]
    rows = rows[order]
    cols = cols[order]
    values = values[order]

    xs, ys = xy(final_transform, rows, cols, offset="center")
    gdf = gpd.GeoDataFrame(
        {"origin_id": np.arange(len(values)), "harvested_area_index": values},
        geometry=gpd.points_from_xy(xs, ys),
        crs="EPSG:4326",
    ).to_crs(target_crs)
    return gdf


def _sample_roads_precipitation(roads: gpd.GeoDataFrame, chirps_path: Path) -> gpd.GeoDataFrame:
    roads = roads.copy()
    centroids = roads.to_crs("EPSG:4326").geometry.centroid
    coords = [(geom.x, geom.y) for geom in centroids]
    with rasterio.open(chirps_path) as src:
        sampled = [value[0] for value in src.sample(coords)]
    roads["precip_month"] = np.asarray(sampled, dtype="float64")
    roads["precip_month"] = roads["precip_month"].fillna(0)
    roads["precip_quantile"] = roads["precip_month"].rank(pct=True, method="average")
    return roads


def _iter_lines(geometry) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [line for line in geometry.geoms if isinstance(line, LineString)]
    return []


def _round_node(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), 1), round(float(y), 1))


def _build_graph(
    roads_proj: gpd.GeoDataFrame,
    closed_row_ids: set[int],
) -> tuple[dict[int, list[tuple[int, float]]], dict[int, tuple[float, float]], STRtree, dict[bytes, int], dict[int, int]]:
    adjacency: dict[int, list[tuple[int, float]]] = {}
    node_lookup: dict[tuple[float, float], int] = {}
    node_coords: dict[int, tuple[float, float]] = {}
    next_node_id = 0

    def ensure_node(coord: tuple[float, float]) -> int:
        nonlocal next_node_id
        if coord not in node_lookup:
            node_lookup[coord] = next_node_id
            node_coords[next_node_id] = coord
            adjacency[next_node_id] = []
            next_node_id += 1
        return node_lookup[coord]

    for row in roads_proj.itertuples():
        if int(row.road_row_id) in closed_row_ids:
            continue
        for line in _iter_lines(row.geometry):
            coords = list(line.coords)
            for start, end in zip(coords[:-1], coords[1:], strict=False):
                start_key = _round_node(start[0], start[1])
                end_key = _round_node(end[0], end[1])
                start_id = ensure_node(start_key)
                end_id = ensure_node(end_key)
                length = math.hypot(end_key[0] - start_key[0], end_key[1] - start_key[1])
                if length == 0:
                    continue
                adjacency[start_id].append((end_id, length))
                adjacency[end_id].append((start_id, length))

    sorted_node_ids = sorted(node_coords)
    node_geoms = [Point(node_coords[node_id]) for node_id in sorted_node_ids]
    tree = STRtree(node_geoms)
    geom_wkb_to_node = {geom.wkb: node_id for geom, node_id in zip(node_geoms, sorted(node_coords), strict=False)}
    tree_index_to_node = {idx: node_id for idx, node_id in enumerate(sorted_node_ids)}
    return adjacency, node_coords, tree, geom_wkb_to_node, tree_index_to_node


def _nearest_node_id(
    point: Point,
    tree: STRtree,
    geom_wkb_to_node: dict[bytes, int],
    tree_index_to_node: dict[int, int],
) -> tuple[int, float]:
    nearest = tree.nearest(point)
    if isinstance(nearest, (int, np.integer)):
        node_id = tree_index_to_node[int(nearest)]
        nearest_geom = tree.geometries[int(nearest)]
        return node_id, float(point.distance(nearest_geom))
    node_id = geom_wkb_to_node[nearest.wkb]
    return node_id, float(point.distance(nearest))


def _multi_source_dijkstra(adjacency: dict[int, list[tuple[int, float]]], sources: list[tuple[int, float]]) -> dict[int, float]:
    distances: dict[int, float] = {}
    heap: list[tuple[float, int]] = []
    for node_id, initial_cost in sources:
        current = distances.get(node_id)
        if current is None or initial_cost < current:
            distances[node_id] = initial_cost
            heappush(heap, (initial_cost, node_id))

    while heap:
        dist, node_id = heappop(heap)
        if dist > distances.get(node_id, math.inf):
            continue
        for neighbor, weight in adjacency.get(node_id, []):
            next_dist = dist + weight
            if next_dist < distances.get(neighbor, math.inf):
                distances[neighbor] = next_dist
                heappush(heap, (next_dist, neighbor))
    return distances


def _scenario_closed_rows(roads: gpd.GeoDataFrame, scenario: ScenarioConfig, q_unpaved: float, q_damaged: float) -> tuple[pd.Series, set[int]]:
    surfaces = roads["surface_group"].astype("string").fillna("unknown")
    effective_surface = surfaces.where(surfaces != "unknown", scenario.unknown_surface_mode)
    permanent_damage = roads["precip_quantile"] > q_damaged
    temporary_closure = (roads["precip_quantile"] > q_unpaved) & (effective_surface == "unpaved")
    closed = permanent_damage | temporary_closure
    return effective_surface, set(roads.loc[closed, "road_row_id"].astype(int))


def _run_scenario(
    scenario: ScenarioConfig,
    roads_proj: gpd.GeoDataFrame,
    origins_proj: gpd.GeoDataFrame,
    cities_proj: gpd.GeoDataFrame,
    q_unpaved: float,
    q_damaged: float,
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    roads_proj = roads_proj.copy()
    effective_surface, closed_ids = _scenario_closed_rows(roads_proj, scenario, q_unpaved, q_damaged)
    roads_proj["scenario"] = scenario.name
    roads_proj["effective_surface"] = effective_surface
    roads_proj["closed"] = roads_proj["road_row_id"].astype(int).isin(closed_ids)

    adjacency, node_coords, tree, geom_wkb_to_node, tree_index_to_node = _build_graph(roads_proj, closed_ids)
    if not adjacency:
        raise RuntimeError(f"Scenario {scenario.name} removed all traversable road edges.")

    city_sources: list[tuple[int, float]] = []
    city_snap_records: list[dict[str, float | int | str]] = []
    for row in cities_proj.itertuples():
        node_id, snap_distance = _nearest_node_id(row.geometry, tree, geom_wkb_to_node, tree_index_to_node)
        city_sources.append((node_id, snap_distance))
        city_snap_records.append(
            {
                "city_name": row.name,
                "city_population": int(row.population),
                "city_node_id": int(node_id),
                "city_snap_distance_m": snap_distance,
            }
        )

    graph_dist = _multi_source_dijkstra(adjacency, city_sources)

    origin_records: list[dict[str, float | int | str | None]] = []
    for row in origins_proj.itertuples():
        node_id, snap_distance = _nearest_node_id(row.geometry, tree, geom_wkb_to_node, tree_index_to_node)
        route_distance = graph_dist.get(node_id)
        total_distance = None if route_distance is None else float(route_distance + snap_distance)
        origin_records.append(
            {
                "origin_id": int(row.origin_id),
                "harvested_area_index": float(row.harvested_area_index),
                "origin_snap_distance_m": snap_distance,
                "graph_node_id": int(node_id),
                "route_length_m": total_distance,
                "connected": total_distance is not None,
                "scenario": scenario.name,
            }
        )

    return pd.DataFrame(origin_records), roads_proj


def _write_outputs(
    out_dir: Path,
    baseline: pd.DataFrame,
    scenario_results: dict[str, pd.DataFrame],
    scenario_roads: dict[str, gpd.GeoDataFrame],
    origins_proj: gpd.GeoDataFrame,
    cities_proj: gpd.GeoDataFrame,
    summary: dict,
) -> None:
    ensure_directory(out_dir)
    baseline.to_csv(out_dir / "baseline_routes.csv", index=False)
    origins_proj.to_file(out_dir / "cropland_origins.gpkg", driver="GPKG")
    cities_proj.to_file(out_dir / "cities_over_threshold.gpkg", driver="GPKG")
    for name, frame in scenario_results.items():
        frame.to_csv(out_dir / f"{name}_routes.csv", index=False)
    for name, roads in scenario_roads.items():
        roads.to_file(out_dir / f"{name}_road_status.gpkg", driver="GPKG")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    config = load_config(args.config, country_code_override=args.country_code.upper())
    iso3 = str(config.get("study_area", {}).get("country_code", args.country_code)).upper()
    month_stamp = f"{args.year}-{args.month:02d}"

    country, _admin = _country_layers(project_root, iso3)
    target_crs = str(country.estimate_utm_crs())

    print(f"[road-scenarios] country={iso3} month={month_stamp}", flush=True)
    chirps_path = _ensure_chirps(project_root, args.year, args.month)
    print(f"[road-scenarios] chirps={chirps_path}", flush=True)
    roads = _load_roads(project_root, iso3, country)
    roads = _sample_roads_precipitation(roads, chirps_path)
    print(f"[road-scenarios] roads={len(roads)}", flush=True)

    roads_proj = roads.to_crs(target_crs)
    cities_proj = _load_cities(project_root, iso3, args.city_population_threshold, target_crs)
    print(f"[road-scenarios] cities_over_threshold={len(cities_proj)}", flush=True)
    origins_proj = _load_cropland_origins(project_root, country, target_crs, args.max_origins)
    print(f"[road-scenarios] cropland_origins={len(origins_proj)}", flush=True)

    baseline_scenario = ScenarioConfig(name="baseline", unknown_surface_mode="paved")
    baseline_routes, _baseline_roads = _run_scenario(
        baseline_scenario,
        roads_proj,
        origins_proj,
        cities_proj,
        q_unpaved=2.0,
        q_damaged=2.0,
    )
    print("[road-scenarios] baseline done", flush=True)

    scenarios = [
        ScenarioConfig(name="unknown_as_paved", unknown_surface_mode="paved"),
        ScenarioConfig(name="unknown_as_unpaved", unknown_surface_mode="unpaved"),
    ]

    scenario_results: dict[str, pd.DataFrame] = {}
    scenario_roads: dict[str, gpd.GeoDataFrame] = {}
    comparison_rows: list[dict[str, float | int | str | None]] = []

    for scenario in scenarios:
        print(f"[road-scenarios] running scenario={scenario.name}", flush=True)
        routes, roads_status = _run_scenario(
            scenario,
            roads_proj,
            origins_proj,
            cities_proj,
            q_unpaved=args.road_quantile_unpaved,
            q_damaged=args.road_quantile_damaged,
        )
        merged = baseline_routes.merge(routes[["origin_id", "route_length_m", "connected"]], on="origin_id", suffixes=("_baseline", f"_{scenario.name}"))
        merged["scenario"] = scenario.name
        merged["delta_length_m"] = merged[f"route_length_m_{scenario.name}"] - merged["route_length_m_baseline"]
        comparison_rows.extend(merged.to_dict(orient="records"))
        scenario_results[scenario.name] = routes
        scenario_roads[scenario.name] = roads_status
        print(f"[road-scenarios] scenario={scenario.name} done", flush=True)

    out_dir = project_root / "outputs" / "road_scenarios" / iso3 / month_stamp
    ensure_directory(out_dir)
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(out_dir / "scenario_comparison.csv", index=False)

    summary = {
        "country_code": iso3,
        "month": month_stamp,
        "city_population_threshold": args.city_population_threshold,
        "n_cities": int(len(cities_proj)),
        "n_cropland_origins": int(len(origins_proj)),
        "baseline_connected_origins": int(baseline_routes["connected"].sum()),
        "scenarios": {},
        "dynamic_layers_used": ["CHIRPS monthly precipitation"],
        "static_layers_ready_for_future_overlay": ["road_surface", "GADM", "GEM", "liquefaction", "flood", "FLOPROS"],
    }

    for scenario_name, routes in scenario_results.items():
        roads_status = scenario_roads[scenario_name]
        baseline_series = baseline_routes["route_length_m"].dropna()
        scenario_series = routes["route_length_m"].dropna()
        deltas = comparison.loc[comparison["scenario"] == scenario_name, "delta_length_m"].dropna()
        summary["scenarios"][scenario_name] = {
            "connected_origins": int(routes["connected"].sum()),
            "closed_road_rows": int(roads_status["closed"].sum()),
            "mean_baseline_length_m": None if baseline_series.empty else float(baseline_series.mean()),
            "mean_scenario_length_m": None if scenario_series.empty else float(scenario_series.mean()),
            "mean_delta_length_m": None if deltas.empty else float(deltas.mean()),
            "median_delta_length_m": None if deltas.empty else float(deltas.median()),
        }

    _write_outputs(out_dir, baseline_routes, scenario_results, scenario_roads, origins_proj, cities_proj, summary)
    print(f"[road-scenarios] outputs={out_dir}", flush=True)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
