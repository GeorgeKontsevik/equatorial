"""Shared helpers for loading country boundaries, roads, and fast road probe points."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import LineString, MultiLineString, Point

EXCLUDED_NON_TRUCK_HIGHWAY_CLASSES = {
    "footway",
    "path",
    "steps",
    "pedestrian",
    "cycleway",
    "bridleway",
    "living_street",
}


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
    skip_features: int | None = None,
    max_features: int | None = None,
    road_row_offset: int = 0,
    columns: list[str] | None = None,
    road_backend: str = "parquet",
    postgis_dsn: str = "",
    postgis_table: str = "",
) -> gpd.GeoDataFrame:
    bbox = tuple(country.total_bounds)
    if road_backend == "postgis":
        roads = _load_roads_postgis(
            bbox=bbox,
            geometry_mode=geometry_mode,
            skip_features=skip_features,
            max_features=max_features,
            dsn=postgis_dsn,
            table=postgis_table or f"road_surface_{iso3.lower()}",
        )
    else:
        parquet_path, gpkg_path = _road_surface_paths(project_root, iso3)
        path = parquet_path if parquet_path.exists() else gpkg_path
        if road_backend == "parquet" and not parquet_path.exists():
            print(f"[roads] parquet missing, fallback to gpkg: {gpkg_path}", flush=True)
        read_kwargs = {"bbox": bbox}
        if skip_features is not None:
            read_kwargs["skip_features"] = int(skip_features)
        if max_features is not None:
            read_kwargs["max_features"] = int(max_features)
        if columns is not None and geometry_mode != "probe_point":
            read_kwargs["columns"] = columns
        if path.suffix.lower() == ".parquet":
            roads = gpd.read_parquet(path)
            if roads.crs is None:
                roads = roads.set_crs("EPSG:4326")
            roads = roads.to_crs("EPSG:4326")
            minx, miny, maxx, maxy = bbox
            roads = roads.cx[minx:maxx, miny:maxy]
            if columns is not None:
                keep_cols = [col for col in columns if col in roads.columns]
                roads = roads[["geometry", *keep_cols]].copy()
            if skip_features is not None or max_features is not None:
                start = int(skip_features or 0)
                stop = None if max_features is None else start + int(max_features)
                roads = roads.iloc[start:stop].copy()
            if geometry_mode == "probe_point":
                roads["geometry"] = roads.geometry.apply(geometry_probe_point)
        elif geometry_mode == "probe_point":
            layer_name = str(pyogrio.list_layers(path)[0][0])
            raw_fields = pyogrio.read_info(path, layer=layer_name).get("fields")
            available_fields = set(raw_fields.tolist() if hasattr(raw_fields, "tolist") else (raw_fields or []))
            columns = [
                "combined_surface_DL_priority",
                "combined_surface_osm_priority",
                "osm_surface_class",
                "pred_label",
                "surface",
            ]
            if "highway" in available_fields:
                columns.append("highway")
            sql = (
                "SELECT ST_Line_Interpolate_Point(geom, 0.5) AS geom, "
                + ", ".join(columns)
                + f" FROM {layer_name}"
            )
            roads = pyogrio.read_dataframe(path, sql=sql, **read_kwargs)
        else:
            try:
                roads = pyogrio.read_dataframe(path, **read_kwargs)
            except TypeError as exc:
                # Some pyogrio/GDAL builds intermittently fail with "an integer is required"
                # on this dataset; fall back to geopandas/fiona for robustness.
                print(f"[roads] pyogrio failed ({exc}); falling back to geopandas.read_file", flush=True)
                roads = gpd.read_file(path, bbox=bbox)
                if columns is not None:
                    keep_cols = [col for col in columns if col in roads.columns]
                    roads = roads[["geometry", *keep_cols]].copy()
                if skip_features is not None or max_features is not None:
                    start = int(skip_features or 0)
                    stop = None if max_features is None else start + int(max_features)
                    roads = roads.iloc[start:stop].copy()
    if roads.crs is None:
        roads = roads.set_crs("EPSG:4326")
    roads = roads.to_crs("EPSG:4326")
    geom_name = roads.geometry.name
    if geom_name != "geometry":
        roads = roads.rename(columns={geom_name: "geometry"}).set_geometry("geometry")
    roads = roads.loc[roads.geometry.notna()].copy()
    if "highway" in roads.columns:
        highway = roads["highway"].astype("string").str.lower()
        keep_mask = ~highway.isin(EXCLUDED_NON_TRUCK_HIGHWAY_CLASSES)
        n_drop = int((~keep_mask).sum())
        if n_drop:
            print(f"[roads] filtered_non_truck_classes={n_drop}", flush=True)
        roads = roads.loc[keep_mask].copy()
    roads["surface_group"] = road_surface_class(roads)
    roads["road_row_id"] = road_row_offset + np.arange(len(roads))
    if compute_length_km:
        roads["length_km"] = roads.to_crs(str(country.estimate_utm_crs())).length / 1000.0
    return roads


def _road_surface_paths(project_root: Path, iso3: str) -> tuple[Path, Path]:
    base = project_root / "data" / "raw" / "road_surface" / iso3
    stem = f"heigit_{iso3.lower()}_roadsurface_lines"
    return base / f"{stem}.parquet", base / f"{stem}.gpkg"


def _load_roads_postgis(
    *,
    bbox: tuple[float, float, float, float],
    geometry_mode: str,
    skip_features: int | None,
    max_features: int | None,
    dsn: str,
    table: str,
) -> gpd.GeoDataFrame:
    if not dsn:
        raise ValueError("PostGIS backend selected but `postgis_dsn` is empty.")
    try:
        import sqlalchemy as sa
    except Exception as exc:
        raise RuntimeError("PostGIS backend requires sqlalchemy. Install it in equatorial/.venv.") from exc

    minx, miny, maxx, maxy = bbox
    preferred = [
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
        "highway",
    ]
    engine = sa.create_engine(dsn)
    try:
        geom_col = _postgis_geometry_column(engine, table)
        colmap = _postgis_column_map(engine, table)
        select_parts: list[str] = []
        for col in preferred:
            actual = colmap.get(col.lower())
            if actual is None:
                continue
            select_parts.append(f'"{actual}" AS "{col}"')
        select_cols = ", ".join(select_parts)
        geom_sql = f"ST_LineInterpolatePoint({geom_col}, 0.5)" if geometry_mode == "probe_point" else geom_col
        limit_sql = f" LIMIT {int(max_features)}" if max_features is not None else ""
        offset_sql = f" OFFSET {int(skip_features)}" if skip_features is not None else ""
        sql = f"""
            SELECT {geom_sql} AS geom, {select_cols}
            FROM {table}
            WHERE {geom_col} && ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}, 4326)
            ORDER BY ctid
            {limit_sql}{offset_sql}
        """
        roads = gpd.read_postgis(sql, con=engine, geom_col="geom")
    finally:
        engine.dispose()
    return roads


def count_roads_postgis(
    *,
    bbox: tuple[float, float, float, float],
    dsn: str,
    table: str,
) -> int:
    if not dsn:
        raise ValueError("PostGIS backend selected but `postgis_dsn` is empty.")
    try:
        import sqlalchemy as sa
    except Exception as exc:
        raise RuntimeError("PostGIS backend requires sqlalchemy. Install it in equatorial/.venv.") from exc
    engine = sa.create_engine(dsn)
    try:
        minx, miny, maxx, maxy = bbox
        geom_col = _postgis_geometry_column(engine, table)
        sql = f"""
            SELECT COUNT(*) AS n
            FROM {table}
            WHERE {geom_col} && ST_MakeEnvelope({minx}, {miny}, {maxx}, {maxy}, 4326)
        """
        with engine.connect() as conn:
            n = conn.execute(sa.text(sql)).scalar_one()
    finally:
        engine.dispose()
    return int(n)


def _postgis_geometry_column(engine, table: str) -> str:
    try:
        import sqlalchemy as sa
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """
                    SELECT f_geometry_column
                    FROM public.geometry_columns
                    WHERE f_table_schema = 'public' AND f_table_name = :table
                    LIMIT 1
                    """
                ),
                {"table": table},
            ).fetchone()
            if row and row[0]:
                return str(row[0])
    except Exception:
        pass
    return "geometry"


def _postgis_column_map(engine, table: str) -> dict[str, str]:
    import sqlalchemy as sa

    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=:table
                """
            ),
            {"table": table},
        ).fetchall()
    out: dict[str, str] = {}
    for (name,) in rows:
        if not name:
            continue
        out[str(name).lower()] = str(name)
    return out


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
