"""Run a first-pass road overlay against all currently downloaded hazard/climate layers."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import xarray as xr
import yaml
from src.data.config import load_config
from src.data.run_flood_depth_experiment import _country_layer, _geometry_probe_point, _load_roads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Overlay roads with all downloaded sources for one country.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"))
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--damage-config", type=Path, default=Path("config/road_climate_damage.yaml"))
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sample_raster_paths(
    paths: list[Path],
    probe_points_wgs84: gpd.GeoSeries,
    *,
    reducer: str = "max",
    positive_only: bool = False,
) -> np.ndarray:
    if not paths:
        return np.full(len(probe_points_wgs84), np.nan, dtype="float64")

    samples: list[np.ndarray] = []
    for path in paths:
        with rasterio.open(path) as src:
            if src.crs:
                points_src = probe_points_wgs84.to_crs(src.crs)
            else:
                points_src = probe_points_wgs84
            coords = [(geom.x, geom.y) for geom in points_src]
            vals = np.asarray([row[0] if len(row) else np.nan for row in src.sample(coords)], dtype="float64")
            nodata = src.nodata
            if nodata is not None:
                vals[vals == nodata] = np.nan
            vals[~np.isfinite(vals)] = np.nan
            if positive_only:
                vals[vals <= 0] = np.nan
            samples.append(vals)

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
    with np.errstate(invalid="ignore"):
        return np.nanmax(stack, axis=0)


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
) -> np.ndarray:
    arr = da
    time_dim = _netcdf_time_dim(arr)
    if time_dim is not None:
        time_index = pd.to_datetime(arr[time_dim].values)
        if start_date is not None and end_date is not None:
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date) + pd.Timedelta(days=1)
            mask = (time_index >= start_ts) & (time_index < end_ts)
            if not mask.any():
                return np.full((0, lons.shape[0]), np.nan, dtype="float64")
            arr = arr.sel({time_dim: arr[time_dim].values[mask]})

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
        with np.errstate(invalid="ignore"):
            return np.nanmax(values, axis=0)
    if reducer == "min":
        with np.errstate(invalid="ignore"):
            return np.nanmin(values, axis=0)
    with np.errstate(invalid="ignore"):
        return np.nanmean(values, axis=0)


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


def _open_era5_dataset(paths: list[Path]) -> xr.Dataset:
    if not paths:
        raise FileNotFoundError("No ERA5 files were provided for overlay.")
    datasets = [xr.open_dataset(path) for path in paths]
    if len(datasets) == 1:
        return datasets[0]
    try:
        time_dim = next((name for name in ["valid_time", "time"] if name in datasets[0].dims), None)
        if time_dim is None:
            return xr.combine_by_coords(datasets)
        combined = xr.concat(datasets, dim=time_dim)
        return combined.sortby(time_dim)
    except Exception:
        for ds in datasets:
            ds.close()
        raise


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
    if split_request_by == "weekly":
        request = dict(era5_cfg.get("request") or {})
        if not request:
            raise ValueError("ERA5 weekly overlay requires datasets.era5.request in the config.")
        target_prefix = str(era5_cfg.get("target_prefix", "era5")).strip() or "era5"
        step_days = int(era5_cfg.get("request_step_days", era5_cfg.get("step_days", 7)))
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
    return [era5_root / era5_target] if era5_target else []


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
) -> dict[date, list[Path]]:
    flood_paths = sorted((raw_root / "flood" / "copernicus_gfm" / "GFM").glob("2024/*/*.tif"))
    by_week: dict[date, list[Path]] = {week_start: [] for week_start in week_starts}
    if not flood_paths:
        return by_week

    catalog_path = project_root / "data" / "metadata" / "catalog.csv"
    if not catalog_path.exists():
        raise FileNotFoundError(f"Flood rasters exist but weekly catalog mapping is missing: {catalog_path}")

    catalog = pd.read_csv(catalog_path)
    required = {"dataset_name", "local_path", "notes"}
    if not required.issubset(catalog.columns):
        raise ValueError(f"Flood catalog must contain columns {sorted(required)}: {catalog_path}")

    flood_path_set = set(flood_paths)
    flood_rows = catalog.loc[catalog["dataset_name"].astype("string") == "flood"].copy()
    for row in flood_rows.itertuples(index=False):
        local_path = project_root / str(row.local_path)
        if local_path not in flood_path_set:
            continue
        match = re.search(r"weekly window (\d{4}-\d{2}-\d{2})\.\.", str(row.notes))
        if match is None:
            continue
        week_start = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        if week_start in by_week:
            by_week[week_start].append(local_path)

    return by_week


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
    country = _country_layer(project_root, iso3)
    roads = _load_roads(project_root, iso3, country)
    roads["probe_point"] = roads.geometry.apply(_geometry_probe_point)
    probe_points = gpd.GeoSeries(roads["probe_point"], crs="EPSG:4326")
    lons = np.asarray([pt.x for pt in probe_points], dtype="float64")
    lats = np.asarray([pt.y for pt in probe_points], dtype="float64")

    layer_columns: list[str] = []

    chirps_cfg = dict(datasets_cfg.get("chirps", {}))
    if chirps_cfg.get("enabled", True):
        chirps_frequency = str(chirps_cfg.get("frequency", chirps_cfg.get("temporal_resolution", "daily"))).lower()
        if chirps_frequency != "daily":
            raise RuntimeError(f"Weekly overlay requires daily CHIRPS inputs, got `{chirps_frequency}`.")
        chirps_version = str(chirps_cfg.get("version", "v3.0"))
        daily_variant = str(chirps_cfg.get("daily_variant", "sat")).strip().lower()
        for week_start in week_starts:
            week_end = _week_end(week_start, end_date, step_days)
            col = f"chirps_week_{_week_token(week_start)}_mm"
            week_paths = _chirps_daily_paths_for_week(
                raw_root,
                version=chirps_version,
                daily_variant=daily_variant,
                week_start=week_start,
                week_end=week_end,
            )
            roads[col] = _sample_raster_paths(week_paths, probe_points, reducer="sum")
            layer_columns.append(col)

    flood_by_week = _flood_paths_by_week_start(project_root, raw_root, week_starts)
    for week_start in week_starts:
        col = f"flood_week_{week_start.isoformat().replace('-', '_')}"
        roads[col] = _sample_raster_paths(flood_by_week.get(week_start, []), probe_points, reducer="max", positive_only=True)
        roads[col] = roads[col].fillna(0.0)
        layer_columns.append(col)

    if datasets_cfg.get("landslide_susceptibility", {}).get("enabled", True):
        landslide_path = raw_root / "landslide_susceptibility" / "global" / f"nasa_landslide_susceptibility_{iso3.lower()}.tif"
        if not landslide_path.exists():
            fallback = sorted((raw_root / "landslide_susceptibility" / "global").glob("*.tif"))
            landslide_path = fallback[0] if fallback else None
        roads["landslide_susceptibility"] = (
            _sample_raster_paths([landslide_path], probe_points, reducer="first_valid") if landslide_path else np.nan
        )
        layer_columns.append("landslide_susceptibility")

    if datasets_cfg.get("gem", {}).get("enabled", True):
        gem_path = raw_root / "gem" / "global" / "v2023_1_pga_475_rock_3min.tif"
        roads["gem_pga_475y"] = _sample_raster_paths([gem_path], probe_points, reducer="first_valid") if gem_path.exists() else np.nan
        layer_columns.append("gem_pga_475y")

    if datasets_cfg.get("liquefaction", {}).get("enabled", True):
        liquefaction_path = raw_root / "liquefaction" / "global" / "liquefaction_v1_deg.tif"
        roads["liquefaction_class"] = (
            _sample_raster_paths([liquefaction_path], probe_points, reducer="first_valid") if liquefaction_path.exists() else np.nan
        )
        layer_columns.append("liquefaction_class")

    if datasets_cfg.get("worldcover", {}).get("enabled", True):
        worldcover_paths = sorted((raw_root / "worldcover").glob("**/*_Map.tif"))
        roads["worldcover_class"] = (
            _sample_raster_paths(worldcover_paths, probe_points, reducer="first_valid", positive_only=True) if worldcover_paths else np.nan
        )
        layer_columns.append("worldcover_class")

    if datasets_cfg.get("soilgrids", {}).get("enabled", True):
        for soil_path in sorted((raw_root / "soilgrids").glob("*.tif")):
            col = f"soil_{soil_path.stem}"
            roads[col] = _sample_raster_paths([soil_path], probe_points, reducer="first_valid")
            layer_columns.append(col)

    if datasets_cfg.get("era5_spi", {}).get("enabled", True):
        for spi_path in sorted((raw_root / "era5_spi" / "global" / "monthly").glob("GLOBAL-ERA5_LAND_DAILY-spi-*.tif")):
            col = f"era5_spi_{spi_path.stem.split('-spi-')[-1]}"
            roads[col] = _sample_raster_paths([spi_path], probe_points, reducer="first_valid")
            layer_columns.append(col)

    era5_cfg = dict(datasets_cfg.get("era5", {}))
    era5_paths = _era5_paths_from_config(raw_root, era5_cfg, analysis_start=start_date, analysis_end=end_date)
    if datasets_cfg.get("era5", {}).get("enabled", True):
        missing = [str(path) for path in era5_paths if not path.exists()]
        if missing:
            raise FileNotFoundError(f"Missing ERA5 weekly-capable source file(s): {', '.join(missing)}")
        ds = _open_era5_dataset(era5_paths)
        try:
            weekly_specs = {
                "t2m": ("mean", "max"),
                "skt": ("mean", "max"),
                "tp": ("sum",),
                "swvl1": ("mean",),
                "u10": ("mean",),
                "v10": ("mean",),
            }
            for week_start in week_starts:
                token = _week_token(week_start)
                week_end = _week_end(week_start, end_date, step_days)
                for var, reducers in weekly_specs.items():
                    if var not in ds:
                        continue
                    for reducer in reducers:
                        col = f"era5_{var}_week_{token}_{reducer}"
                        roads[col] = _sample_netcdf_var(
                            ds[var],
                            lons,
                            lats,
                            start_date=week_start,
                            end_date=week_end,
                            reducer=reducer,
                        )
                        layer_columns.append(col)
                for reducer in ("mean", "max"):
                    col = f"era5_wind_speed_week_{token}_{reducer}"
                    roads[col] = _sample_netcdf_wind_speed(
                        ds,
                        lons,
                        lats,
                        start_date=week_start,
                        end_date=week_end,
                        reducer=reducer,
                    )
                    layer_columns.append(col)
        finally:
            ds.close()

    cams_target = str(datasets_cfg.get("cams", {}).get("target_filename", "")).strip()
    cams_zip = raw_root / "cams" / cams_target if cams_target else None
    if datasets_cfg.get("cams", {}).get("enabled", True):
        if cams_zip is None or not cams_zip.exists():
            raise FileNotFoundError(f"Missing CAMS weekly-capable source file: {cams_zip}")
        with tempfile.TemporaryDirectory(prefix="cams-overlay-") as tmpdir:
            cams_path = cams_zip
            if zipfile.is_zipfile(cams_zip):
                with zipfile.ZipFile(cams_zip) as archive:
                    archive.extractall(tmpdir)
                cams_path = Path(tmpdir) / "data_allhours_sfc.nc"
                if not cams_path.exists():
                    raise FileNotFoundError(f"CAMS archive is missing data_allhours_sfc.nc: {cams_zip}")
            ds = xr.open_dataset(cams_path)
            try:
                for week_start in week_starts:
                    token = _week_token(week_start)
                    week_end = _week_end(week_start, end_date, step_days)
                    for var in ["pm2p5", "pm10", "duaod550"]:
                        if var not in ds:
                            continue
                        for reducer in ("mean", "max"):
                            col = f"cams_{var}_week_{token}_{reducer}"
                            roads[col] = _sample_netcdf_var(
                                ds[var],
                                lons,
                                lats,
                                start_date=week_start,
                                end_date=week_end,
                                reducer=reducer,
                            )
                            layer_columns.append(col)
            finally:
                ds.close()

    if datasets_cfg.get("flopros", {}).get("enabled", True):
        roads, flopros_cols = _add_flopros(roads, probe_points, raw_root)
        layer_columns.extend(flopros_cols)

    roads = roads.drop(columns=["probe_point"])
    stats = _layer_stats(roads, layer_columns)

    surface_stats = []
    flood_week_cols = [col for col in layer_columns if col.startswith("flood_week_")]
    for group, subset in roads.groupby("surface_group"):
        row = {"surface_group": group, "n_roads": int(len(subset))}
        if flood_week_cols:
            flood_any = subset[flood_week_cols].apply(pd.to_numeric, errors="coerce").gt(0).any(axis=1)
            row["n_flood_positive_any_week"] = int(flood_any.sum())
        surface_stats.append(row)

    out_dir = project_root / "outputs" / "road_multisource_overlay" / iso3 / period_label
    out_dir.mkdir(parents=True, exist_ok=True)
    roads.to_file(out_dir / "roads_with_multisource_overlay.gpkg", driver="GPKG")
    pd.DataFrame(stats).to_csv(out_dir / "layer_summary.csv", index=False)
    pd.DataFrame(surface_stats).to_csv(out_dir / "surface_summary.csv", index=False)

    report = {
        "country_code": iso3,
        "analysis_period": analysis_period,
        "n_roads": int(len(roads)),
        "layer_count": len(layer_columns),
        "layers": layer_columns,
        "outputs": {
            "roads_gpkg": str((out_dir / "roads_with_multisource_overlay.gpkg").relative_to(project_root)),
            "layer_summary_csv": str((out_dir / "layer_summary.csv").relative_to(project_root)),
            "surface_summary_csv": str((out_dir / "surface_summary.csv").relative_to(project_root)),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
