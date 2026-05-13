"""Shared helpers for loading country boundaries, roads, and fast road probe points."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import LineString, MultiLineString, Point


def country_layer(project_root: Path, iso3: str) -> gpd.GeoDataFrame:
    gadm_path = project_root / "data" / "raw" / "gadm" / iso3 / f"gadm41_{iso3}.gpkg"
    return gpd.read_file(gadm_path, layer="ADM_ADM_0").to_crs("EPSG:4326")


def road_surface_class(frame: gpd.GeoDataFrame) -> pd.Series:
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


def load_roads(
    project_root: Path,
    iso3: str,
    country: gpd.GeoDataFrame,
    *,
    compute_length_km: bool = False,
    geometry_mode: str = "line",
) -> gpd.GeoDataFrame:
    path = project_root / "data" / "raw" / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    bbox = tuple(country.total_bounds)
    if geometry_mode == "probe_point":
        layer_name = str(pyogrio.list_layers(path)[0][0])
        columns = [
            "combined_surface_DL_priority",
            "combined_surface_osm_priority",
            "osm_surface_class",
            "pred_label",
            "surface",
        ]
        sql = (
            "SELECT ST_Line_Interpolate_Point(geom, 0.5) AS geom, "
            + ", ".join(columns)
            + f" FROM {layer_name}"
        )
        roads = pyogrio.read_dataframe(path, sql=sql, bbox=bbox)
    else:
        roads = pyogrio.read_dataframe(path, bbox=bbox)
    if roads.crs is None:
        roads = roads.set_crs("EPSG:4326")
    roads = roads.to_crs("EPSG:4326")
    roads = roads.loc[roads.geometry.notna()].copy()
    roads["surface_group"] = road_surface_class(roads)
    roads["road_row_id"] = np.arange(len(roads))
    if compute_length_km:
        roads["length_km"] = roads.to_crs(str(country.estimate_utm_crs())).length / 1000.0
    return roads


def geometry_probe_point(geometry) -> Point:
    """Return one fast probe point for first-pass road/raster overlay."""
    if geometry is None or geometry.is_empty:
        return Point()
    if isinstance(geometry, LineString):
        return geometry.interpolate(0.5, normalized=True)
    if isinstance(geometry, MultiLineString):
        longest = max((part for part in geometry.geoms if isinstance(part, LineString)), key=lambda part: part.length, default=None)
        if longest is None:
            return Point()
        return longest.interpolate(0.5, normalized=True)
    centroid = geometry.centroid
    if centroid.is_empty:
        return Point()
    return centroid
