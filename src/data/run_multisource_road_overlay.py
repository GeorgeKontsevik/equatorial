"""Run a first-pass road overlay against all currently downloaded hazard/climate layers."""

from __future__ import annotations

import argparse
import gc
import ast
import json
import math
import re
import shutil
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
import xarray as xr
import yaml
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from shapely.geometry import LineString, MultiLineString, box
from src.data.config import load_config
from src.data.road_input_utils import country_layer, geometry_probe_point, load_roads

try:
    from tqdm.auto import tqdm
except Exception:  # pragma: no cover - progress fallback
    def tqdm(iterable=None, **kwargs):
        return iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Overlay roads with all downloaded sources for one country.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"))
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--damage-config", type=Path, default=Path("config/road_climate_damage.yaml"))
    parser.add_argument(
        "--road-geometry-mode",
        choices=("line", "probe_point"),
        default="line",
        help="Use `probe_point` for heavy annual runs when boxplots matter more than full road line output.",
    )
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel workers for independent weekly computations.")
    parser.add_argument("--road-backend", choices=("parquet", "gpkg", "postgis"), default="parquet", help="Road source backend.")
    parser.add_argument("--postgis-dsn", type=str, default="", help="SQLAlchemy DSN for PostGIS, e.g. postgresql+psycopg://user:pass@host:5432/db")
    parser.add_argument("--postgis-table", type=str, default="", help="PostGIS table name for roads, default road_surface_<iso3>.")
    parser.add_argument("--point-batch-size", type=int, default=250_000, help="Batch size for large point sampling arrays.")
    parser.add_argument("--road-chunk-size", type=int, default=50_000, help="Number of road features to process per overlay chunk.")
    parser.add_argument("--max-road-chunks", type=int, default=None, help="Optional debug limit for road chunks.")
    parser.add_argument("--multiscale-road-merge", action="store_true", help="Sample source layers on per-source cell/surface representatives, then map values back to road rows.")
    parser.add_argument("--era5-cell-m", type=float, default=11000.0)
    parser.add_argument("--chirps-cell-m", type=float, default=5500.0)
    parser.add_argument("--flood-cell-m", type=float, default=20.0)
    parser.add_argument("--visibility-cell-m", type=float, default=50000.0)
    parser.add_argument("--skip-era5-daily-sum-max", action="store_true", help="Skip expensive ERA5 daily precipitation max derived metric.")
    parser.add_argument(
        "--era5-precip-only",
        action="store_true",
        help="Restrict ERA5 overlay to precipitation-only metrics and skip wind/temperature/soil/gust/rate fields.",
    )
    parser.add_argument(
        "--compact-weekly-logs",
        action="store_true",
        help="Print compact weekly country progress only (minimal per-factor logs).",
    )
    parser.add_argument("--output-root", type=Path, default=None, help="Custom output directory root for this run.")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _relpath(path: Path, project_root: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _sample_raster_paths(
    paths: list[Path],
    probe_points_wgs84: gpd.GeoSeries,
    *,
    reducer: str = "max",
    positive_only: bool = False,
    progress_label: str | None = None,
) -> np.ndarray:
    if not paths:
        return np.full(len(probe_points_wgs84), np.nan, dtype="float64")

    samples: list[np.ndarray] = []
    iterator = paths
    if progress_label and len(paths) > 1:
        iterator = tqdm(paths, desc=progress_label, unit="raster", leave=False)
    for path in iterator:
        with rasterio.open(path) as src:
            samples.append(_sample_raster_band(src, probe_points_wgs84, positive_only=positive_only))

    if len(samples) == 1:
        return samples[0]

    stack = np.vstack(samples)
    if reducer == "sum":
        with np.errstate(invalid="ignore"):
            return np.nansum(stack, axis=0)
    if reducer == "mean":
        with np.errstate(invalid="ignore"):
            return np.nanmean(stack, axis=0)
    if reducer == "first_valid":
        out = np.full(stack.shape[1], np.nan, dtype="float64")
        for row in stack:
            missing = ~np.isfinite(out)
            out[missing] = row[missing]
        return out
    all_nan = np.isnan(stack).all(axis=0)
    out = np.full(stack.shape[1], np.nan, dtype="float64")
    valid_cols = ~all_nan
    if valid_cols.any():
        with np.errstate(invalid="ignore"):
            out[valid_cols] = np.nanmax(stack[:, valid_cols], axis=0)
    return out


def _sample_raster_band(
    src: rasterio.io.DatasetReader,
    probe_points_wgs84: gpd.GeoSeries,
    *,
    positive_only: bool = False,
) -> np.ndarray:
    if src.crs:
        points_src = probe_points_wgs84.to_crs(src.crs)
    else:
        points_src = probe_points_wgs84

    out = np.full(len(points_src), np.nan, dtype="float64")
    xs = np.asarray([geom.x if geom and not geom.is_empty else np.nan for geom in points_src], dtype="float64")
    ys = np.asarray([geom.y if geom and not geom.is_empty else np.nan for geom in points_src], dtype="float64")
    finite = np.isfinite(xs) & np.isfinite(ys)
    if not finite.any():
        return out

    rows, cols = rasterio.transform.rowcol(src.transform, xs[finite], ys[finite])
    rows = np.asarray(rows, dtype="int64")
    cols = np.asarray(cols, dtype="int64")
    in_bounds = (rows >= 0) & (cols >= 0) & (rows < src.height) & (cols < src.width)
    if not in_bounds.any():
        return out

    valid_positions = np.flatnonzero(finite)[in_bounds]
    valid_cells = np.column_stack((rows[in_bounds], cols[in_bounds]))
    unique_cells, inverse = np.unique(valid_cells, axis=0, return_inverse=True)
    unique_values = _read_unique_raster_cells(src, unique_cells)
    sampled = unique_values[inverse]

    nodata = src.nodata
    if nodata is not None:
        sampled[sampled == nodata] = np.nan
    sampled[~np.isfinite(sampled)] = np.nan
    if positive_only:
        sampled[sampled <= 0] = np.nan
    out[valid_positions] = sampled
    return out


def _read_unique_raster_cells(src: rasterio.io.DatasetReader, unique_cells: np.ndarray) -> np.ndarray:
    if unique_cells.size == 0:
        return np.empty(0, dtype="float64")

    block_height, block_width = src.block_shapes[0] if src.block_shapes else (src.height, src.width)
    if not block_height or not block_width:
        block_height, block_width = src.height, src.width

    block_rows = unique_cells[:, 0] // block_height
    block_cols = unique_cells[:, 1] // block_width
    block_keys = np.column_stack((block_rows, block_cols))
    order = np.lexsort((block_keys[:, 1], block_keys[:, 0]))
    values = np.full(unique_cells.shape[0], np.nan, dtype="float64")

    start = 0
    while start < len(order):
        current = order[start]
        block_row = int(block_keys[current, 0])
        block_col = int(block_keys[current, 1])
        end = start + 1
        while end < len(order):
            candidate = order[end]
            if block_keys[candidate, 0] != block_row or block_keys[candidate, 1] != block_col:
                break
            end += 1

        block_indices = order[start:end]
        row_off = block_row * block_height
        col_off = block_col * block_width
        height = min(block_height, src.height - row_off)
        width = min(block_width, src.width - col_off)
        window = Window(col_off=col_off, row_off=row_off, width=width, height=height)
        block = np.asarray(src.read(1, window=window), dtype="float64")
        local_rows = unique_cells[block_indices, 0] - row_off
        local_cols = unique_cells[block_indices, 1] - col_off
        values[block_indices] = block[local_rows, local_cols]
        start = end

    return values


def _multiscale_group_index(
    roads: gpd.GeoDataFrame,
    probe_points_wgs84: gpd.GeoSeries,
    *,
    cell_m: float,
) -> tuple[np.ndarray, np.ndarray] | None:
    if cell_m <= 0 or roads.empty:
        return None
    try:
        projected_crs = roads.estimate_utm_crs() or "EPSG:3857"
        points = probe_points_wgs84.to_crs(projected_crs)
    except Exception:
        points = probe_points_wgs84.to_crs("EPSG:3857")
    xs = np.asarray([geom.x if geom and not geom.is_empty else np.nan for geom in points], dtype="float64")
    ys = np.asarray([geom.y if geom and not geom.is_empty else np.nan for geom in points], dtype="float64")
    ix = np.floor(xs / float(cell_m)).astype("float64")
    iy = np.floor(ys / float(cell_m)).astype("float64")
    surface = roads.get("surface_group", pd.Series("unknown", index=roads.index)).astype("string").fillna("unknown")
    keys = pd.Series(ix).astype("Int64").astype(str) + ":" + pd.Series(iy).astype("Int64").astype(str) + ":" + surface.reset_index(drop=True).astype(str)
    codes, _ = pd.factorize(keys, sort=False)
    representatives = pd.Series(np.arange(len(codes))).groupby(codes, sort=False).first().to_numpy(dtype=int)
    return representatives, codes.astype(int)


def _expand_multiscale_values(values: np.ndarray, groups: tuple[np.ndarray, np.ndarray] | None, n_rows: int) -> np.ndarray:
    if groups is None:
        return values
    representatives, inverse = groups
    if len(representatives) == n_rows:
        return values
    return np.asarray(values)[inverse]


def _road_unit_vectors(roads: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    projected = roads.to_crs(roads.estimate_utm_crs() or "EPSG:3857")
    ux = np.zeros(len(projected), dtype="float64")
    uy = np.ones(len(projected), dtype="float64")

    for idx, geom in enumerate(projected.geometry):
        line: LineString | None = None
        if isinstance(geom, LineString):
            line = geom
        elif isinstance(geom, MultiLineString) and geom.geoms:
            line = max((part for part in geom.geoms if isinstance(part, LineString)), key=lambda part: part.length, default=None)
        if line is None or line.is_empty:
            continue
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        start = coords[0]
        end = coords[-1]
        dx = float(end[0] - start[0])
        dy = float(end[1] - start[1])
        length = float(np.hypot(dx, dy))
        if length <= 0:
            continue
        ux[idx] = dx / length
        uy[idx] = dy / length

    return ux, uy


def _period_months(start: date, end: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append((cursor.year, cursor.month))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _period_week_starts(start: date, end: date, step_days: int) -> list[date]:
    weeks: list[date] = []
    cursor = start
    while cursor <= end:
        weeks.append(cursor)
        cursor += timedelta(days=step_days)
    return weeks


def _analysis_date(value: object, fallback: str) -> date:
    raw = str(value or fallback)
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _month_token(year: int, month: int) -> str:
    return f"{year:04d}_{month:02d}"


def _week_token(week_start: date) -> str:
    return week_start.isoformat().replace("-", "_")


def _netcdf_time_dim(da: xr.DataArray) -> str | None:
    return next((name for name in ["valid_time", "time"] if name in da.dims), None)


def _sample_netcdf_timeseries(
    da: xr.DataArray,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    start_time: pd.Timestamp | None = None,
    end_time: pd.Timestamp | None = None,
    include_end_time: bool = False,
) -> np.ndarray:
    arr = da
    time_dim = _netcdf_time_dim(arr)
    if time_dim is not None:
        time_index = pd.to_datetime(arr[time_dim].values)
        if start_time is not None and end_time is not None:
            mask = (time_index >= start_time) & (time_index <= end_time if include_end_time else time_index < end_time)
            if not mask.any():
                return np.full((0, lons.shape[0]), np.nan, dtype="float64")
            positions = np.flatnonzero(mask)
            selected_times = time_index[positions]
            positions = positions[~selected_times.duplicated()]
            arr = arr.isel({time_dim: positions})
        elif start_date is not None and end_date is not None:
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1)
            mask = (time_index >= start_ts) & (time_index < end_ts)
            if not mask.any():
                return np.full((0, lons.shape[0]), np.nan, dtype="float64")
            positions = np.flatnonzero(mask)
            selected_times = time_index[positions]
            positions = positions[~selected_times.duplicated()]
            arr = arr.isel({time_dim: positions})

    lon_dim = next((name for name in ["longitude", "lon", "x"] if name in arr.dims), None)
    lat_dim = next((name for name in ["latitude", "lat", "y"] if name in arr.dims), None)
    if lon_dim is None or lat_dim is None:
        return np.full((0, lons.shape[0]), np.nan, dtype="float64")

    sample_lons = lons
    lon_values = np.asarray(arr[lon_dim].values, dtype="float64")
    if np.nanmin(lon_values) >= 0.0 and np.nanmax(lon_values) > 180.0:
        sample_lons = np.mod(sample_lons, 360.0)

    sampled = arr.sel(
        {
            lon_dim: xr.DataArray(sample_lons, dims="points"),
            lat_dim: xr.DataArray(lats, dims="points"),
        },
        method="nearest",
    )
    values = np.asarray(sampled.values, dtype="float64")
    values[~np.isfinite(values)] = np.nan
    if time_dim is None or values.ndim == 1:
        return values.reshape(1, -1)
    return values


def _reduce_sampled_timeseries(values: np.ndarray, reducer: str, n_points: int) -> np.ndarray:
    if values.size == 0:
        return np.full(n_points, np.nan, dtype="float64")
    if reducer == "sum":
        with np.errstate(invalid="ignore"):
            return np.nansum(values, axis=0)
    if reducer == "max":
        all_nan = np.isnan(values).all(axis=0)
        out = np.full(n_points, np.nan, dtype="float64")
        valid_cols = ~all_nan
        if valid_cols.any():
            with np.errstate(invalid="ignore"):
                out[valid_cols] = np.nanmax(values[:, valid_cols], axis=0)
        return out
    if reducer == "min":
        all_nan = np.isnan(values).all(axis=0)
        out = np.full(n_points, np.nan, dtype="float64")
        valid_cols = ~all_nan
        if valid_cols.any():
            with np.errstate(invalid="ignore"):
                out[valid_cols] = np.nanmin(values[:, valid_cols], axis=0)
        return out
    all_nan = np.isnan(values).all(axis=0)
    out = np.full(n_points, np.nan, dtype="float64")
    valid_cols = ~all_nan
    if valid_cols.any():
        with np.errstate(invalid="ignore"):
            out[valid_cols] = np.nanmean(values[:, valid_cols], axis=0)
    return out


def _sample_netcdf_var(
    da: xr.DataArray,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    reducer: str = "mean",
) -> np.ndarray:
    sampled = _sample_netcdf_timeseries(da, lons, lats, start_date=start_date, end_date=end_date)
    return _reduce_sampled_timeseries(sampled, reducer, lons.shape[0])


def _sample_netcdf_var_chunked(
    da: xr.DataArray,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    reducer: str = "mean",
    batch_size: int = 250_000,
) -> np.ndarray:
    if len(lons) <= batch_size:
        return _sample_netcdf_var(da, lons, lats, start_date=start_date, end_date=end_date, reducer=reducer)
    out = np.full(len(lons), np.nan, dtype="float64")
    for start in range(0, len(lons), batch_size):
        stop = min(start + batch_size, len(lons))
        out[start:stop] = _sample_netcdf_var(
            da,
            lons[start:stop],
            lats[start:stop],
            start_date=start_date,
            end_date=end_date,
            reducer=reducer,
        )
    return out


def _sample_netcdf_wind_speed(
    ds: xr.Dataset,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    reducer: str = "max",
) -> np.ndarray:
    if "u10" not in ds or "v10" not in ds:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    u = _sample_netcdf_timeseries(ds["u10"], lons, lats, start_date=start_date, end_date=end_date)
    v = _sample_netcdf_timeseries(ds["v10"], lons, lats, start_date=start_date, end_date=end_date)
    if u.shape != v.shape:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    return _reduce_sampled_timeseries(np.sqrt(u * u + v * v), reducer, lons.shape[0])


def _sample_netcdf_crosswind_speed(
    ds: xr.Dataset,
    lons: np.ndarray,
    lats: np.ndarray,
    road_ux: np.ndarray,
    road_uy: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    reducer: str = "max",
) -> np.ndarray:
    if "u10" not in ds or "v10" not in ds:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    u = _sample_netcdf_timeseries(ds["u10"], lons, lats, start_date=start_date, end_date=end_date)
    v = _sample_netcdf_timeseries(ds["v10"], lons, lats, start_date=start_date, end_date=end_date)
    if u.shape != v.shape:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    normal_x = -road_uy.reshape(1, -1)
    normal_y = road_ux.reshape(1, -1)
    crosswind = np.abs(u * normal_x + v * normal_y)
    return _reduce_sampled_timeseries(crosswind, reducer, lons.shape[0])


def _era5_hourly_increment_mm(values_m: np.ndarray) -> np.ndarray:
    if values_m.size == 0:
        return values_m
    values = np.asarray(values_m, dtype="float64") * 1000.0
    if values.shape[0] == 0:
        return values
    increments = np.empty_like(values)
    increments[0, :] = values[0, :]
    diff = np.diff(values, axis=0)
    reset_mask = diff < -0.01
    increments[1:, :] = np.where(reset_mask, values[1:, :], np.maximum(diff, 0.0))
    increments[~np.isfinite(increments)] = np.nan
    increments[increments < 0.0] = 0.0
    return increments


def _sample_era5_tp_1h_max_mm_per_h(
    ds: xr.Dataset,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> np.ndarray:
    if "tp" not in ds:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    start_time = pd.Timestamp(start_date) + pd.Timedelta(hours=1) if start_date is not None else None
    end_time = pd.Timestamp(end_date) + pd.Timedelta(days=1) if end_date is not None else None
    sampled = _sample_netcdf_timeseries(
        ds["tp"],
        lons,
        lats,
        start_time=start_time,
        end_time=end_time,
        include_end_time=True,
    )
    increments = _era5_hourly_increment_mm(sampled)
    return _reduce_sampled_timeseries(increments, "max", lons.shape[0])


def _sample_era5_tp_daily_sum_weekly_max_mm(
    ds: xr.Dataset,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date,
    end_date: date,
) -> np.ndarray:
    if "tp" not in ds:
        return np.full(lons.shape[0], np.nan, dtype="float64")

    daily_sums: list[np.ndarray] = []
    for day in _iter_days(start_date, end_date):
        sampled = _sample_netcdf_timeseries(
            ds["tp"],
            lons,
            lats,
            start_time=pd.Timestamp(day) + pd.Timedelta(hours=1),
            end_time=pd.Timestamp(day) + pd.Timedelta(days=1),
            include_end_time=True,
        )
        increments = _era5_hourly_increment_mm(sampled)
        daily_sums.append(_reduce_sampled_timeseries(increments, "sum", lons.shape[0]))

    if not daily_sums:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    stacked = np.vstack(daily_sums)
    all_nan = np.isnan(stacked).all(axis=0)
    out = np.full(lons.shape[0], np.nan, dtype="float64")
    valid_cols = ~all_nan
    if valid_cols.any():
        with np.errstate(invalid="ignore"):
            out[valid_cols] = np.nanmax(stacked[:, valid_cols], axis=0)
    return out


def _find_first_var(ds: xr.Dataset, candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in ds), None)


def _sample_era5_rate_mm_per_h(
    ds: xr.Dataset,
    candidates: tuple[str, ...],
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> np.ndarray:
    name = _find_first_var(ds, candidates)
    if name is None:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    sampled = _sample_netcdf_timeseries(ds[name], lons, lats, start_date=start_date, end_date=end_date)
    units = str(ds[name].attrs.get("units", "")).lower()
    factor = 3600.0 if "s" in units else 1.0
    return _reduce_sampled_timeseries(sampled * factor, "max", lons.shape[0])


def _sample_era5_gust_speed(
    ds: xr.Dataset,
    lons: np.ndarray,
    lats: np.ndarray,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> np.ndarray:
    name = _find_first_var(ds, ("fg10", "i10fg", "10fg"))
    if name is None:
        return np.full(lons.shape[0], np.nan, dtype="float64")
    sampled = _sample_netcdf_timeseries(ds[name], lons, lats, start_date=start_date, end_date=end_date)
    return _reduce_sampled_timeseries(sampled, "max", lons.shape[0])


def _open_era5_dataset(paths: list[Path]) -> xr.Dataset:
    if not paths:
        raise FileNotFoundError("No ERA5 files were provided for overlay.")
    datasets = [_normalize_era5_dataset(xr.open_dataset(path)) for path in paths]
    if len(datasets) == 1:
        return datasets[0]
    try:
        time_dim = next((name for name in ["time", "valid_time"] if name in datasets[0].dims), None)
        if time_dim is None:
            return xr.combine_by_coords(datasets, coords="minimal", compat="override")
        combined = xr.concat(datasets, dim=time_dim, coords="minimal", compat="override")
        return combined.sortby(time_dim)
    except Exception:
        for ds in datasets:
            ds.close()
        raise


def _normalize_era5_dataset(ds: xr.Dataset) -> xr.Dataset:
    if "valid_time" in ds.dims and "time" not in ds.dims:
        ds = ds.rename({"valid_time": "time"})
    elif "valid_time" in ds.coords and "time" not in ds.coords:
        ds = ds.rename({"valid_time": "time"})
    drop_names = [
        name
        for name in ("expver", "depthBelowLandLayer", "number", "step", "surface")
        if name in ds.coords and name not in ds.dims
    ]
    if drop_names:
        ds = ds.drop_vars(drop_names, errors="ignore")
    return ds


def _era5_paths_for_window(paths: list[Path], start_date: date, end_date: date) -> list[Path]:
    if len(paths) <= 1:
        return paths
    selected: list[Path] = []
    for path in paths:
        match = re.search(r"-(\d{4})-(\d{2})\.nc$", path.name)
        if match is None:
            return paths
        year = int(match.group(1))
        month = int(match.group(2))
        month_start = date(year, month, 1)
        if month == 12:
            month_end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(year, month + 1, 1) - timedelta(days=1)
        if month_start <= end_date and month_end >= start_date:
            selected.append(path)
    return selected or paths


def _iter_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _week_end(week_start: date, end_date: date, step_days: int) -> date:
    return min(end_date, week_start + timedelta(days=step_days - 1))


def _format_suffix_from_request(request: dict) -> str:
    format_hint = str(request.get("data_format") or request.get("format") or "netcdf").lower()
    return ".grib" if "grib" in format_hint else ".nc"


def _era5_paths_from_config(
    raw_root: Path,
    era5_cfg: dict,
    *,
    analysis_start: date,
    analysis_end: date,
) -> list[Path]:
    era5_root = raw_root / "era5"
    raw_source_files = era5_cfg.get("source_files")
    if isinstance(raw_source_files, list) and raw_source_files:
        paths = [era5_root / str(name).strip() for name in raw_source_files if str(name).strip()]
        if paths:
            return paths
    if isinstance(era5_cfg.get("requests"), list) and era5_cfg["requests"]:
        paths: list[Path] = []
        for item in era5_cfg["requests"]:
            if not isinstance(item, dict):
                continue
            target_name = str(item.get("target_filename", "")).strip()
            if target_name:
                paths.append(era5_root / target_name)
        return paths

    split_request_by = str(era5_cfg.get("split_request_by", "")).strip().lower()
    if split_request_by in {"daily", "weekly"}:
        request = dict(era5_cfg.get("request") or {})
        if not request:
            raise ValueError(f"ERA5 {split_request_by} overlay requires datasets.era5.request in the config.")
        target_prefix = str(era5_cfg.get("target_prefix", "era5")).strip() or "era5"
        default_step_days = 1 if split_request_by == "daily" else 7
        step_days = int(era5_cfg.get("request_step_days", era5_cfg.get("step_days", default_step_days)))
        source_start = _analysis_date(era5_cfg.get("start_date"), analysis_start.isoformat())
        source_end = _analysis_date(era5_cfg.get("end_date"), analysis_end.isoformat())
        window_start = max(analysis_start, source_start)
        window_end = min(analysis_end, source_end)
        if window_end < window_start:
            raise ValueError(
                f"ERA5 source window {source_start.isoformat()}..{source_end.isoformat()} "
                f"does not overlap analysis period {analysis_start.isoformat()}..{analysis_end.isoformat()}."
            )
        suffix = _format_suffix_from_request(request)
        return [era5_root / f"{target_prefix}-{week_start.isoformat()}{suffix}" for week_start in _period_week_starts(window_start, window_end, step_days)]

    era5_target = str(era5_cfg.get("target_filename", "")).strip()
    if not era5_target:
        return []

    target_path = era5_root / era5_target
    if target_path.exists():
        return [target_path]

    fallback_paths = _fallback_era5_monthly_paths(era5_root, era5_target, analysis_start, analysis_end)
    return fallback_paths or [target_path]


def _fallback_era5_monthly_paths(era5_root: Path, era5_target: str, analysis_start: date, analysis_end: date) -> list[Path]:
    target = era5_target.strip().lower()
    location_slug = ""
    hourly_match = re.match(r"^era5-land-hourly-([a-z0-9-]+)-\d{4}-(?:q\d|[a-z0-9_-]+)\.nc$", target)
    if hourly_match:
        location_slug = hourly_match.group(1)
    else:
        match = re.match(r"^era5-land-([a-z0-9-]+)\.nc$", target)
        if match:
            location_slug = match.group(1)
    if not location_slug:
        return []
    monthly_paths = [
        era5_root / f"era5-land-hourly-{location_slug}-{year:04d}-{month:02d}.nc"
        for year, month in _period_months(analysis_start, analysis_end)
    ]
    existing = [path for path in monthly_paths if path.exists()]
    return existing if len(existing) == len(monthly_paths) else []


def _chirps_daily_paths_for_week(
    raw_root: Path,
    *,
    version: str,
    daily_variant: str,
    week_start: date,
    week_end: date,
) -> list[Path]:
    paths: list[Path] = []
    for day in _iter_days(week_start, week_end):
        path = (
            raw_root
            / "chirps"
            / "global"
            / "daily"
            / daily_variant
            / str(day.year)
            / f"chirps-{version}.{daily_variant}.{day:%Y.%m.%d}.tif"
        )
        if not path.exists():
            raise FileNotFoundError(f"Missing CHIRPS daily raster for {day.isoformat()}: {path}")
        paths.append(path)
    return paths


def _flood_paths_by_week_start(
    project_root: Path,
    raw_root: Path,
    week_starts: list[date],
    *,
    bbox: tuple[float, float, float, float] | None = None,
    progress_label: str | None = None,
) -> dict[date, list[Path]]:
    flood_paths = sorted((raw_root / "flood" / "copernicus_gfm" / "GFM").glob("2024/*/*.tif"))
    by_week: dict[date, list[Path]] = {week_start: [] for week_start in week_starts}
    if not flood_paths:
        return by_week
    raster_has_positive_cache: dict[Path, bool] = {}

    def _raster_intersects_bbox(path: Path, query_bbox: tuple[float, float, float, float]) -> bool:
        try:
            with rasterio.open(path) as src:
                bounds = src.bounds
                if src.crs is not None and str(src.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
                    minx, miny, maxx, maxy = transform_bounds(src.crs, "EPSG:4326", *bounds, densify_pts=21)
                else:
                    minx, miny, maxx, maxy = bounds.left, bounds.bottom, bounds.right, bounds.top
        except Exception:
            return False
        qminx, qminy, qmaxx, qmaxy = query_bbox
        return max(float(minx), qminx) <= min(float(maxx), qmaxx) and max(float(miny), qminy) <= min(float(maxy), qmaxy)

    def _raster_has_positive_data_in_bbox(path: Path, query_bbox: tuple[float, float, float, float]) -> bool:
        cached = raster_has_positive_cache.get(path)
        if cached is not None:
            return cached
        try:
            with rasterio.open(path) as src:
                if src.crs is not None and str(src.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
                    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", src.crs, *query_bbox, densify_pts=21)
                else:
                    minx, miny, maxx, maxy = query_bbox
                window = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=src.transform)
                full = Window(col_off=0, row_off=0, width=src.width, height=src.height)
                try:
                    window = window.intersection(full).round_offsets().round_lengths()
                except Exception:
                    raster_has_positive_cache[path] = False
                    return False
                if window.width <= 0 or window.height <= 0:
                    raster_has_positive_cache[path] = False
                    return False
                arr = src.read(1, window=window, masked=True)
        except Exception:
            raster_has_positive_cache[path] = False
            return False
        if np.ma.isMaskedArray(arr):
            has_positive = bool(np.any(arr.compressed() > 0))
        else:
            with np.errstate(invalid="ignore"):
                has_positive = bool(np.any(np.asarray(arr) > 0))
        raster_has_positive_cache[path] = has_positive
        return has_positive

    def _assign_by_filename_timestamp() -> dict[date, list[Path]]:
        fallback: dict[date, list[Path]] = {week_start: [] for week_start in week_starts}
        week_ranges = [(week_start, week_start + timedelta(days=6)) for week_start in week_starts]
        iterator = flood_paths
        if progress_label and len(flood_paths) > 1:
            iterator = tqdm(flood_paths, desc=f"{progress_label} fallback", unit="raster", leave=False)
        for path in iterator:
            match = re.search(r"ENSEMBLE_FLOOD_(\d{8})T\d{6}", path.name)
            if match is None:
                continue
            stamp = datetime.strptime(match.group(1), "%Y%m%d").date()
            for week_start, week_end in week_ranges:
                if week_start <= stamp <= week_end:
                    fallback[week_start].append(path)
                    break
        return fallback

    catalog_path = project_root / "data" / "metadata" / "catalog.csv"
    if not catalog_path.exists():
        return _assign_by_filename_timestamp()

    catalog = pd.read_csv(catalog_path)
    required = {"dataset_name", "local_path", "notes"}
    if not required.issubset(catalog.columns):
        raise ValueError(f"Flood catalog must contain columns {sorted(required)}: {catalog_path}")

    flood_path_set = set(flood_paths)
    flood_rows = catalog.loc[catalog["dataset_name"].astype("string") == "flood"].copy()
    if bbox is not None and "bbox_if_known" in flood_rows.columns:
        query_bbox = tuple(float(x) for x in bbox)

        def _intersects(raw: object) -> bool:
            try:
                values = ast.literal_eval(str(raw))
                if not isinstance(values, list) or len(values) != 4:
                    return False
                minx, miny, maxx, maxy = (float(x) for x in values)
            except Exception:
                return False
            qminx, qminy, qmaxx, qmaxy = query_bbox
            return max(minx, qminx) <= min(maxx, qmaxx) and max(miny, qminy) <= min(maxy, qmaxy)

        flood_rows = flood_rows.loc[flood_rows["bbox_if_known"].map(_intersects)].copy()
    row_iterator = flood_rows.itertuples(index=False)
    if progress_label and len(flood_rows) > 1:
        row_iterator = tqdm(list(row_iterator), desc=f"{progress_label} catalog", unit="row", leave=False)
    for row in row_iterator:
        local_path = project_root / str(row.local_path)
        if local_path not in flood_path_set:
            continue
        if bbox is not None and not _raster_intersects_bbox(local_path, tuple(float(x) for x in bbox)):
            continue
        if bbox is not None and not _raster_has_positive_data_in_bbox(local_path, tuple(float(x) for x in bbox)):
            continue
        match = re.search(r"weekly window (\d{4}-\d{2}-\d{2})\.\.", str(row.notes))
        if match is None:
            continue
        week_start = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        if week_start in by_week:
            by_week[week_start].append(local_path)

    if any(by_week.values()) or bbox is not None:
        return by_week
    return _assign_by_filename_timestamp()


def _flood_depth_paths_by_week_start(raw_root: Path, iso3: str, week_starts: list[date]) -> dict[date, list[Path]]:
    root = raw_root / "flood_depth" / iso3
    by_week: dict[date, list[Path]] = {week_start: [] for week_start in week_starts}
    if not root.exists():
        return by_week

    static_paths = sorted(root.glob("*static*.tif")) + sorted(root.glob("*scenario*.tif"))
    for week_start in week_starts:
        by_week[week_start].extend(static_paths)

    for tif in sorted(root.glob("*.tif")):
        stem = tif.stem
        for week_start in week_starts:
            iso_token = week_start.isoformat()
            underscore_token = _week_token(week_start)
            if iso_token in stem or underscore_token in stem:
                by_week[week_start].append(tif)
    return {week_start: sorted(set(paths)) for week_start, paths in by_week.items()}


def _visibility_values_m(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.extract(r"^(\d+)")[0].pipe(pd.to_numeric, errors="coerce")
    values = values.where(values < 999999)
    return values.astype("float64")


def _sample_noaa_visibility_weekly_min_m(
    raw_root: Path,
    iso3: str,
    probe_points_wgs84: gpd.GeoSeries,
    *,
    week_start: date,
    week_end: date,
) -> np.ndarray:
    root = raw_root / "visibility_noaa_isd" / iso3
    station_path = root / "stations.csv"
    if not station_path.exists():
        return np.full(len(probe_points_wgs84), np.nan, dtype="float64")

    stations = pd.read_csv(station_path)
    if stations.empty or not {"station_id", "lat_num", "lon_num"}.issubset(stations.columns):
        return np.full(len(probe_points_wgs84), np.nan, dtype="float64")

    week_start_ts = pd.Timestamp(week_start)
    week_end_ts = pd.Timestamp(week_end) + pd.Timedelta(days=1)
    rows: list[dict[str, float]] = []
    for station in stations.itertuples(index=False):
        station_id = str(getattr(station, "station_id"))
        station_values: list[pd.Series] = []
        for csv_path in sorted((root / str(week_start.year)).glob(f"{station_id}.csv")) + sorted(
            (root / str(week_end.year)).glob(f"{station_id}.csv")
        ):
            try:
                frame = pd.read_csv(csv_path, usecols=["DATE", "VIS"])
            except Exception:
                continue
            frame["DATE"] = pd.to_datetime(frame["DATE"], errors="coerce", utc=True).dt.tz_convert(None)
            frame["visibility_m"] = _visibility_values_m(frame["VIS"])
            in_week = frame.loc[(frame["DATE"] >= week_start_ts) & (frame["DATE"] < week_end_ts), "visibility_m"].dropna()
            if not in_week.empty:
                station_values.append(in_week)
        if not station_values:
            continue
        values = pd.concat(station_values, ignore_index=True)
        if values.empty:
            continue
        rows.append(
            {
                "lat": float(getattr(station, "lat_num")),
                "lon": float(getattr(station, "lon_num")),
                "visibility_m": float(values.min()),
            }
        )

    if not rows:
        return np.full(len(probe_points_wgs84), np.nan, dtype="float64")

    station_lons = np.asarray([row["lon"] for row in rows], dtype="float64")
    station_lats = np.asarray([row["lat"] for row in rows], dtype="float64")
    station_vis = np.asarray([row["visibility_m"] for row in rows], dtype="float64")
    probe_lons = np.asarray([geom.x for geom in probe_points_wgs84], dtype="float64")
    probe_lats = np.asarray([geom.y for geom in probe_points_wgs84], dtype="float64")
    distances = (probe_lons.reshape(-1, 1) - station_lons.reshape(1, -1)) ** 2 + (
        probe_lats.reshape(-1, 1) - station_lats.reshape(1, -1)
    ) ** 2
    nearest = np.nanargmin(distances, axis=1)
    return station_vis[nearest]


def _local_percentile(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.rank(method="average", pct=True) * 100.0


def _layer_stats(frame: gpd.GeoDataFrame, layer_columns: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    n_total = int(len(frame))
    for col in layer_columns:
        series = pd.to_numeric(frame[col], errors="coerce")
        finite = series[np.isfinite(series)]
        positive = finite[finite > 0]
        rows.append(
            {
                "layer": col,
                "n_total_roads": n_total,
                "n_with_value": int(finite.size),
                "n_positive": int(positive.size),
                "min": None if finite.empty else float(finite.min()),
                "max": None if finite.empty else float(finite.max()),
                "mean": None if finite.empty else float(finite.mean()),
            }
        )
    return rows


def _add_flopros(roads: gpd.GeoDataFrame, probe_points_wgs84: gpd.GeoSeries, raw_root: Path) -> tuple[gpd.GeoDataFrame, list[str]]:
    shp = raw_root / "flopros" / "global" / "original" / "Scussolini_etal_Suppl_info" / "FLOPROS_shp_V1" / "FLOPROS_shp_V1.shp"
    if not shp.exists():
        roads["flopros_merl_riv"] = np.nan
        roads["flopros_dl_max_riv"] = np.nan
        return roads, []
    flopros = gpd.read_file(shp)[["MerL_Riv", "DL_Max_Riv", "geometry"]].to_crs("EPSG:4326")
    probes = gpd.GeoDataFrame({"road_row_id": roads["road_row_id"].values}, geometry=probe_points_wgs84, crs="EPSG:4326")
    joined = gpd.sjoin(probes, flopros, how="left", predicate="within")
    grouped = joined.groupby("road_row_id")[["MerL_Riv", "DL_Max_Riv"]].max()
    roads = roads.merge(grouped, left_on="road_row_id", right_index=True, how="left")
    roads = roads.rename(columns={"MerL_Riv": "flopros_merl_riv", "DL_Max_Riv": "flopros_dl_max_riv"})
    return roads, ["flopros_merl_riv", "flopros_dl_max_riv"]


def _overlay_bbox(config: dict[str, object]) -> tuple[float, float, float, float] | None:
    datasets = config.get("datasets", {}) if isinstance(config, dict) else {}
    for key in ("gadm", "era5", "chirps"):
        raw = datasets.get(key, {}).get("bbox") if isinstance(datasets.get(key), dict) else None
        if isinstance(raw, list) and len(raw) == 4:
            return tuple(float(value) for value in raw)
    return None


def _country_or_bbox_geometry(project_root: Path, iso3: str, config: dict[str, object]) -> gpd.GeoDataFrame:
    bbox = _overlay_bbox(config)
    if bbox is None:
        return country_layer(project_root, iso3)
    return gpd.GeoDataFrame({"country_code": [iso3]}, geometry=[box(*bbox)], crs="EPSG:4326")


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    config = load_config(args.config, country_code_override=args.country_code.upper())
    iso3 = str(config.get("study_area", {}).get("country_code", args.country_code)).upper()
    damage_cfg = yaml.safe_load(args.damage_config.read_text(encoding="utf-8"))["road_climate_damage"]
    analysis_period = damage_cfg.get("analysis_period", {})
    start_date = _analysis_date(analysis_period.get("start_date"), "2024-07-01")
    end_date = _analysis_date(analysis_period.get("end_date"), "2024-09-30")
    step_days = int(analysis_period.get("aggregation_period_days", 7))
    week_starts = _period_week_starts(start_date, end_date, step_days)
    period_label = (
        f"{analysis_period.get('start_date', 'na')}_to_{analysis_period.get('end_date', 'na')}_"
        f"{analysis_period.get('aggregation_period_days', 'na')}d"
    )

    raw_root = project_root / "data" / "raw"
    datasets_cfg = dict(config.get("datasets", {}))
    print(f"Loading country boundary for {iso3}...", flush=True)
    country = _country_or_bbox_geometry(project_root, iso3, config)
    if _overlay_bbox(config) is not None:
        print(f"Using overlay bbox: {list(country.total_bounds)}", flush=True)
    road_path = raw_root / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    total_features = len(pyogrio.read_dataframe(road_path, bbox=tuple(country.total_bounds), columns=[]))
    point_batch_size = max(1, int(args.point_batch_size))
    road_chunk_size = max(1, int(args.road_chunk_size))
    n_chunks = max(1, math.ceil(total_features / road_chunk_size))
    if args.max_road_chunks is not None:
        n_chunks = min(n_chunks, max(0, int(args.max_road_chunks)))
    print(f"Road layer: {road_path.name} | Features: {total_features:,}", flush=True)
    print(f"Road chunk size: {road_chunk_size:,} | Road chunks: {n_chunks:,}", flush=True)
    if args.road_geometry_mode == "probe_point":
        print("Using probe-point mode; crosswind-aligned road orientation metrics will be skipped.", flush=True)
    source_cell_m = {
        "era5": float(args.era5_cell_m),
        "chirps": float(args.chirps_cell_m),
        "flood": float(args.flood_cell_m),
        "flood_depth": float(args.flood_cell_m),
        "visibility": float(args.visibility_cell_m),
    }
    if args.multiscale_road_merge:
        print(
            "[overlay] multiscale road merge enabled "
            + " ".join(f"{key}={value:g}m" for key, value in source_cell_m.items() if key != "flood_depth"),
            flush=True,
        )

    out_dir = (
        args.output_root
        if args.output_root is not None
        else project_root / "outputs" / "road_multisource_overlay" / iso3 / period_label
    )
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir
    static_dir = out_dir / "static"
    weekly_dir = out_dir / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    if static_dir.exists():
        shutil.rmtree(static_dir)
    if weekly_dir.exists():
        shutil.rmtree(weekly_dir)
    legacy_static = out_dir / "roads_static.parquet"
    if legacy_static.exists():
        legacy_static.unlink()
    static_dir.mkdir(parents=True, exist_ok=True)
    weekly_dir.mkdir(parents=True, exist_ok=True)

    chirps_cfg = dict(datasets_cfg.get("chirps", {}))
    chirps_enabled = chirps_cfg.get("enabled", True)
    if chirps_enabled:
        chirps_frequency = str(chirps_cfg.get("frequency", chirps_cfg.get("temporal_resolution", "daily"))).lower()
        if chirps_frequency != "daily":
            raise RuntimeError(f"Weekly overlay requires daily CHIRPS inputs, got `{chirps_frequency}`.")
    chirps_version = str(chirps_cfg.get("version", "v3.0"))
    daily_variant = str(chirps_cfg.get("daily_variant", "sat")).strip().lower()
    flood_enabled = datasets_cfg.get("flood", {}).get("enabled", True)
    flood_depth_enabled = datasets_cfg.get("flood_depth", {}).get("enabled", True)
    flood_by_week = _flood_paths_by_week_start(project_root, raw_root, week_starts, bbox=tuple(country.total_bounds)) if flood_enabled else {}
    flood_depth_by_week = _flood_depth_paths_by_week_start(raw_root, iso3, week_starts) if flood_depth_enabled else {}
    vis_enabled = datasets_cfg.get("visibility_noaa_isd", {}).get("enabled", True)

    era5_cfg = dict(datasets_cfg.get("era5", {}))
    era5_paths = _era5_paths_from_config(raw_root, era5_cfg, analysis_start=start_date, analysis_end=end_date)
    era5_enabled = datasets_cfg.get("era5", {}).get("enabled", True)
    if era5_enabled:
        missing = [str(path) for path in era5_paths if not path.exists()]
        if missing:
            raise FileNotFoundError(f"Missing ERA5 weekly-capable source file(s): {', '.join(missing)}")

    cams_target = str(datasets_cfg.get("cams", {}).get("target_filename", "")).strip()
    cams_zip = raw_root / "cams" / cams_target if cams_target else None
    cams_enabled = datasets_cfg.get("cams", {}).get("enabled", True)
    if cams_enabled and (cams_zip is None or not cams_zip.exists()):
        raise FileNotFoundError(f"Missing CAMS weekly-capable source file: {cams_zip}")

    layer_columns_static_set: set[str] = set()
    layer_columns_weekly_set: set[str] = set()
    static_stats_rows: list[dict[str, object]] = []
    weekly_nan_rows: list[dict[str, object]] = []
    multiscale_sampling_rows: list[dict[str, object]] = []
    next_road_row_id = 0
    era5_dataset_cache: dict[tuple[str, ...], xr.Dataset] = {}

    def _cached_era5_dataset(paths: list[Path]) -> xr.Dataset:
        key = tuple(str(path) for path in paths)
        ds = era5_dataset_cache.get(key)
        if ds is None:
            ds = _open_era5_dataset(paths)
            era5_dataset_cache[key] = ds
        return ds

    def _compute_static_chunk(
        roads_chunk: gpd.GeoDataFrame,
        probe_points: gpd.GeoSeries,
        *,
        chunk_label: str,
    ) -> tuple[gpd.GeoDataFrame, list[str]]:
        layer_columns_static: list[str] = []
        if datasets_cfg.get("landslide_susceptibility", {}).get("enabled", True):
            print(f"[overlay] {chunk_label} static landslide start", flush=True)
            landslide_cfg = dict(datasets_cfg.get("landslide_susceptibility", {}))
            landslide_slug = str(
                landslide_cfg.get("target_slug")
                or config.get("study_area", {}).get("slug")
                or iso3.lower()
            )
            landslide_path = raw_root / "landslide_susceptibility" / "global" / f"nasa_landslide_susceptibility_{landslide_slug}.tif"
            if not landslide_path.exists():
                landslide_path = raw_root / "landslide_susceptibility" / "global" / f"nasa_landslide_susceptibility_{iso3.lower()}.tif"
            if not landslide_path.exists():
                landslide_path = None
            roads_chunk["landslide_susceptibility"] = (
                _sample_raster_paths([landslide_path], probe_points, reducer="first_valid")
                if landslide_path
                else np.nan
            )
            layer_columns_static.append("landslide_susceptibility")

        if datasets_cfg.get("gem", {}).get("enabled", True):
            print(f"[overlay] {chunk_label} static gem start", flush=True)
            gem_path = raw_root / "gem" / "global" / "v2023_1_pga_475_rock_3min.tif"
            roads_chunk["gem_pga_475y"] = (
                _sample_raster_paths([gem_path], probe_points, reducer="first_valid")
                if gem_path.exists()
                else np.nan
            )
            layer_columns_static.append("gem_pga_475y")

        if datasets_cfg.get("liquefaction", {}).get("enabled", True):
            print(f"[overlay] {chunk_label} static liquefaction start", flush=True)
            liquefaction_path = raw_root / "liquefaction" / "global" / "liquefaction_v1_deg.tif"
            roads_chunk["liquefaction_class"] = (
                _sample_raster_paths([liquefaction_path], probe_points, reducer="first_valid")
                if liquefaction_path.exists()
                else np.nan
            )
            layer_columns_static.append("liquefaction_class")

        if datasets_cfg.get("worldcover", {}).get("enabled", True):
            print(f"[overlay] {chunk_label} static worldcover start", flush=True)
            worldcover_paths = sorted((raw_root / "worldcover").glob("**/*_Map.tif"))
            roads_chunk["worldcover_class"] = (
                _sample_raster_paths(worldcover_paths, probe_points, reducer="first_valid", positive_only=True)
                if worldcover_paths
                else np.nan
            )
            layer_columns_static.append("worldcover_class")

        if datasets_cfg.get("soilgrids", {}).get("enabled", True):
            for soil_path in sorted((raw_root / "soilgrids").glob("*.tif")):
                col = f"soil_{soil_path.stem}"
                print(f"[overlay] {chunk_label} static soilgrids {soil_path.name} start", flush=True)
                roads_chunk[col] = _sample_raster_paths([soil_path], probe_points, reducer="first_valid")
                layer_columns_static.append(col)

        if datasets_cfg.get("era5_spi", {}).get("enabled", True):
            for spi_path in sorted((raw_root / "era5_spi" / "global" / "monthly").glob("GLOBAL-ERA5_LAND_DAILY-spi-*.tif")):
                col = f"era5_spi_{spi_path.stem.split('-spi-')[-1]}"
                print(f"[overlay] {chunk_label} static era5_spi {spi_path.name} start", flush=True)
                roads_chunk[col] = _sample_raster_paths([spi_path], probe_points, reducer="first_valid")
                layer_columns_static.append(col)

        if datasets_cfg.get("flopros", {}).get("enabled", True):
            print(f"[overlay] {chunk_label} static flopros start", flush=True)
            roads_chunk, flopros_cols = _add_flopros(roads_chunk, probe_points, raw_root)
            layer_columns_static.extend(flopros_cols)

        return roads_chunk, layer_columns_static

    def _compute_week_chunk(
        roads_chunk: gpd.GeoDataFrame,
        probe_points: gpd.GeoSeries,
        lons: np.ndarray,
        lats: np.ndarray,
        road_ux: np.ndarray,
        road_uy: np.ndarray,
        merge_groups: dict[str, tuple[np.ndarray, np.ndarray] | None],
        *,
        week_start: date,
        chunk_label: str,
    ) -> pd.DataFrame:
        week_t0 = time.time()
        token = _week_token(week_start)
        week_end = _week_end(week_start, end_date, step_days)
        verbose_week_logs = not args.compact_weekly_logs
        if verbose_week_logs:
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} start", flush=True)
        week_df = pd.DataFrame({"road_row_id": roads_chunk["road_row_id"].values})

        if chirps_enabled:
            week_paths = _chirps_daily_paths_for_week(raw_root, version=chirps_version, daily_variant=daily_variant, week_start=week_start, week_end=week_end)
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} chirps start paths={len(week_paths)}", flush=True)
            col = f"chirps_week_{token}_mm"
            max_24h_col = f"chirps_24h_max_week_{token}_mm"
            groups = merge_groups.get("chirps")
            reps = groups[0] if groups is not None else np.arange(len(probe_points))
            points = probe_points.iloc[reps] if groups is not None else probe_points
            week_df[col] = _expand_multiscale_values(_sample_raster_paths(week_paths, points, reducer="sum"), groups, len(probe_points))
            week_df[max_24h_col] = _expand_multiscale_values(_sample_raster_paths(week_paths, points, reducer="max"), groups, len(probe_points))
            if groups is not None:
                multiscale_sampling_rows.append({"chunk": chunk_label, "week_start": week_start.isoformat(), "source": "chirps", "cell_m": source_cell_m["chirps"], "n_roads": len(probe_points), "n_representatives": len(reps)})

        if flood_enabled:
            flood_paths = flood_by_week.get(week_start, [])
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} flood start rasters={len(flood_paths)}", flush=True)
            flood_col = f"flood_week_{token}"
            groups = merge_groups.get("flood")
            reps = groups[0] if groups is not None else np.arange(len(probe_points))
            points = probe_points.iloc[reps] if groups is not None else probe_points
            week_df[flood_col] = _expand_multiscale_values(_sample_raster_paths(flood_paths, points, reducer="max", positive_only=True), groups, len(probe_points))
            # Keep NaN where flood data coverage is absent. Downstream threshold logic
            # treats NaN as "no evidence" rather than "confirmed no flood".
            flood_data_col = f"meta_flood_week_{token}_has_data"
            week_df[flood_data_col] = pd.to_numeric(week_df[flood_col], errors="coerce").notna().astype(int)
            if groups is not None:
                multiscale_sampling_rows.append({"chunk": chunk_label, "week_start": week_start.isoformat(), "source": "flood", "cell_m": source_cell_m["flood"], "n_roads": len(probe_points), "n_representatives": len(reps)})

        if flood_depth_enabled:
            flood_depth_paths = flood_depth_by_week.get(week_start, [])
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} flood_depth start rasters={len(flood_depth_paths)}", flush=True)
            flood_depth_col = f"flood_depth_week_{token}_max_m"
            groups = merge_groups.get("flood_depth")
            reps = groups[0] if groups is not None else np.arange(len(probe_points))
            points = probe_points.iloc[reps] if groups is not None else probe_points
            week_df[flood_depth_col] = _expand_multiscale_values(_sample_raster_paths(flood_depth_paths, points, reducer="max", positive_only=True), groups, len(probe_points))
            if groups is not None:
                multiscale_sampling_rows.append({"chunk": chunk_label, "week_start": week_start.isoformat(), "source": "flood_depth", "cell_m": source_cell_m["flood_depth"], "n_roads": len(probe_points), "n_representatives": len(reps)})

        if vis_enabled:
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} visibility start", flush=True)
            vis_col = f"visibility_week_{token}_min_m"
            groups = merge_groups.get("visibility")
            reps = groups[0] if groups is not None else np.arange(len(probe_points))
            points = probe_points.iloc[reps] if groups is not None else probe_points
            week_df[vis_col] = _expand_multiscale_values(_sample_noaa_visibility_weekly_min_m(raw_root, iso3, points, week_start=week_start, week_end=week_end), groups, len(probe_points))
            if groups is not None:
                multiscale_sampling_rows.append({"chunk": chunk_label, "week_start": week_start.isoformat(), "source": "visibility", "cell_m": source_cell_m["visibility"], "n_roads": len(probe_points), "n_representatives": len(reps)})

        if era5_enabled:
            week_era5_paths = _era5_paths_for_window(era5_paths, week_start, week_end)
            if verbose_week_logs:
                print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 open files={len(week_era5_paths)}", flush=True)
            ds = _cached_era5_dataset(week_era5_paths)
            try:
                groups = merge_groups.get("era5")
                reps = groups[0] if groups is not None else np.arange(len(lons))
                sample_lons = lons[reps] if groups is not None else lons
                sample_lats = lats[reps] if groups is not None else lats
                sample_ux = road_ux[reps] if groups is not None else road_ux
                sample_uy = road_uy[reps] if groups is not None else road_uy
                if groups is not None:
                    multiscale_sampling_rows.append({"chunk": chunk_label, "week_start": week_start.isoformat(), "source": "era5", "cell_m": source_cell_m["era5"], "n_roads": len(lons), "n_representatives": len(reps)})
                weekly_specs = {"t2m": ("mean", "max"), "skt": ("mean", "max"), "tp": ("sum",), "swvl1": ("mean",), "u10": ("mean",), "v10": ("mean",)}
                if args.era5_precip_only:
                    weekly_specs = {"tp": ("sum",)}
                for var, reducers in weekly_specs.items():
                    if var not in ds:
                        continue
                    for reducer in reducers:
                        t0 = time.time()
                        col = f"era5_{var}_week_{token}_{reducer}"
                        if verbose_week_logs:
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 {var}/{reducer} start", flush=True)
                        sampled = _sample_netcdf_var_chunked(ds[var], sample_lons, sample_lats, start_date=week_start, end_date=week_end, reducer=reducer, batch_size=point_batch_size)
                        week_df[col] = _expand_multiscale_values(sampled, groups, len(lons))
                        if verbose_week_logs:
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 {var}/{reducer} done elapsed_s={time.time() - t0:.1f}", flush=True)
                        if var == "skt" and reducer == "max":
                            pavement_col = f"pavement_surface_temperature_week_{token}_max_c"
                            week_df[pavement_col] = pd.to_numeric(week_df[col], errors="coerce") - 273.15
                        if var == "swvl1" and reducer == "mean":
                            percentile_col = f"soil_moisture_week_{token}_local_percentile"
                            week_df[percentile_col] = _local_percentile(week_df[col])

                if not args.era5_precip_only:
                    for reducer in ("mean", "max"):
                        t0 = time.time()
                        col = f"era5_wind_speed_week_{token}_{reducer}"
                        if verbose_week_logs:
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 wind_speed/{reducer} start", flush=True)
                        sampled = _sample_netcdf_wind_speed(ds, sample_lons, sample_lats, start_date=week_start, end_date=week_end, reducer=reducer)
                        week_df[col] = _expand_multiscale_values(sampled, groups, len(lons))
                        if verbose_week_logs:
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 wind_speed/{reducer} done elapsed_s={time.time() - t0:.1f}", flush=True)
                if verbose_week_logs:
                    print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 tp_1h_max start", flush=True)
                tp_1h_col = f"era5_tp_1h_max_week_{token}_mm_per_h"
                sampled = _sample_era5_tp_1h_max_mm_per_h(ds, sample_lons, sample_lats, start_date=week_start, end_date=week_end)
                week_df[tp_1h_col] = _expand_multiscale_values(sampled, groups, len(lons))
                erosion_pct_col = f"unpaved_erosion_rainfall_week_{token}_local_percentile"
                week_df[erosion_pct_col] = _local_percentile(week_df[tp_1h_col])
                if not args.skip_era5_daily_sum_max:
                    if verbose_week_logs:
                        print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 tp_daily_sum_max start", flush=True)
                    tp_daily_sum_max_col = f"era5_tp_daily_sum_max_week_{token}_mm"
                    sampled = _sample_era5_tp_daily_sum_weekly_max_mm(ds, sample_lons, sample_lats, start_date=week_start, end_date=week_end)
                    week_df[tp_daily_sum_max_col] = _expand_multiscale_values(sampled, groups, len(lons))
                if (not args.era5_precip_only) and args.road_geometry_mode != "probe_point":
                    if verbose_week_logs:
                        print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 crosswind start", flush=True)
                    crosswind_col = f"era5_crosswind_10m_week_{token}_max"
                    sampled = _sample_netcdf_crosswind_speed(ds, sample_lons, sample_lats, sample_ux, sample_uy, start_date=week_start, end_date=week_end, reducer="max")
                    week_df[crosswind_col] = _expand_multiscale_values(sampled, groups, len(lons))
                if not args.era5_precip_only:
                    if verbose_week_logs:
                        print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 gust start", flush=True)
                    gust_col = f"era5_wind_gust_week_{token}_max"
                    sampled = _sample_era5_gust_speed(ds, sample_lons, sample_lats, start_date=week_start, end_date=week_end)
                    week_df[gust_col] = _expand_multiscale_values(sampled, groups, len(lons))
                    if verbose_week_logs:
                        print(f"[overlay] {chunk_label} week={week_start.isoformat()} era5 precip_rate start", flush=True)
                    rate_col = f"era5_max_total_precip_rate_week_{token}_mm_per_h"
                    sampled = _sample_era5_rate_mm_per_h(ds, ("mxtpr", "mtpr", "tprate"), sample_lons, sample_lats, start_date=week_start, end_date=week_end)
                    week_df[rate_col] = _expand_multiscale_values(sampled, groups, len(lons))
            finally:
                pass

        if cams_enabled:
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} cams start", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"cams-overlay-{token}-") as tmpdir:
                cams_path = cams_zip
                if zipfile.is_zipfile(cams_zip):
                    with zipfile.ZipFile(cams_zip) as archive:
                        archive.extractall(tmpdir)
                    cams_path = Path(tmpdir) / "data_allhours_sfc.nc"
                    if not cams_path.exists():
                        raise FileNotFoundError(f"CAMS archive is missing data_allhours_sfc.nc: {cams_zip}")
                ds_cams = xr.open_dataset(cams_path)
                try:
                    for var in ["pm2p5", "pm10", "duaod550"]:
                        if var not in ds_cams:
                            continue
                        for reducer in ("mean", "max"):
                            t0 = time.time()
                            col = f"cams_{var}_week_{token}_{reducer}"
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} cams {var}/{reducer} start", flush=True)
                            week_df[col] = _sample_netcdf_var_chunked(ds_cams[var], lons, lats, start_date=week_start, end_date=week_end, reducer=reducer, batch_size=point_batch_size)
                            print(f"[overlay] {chunk_label} week={week_start.isoformat()} cams {var}/{reducer} done elapsed_s={time.time() - t0:.1f}", flush=True)
                finally:
                    ds_cams.close()

        if verbose_week_logs:
            print(f"[overlay] {chunk_label} week={week_start.isoformat()} done elapsed_s={time.time() - week_t0:.1f}", flush=True)
        return week_df

    print("Starting chunked overlay...", flush=True)
    chunk_bar = tqdm(range(n_chunks), desc="Road chunks", unit="chunk")
    for chunk_idx in chunk_bar:
        chunk_t0 = time.time()
        skip = chunk_idx * road_chunk_size
        chunk_label = f"chunk {chunk_idx + 1}/{n_chunks}"
        print(f"[overlay] {chunk_label} read start skip={skip:,} max={road_chunk_size:,}", flush=True)
        roads = load_roads(
            project_root,
            iso3,
            country,
            geometry_mode=args.road_geometry_mode,
            skip_features=skip,
            max_features=road_chunk_size,
            road_row_offset=next_road_row_id,
            road_backend=args.road_backend,
            postgis_dsn=args.postgis_dsn,
            postgis_table=args.postgis_table,
        )
        if roads.empty:
            print(f"[overlay] {chunk_label} no roads loaded", flush=True)
            continue
        next_road_row_id += len(roads)
        print(f"[overlay] {chunk_label} loaded roads={len(roads):,} road_row_id_max={next_road_row_id - 1:,}", flush=True)
        if args.road_geometry_mode == "probe_point":
            roads["probe_point"] = roads.geometry
        else:
            roads["probe_point"] = roads.geometry.apply(geometry_probe_point)
        probe_points = gpd.GeoSeries(roads["probe_point"], crs="EPSG:4326")
        lons = np.asarray([pt.x if pt and not pt.is_empty else np.nan for pt in probe_points], dtype="float64")
        lats = np.asarray([pt.y if pt and not pt.is_empty else np.nan for pt in probe_points], dtype="float64")
        if args.road_geometry_mode == "probe_point":
            road_ux = np.zeros(len(roads), dtype="float64")
            road_uy = np.ones(len(roads), dtype="float64")
        else:
            road_ux, road_uy = _road_unit_vectors(roads)
        merge_groups: dict[str, tuple[np.ndarray, np.ndarray] | None] = {}
        if args.multiscale_road_merge:
            for source, cell_m in source_cell_m.items():
                merge_groups[source] = _multiscale_group_index(roads, probe_points, cell_m=cell_m)

        roads, layer_columns_static = _compute_static_chunk(roads, probe_points, chunk_label=chunk_label)
        layer_columns_static_set.update(layer_columns_static)
        static_cols = ["road_row_id", "surface_group", "geometry", *layer_columns_static]
        roads_static = roads[static_cols].copy()
        for row in _layer_stats(roads_static, layer_columns_static):
            row["chunk_idx"] = chunk_idx
            static_stats_rows.append(row)
        static_part = static_dir / f"part_{chunk_idx:05d}.parquet"
        print(f"[overlay] {chunk_label} write static {static_part.name} rows={len(roads_static):,}", flush=True)
        roads_static.to_parquet(static_part, index=False)

        week_iter = tqdm(
            week_starts,
            desc=f"{iso3} {chunk_label} weeks" if args.compact_weekly_logs else f"{chunk_label} weeks",
            unit="week",
            leave=False,
        )
        for week_idx, week_start in enumerate(week_iter, start=1):
            token = _week_token(week_start)
            week_df = _compute_week_chunk(roads, probe_points, lons, lats, road_ux, road_uy, merge_groups, week_start=week_start, chunk_label=chunk_label)
            week_cols = [col for col in week_df.columns if col != "road_row_id"]
            layer_columns_weekly_set.update(week_cols)
            for col in week_cols:
                numeric = pd.to_numeric(week_df[col], errors="coerce")
                n_total = int(len(numeric))
                n_nan = int(numeric.isna().sum())
                weekly_nan_rows.append({"chunk_idx": chunk_idx, "week_start": week_start.isoformat(), "week_token": token, "column": col, "n_total": n_total, "n_nan": n_nan, "nan_share": float(n_nan / n_total) if n_total else float("nan")})
            week_out_dir = weekly_dir / f"week_{token}"
            week_out_dir.mkdir(parents=True, exist_ok=True)
            weekly_part = week_out_dir / f"part_{chunk_idx:05d}.parquet"
            if not args.compact_weekly_logs:
                print(f"[overlay] {chunk_label} week={week_start.isoformat()} write {weekly_part.name} rows={len(week_df):,}", flush=True)
            week_df.to_parquet(weekly_part, index=False)
            del week_df
            gc.collect()

        del roads, roads_static, probe_points, lons, lats, road_ux, road_uy
        gc.collect()
        for cached in era5_dataset_cache.values():
            cached.close()
        era5_dataset_cache.clear()
        print(f"[overlay] {chunk_label} done elapsed_s={time.time() - chunk_t0:.1f}", flush=True)

    layer_columns_static = sorted(layer_columns_static_set)
    layer_columns_weekly = sorted(layer_columns_weekly_set)
    pd.DataFrame(static_stats_rows).to_csv(out_dir / "layer_summary_static.csv", index=False)
    weekly_nan_df = pd.DataFrame(weekly_nan_rows)
    weekly_nan_df.to_csv(out_dir / "weekly_nan_diagnostics.csv", index=False)
    multiscale_sampling_df = pd.DataFrame(multiscale_sampling_rows)
    multiscale_sampling_df.to_csv(out_dir / "multiscale_sampling_diagnostics.csv", index=False)
    columns_all_nan = 0
    max_nan_share = 0.0
    if not weekly_nan_df.empty:
        columns_all_nan = int((weekly_nan_df["nan_share"] >= 1.0).sum())
        max_nan_share = float(weekly_nan_df["nan_share"].max())

    report = {
        "country_code": iso3,
        "road_geometry_mode": args.road_geometry_mode,
        "analysis_period": analysis_period,
        "n_roads": int(next_road_row_id),
        "road_chunk_size": road_chunk_size,
        "n_road_chunks": n_chunks,
        "layer_count_static": len(layer_columns_static),
        "layer_count_weekly": len(layer_columns_weekly),
        "layers_static": layer_columns_static,
        "nan_diagnostics": {"weekly_columns_all_nan": columns_all_nan, "weekly_max_nan_share": max_nan_share},
        "multiscale_road_merge": {
            "enabled": bool(args.multiscale_road_merge),
            "source_cell_m": source_cell_m,
            "diagnostics_rows": int(len(multiscale_sampling_df)),
            "min_representatives": int(multiscale_sampling_df["n_representatives"].min()) if not multiscale_sampling_df.empty else None,
            "max_representatives": int(multiscale_sampling_df["n_representatives"].max()) if not multiscale_sampling_df.empty else None,
        },
        "outputs": {
            "static_dir": _relpath(static_dir, project_root),
            "weekly_dir": _relpath(weekly_dir, project_root),
            "layer_summary_static_csv": _relpath(out_dir / "layer_summary_static.csv", project_root),
            "weekly_nan_diagnostics_csv": _relpath(out_dir / "weekly_nan_diagnostics.csv", project_root),
            "multiscale_sampling_diagnostics_csv": _relpath(out_dir / "multiscale_sampling_diagnostics.csv", project_root),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
