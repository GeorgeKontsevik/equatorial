#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import math
import re
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import psycopg
import pycountry
import rasterio
import sqlalchemy as sa
import xarray as xr
import yaml
from rasterio.mask import mask


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CFG_DIR = ROOT / "config/generated/full_year_2024_era5_tp_remaining_20260517_203158"
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SA_URL = "postgresql+psycopg://gk@127.0.0.1:5432/equatorial"
START = date(2024, 1, 1)
END = date(2024, 12, 31)
STEP_DAYS = 7
CITY_POP_MIN = 50_000
TOP_N_CROP = 100
TOP_N_CONNECTED = 3
NON_TRUCK_HIGHWAYS = ("footway", "path", "steps", "pedestrian", "cycleway", "bridleway", "living_street")
ERA5_COORD_DECIMALS = 4


def log(msg: str) -> None:
    print(msg, flush=True)


def qident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"'


def qiso_literal(iso: str) -> str:
    if not re.fullmatch(r"[A-Z]{3}", iso):
        raise ValueError(f"Unsafe ISO code: {iso}")
    return f"'{iso}'"


def table_exists(conn: psycopg.Connection, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        return cur.fetchone()[0] is not None


def scalar(conn: psycopg.Connection, sql: str, params: tuple = ()) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0]
    return int(value or 0)


def week_starts() -> list[date]:
    weeks: list[date] = []
    cur = START
    while cur <= END:
        weeks.append(cur)
        cur += timedelta(days=STEP_DAYS)
    return weeks


def week_end(start: date) -> date:
    return min(start + timedelta(days=STEP_DAYS - 1), END)


def months_between(start_ts: pd.Timestamp, end_ts: pd.Timestamp):
    cur = pd.Timestamp(start_ts.year, start_ts.month, 1)
    last = pd.Timestamp(end_ts.year, end_ts.month, 1)
    while cur <= last:
        yield (cur.year, cur.month)
        cur = cur + pd.DateOffset(months=1)


def hourly_increment_mm(values_m: np.ndarray) -> np.ndarray:
    values = np.asarray(values_m, dtype="float64") * 1000.0
    if values.shape[0] == 0:
        return values
    increments = np.empty_like(values)
    increments[0, :, :] = values[0, :, :]
    diff = np.diff(values, axis=0)
    reset_mask = diff < -0.01
    increments[1:, :, :] = np.where(reset_mask, values[1:, :, :], np.maximum(diff, 0.0))
    increments[~np.isfinite(increments)] = np.nan
    increments[increments < 0.0] = 0.0
    return increments


def fmt(v: float) -> str:
    if v is None or not math.isfinite(float(v)):
        return ""
    return f"{float(v):.6f}"


def fmt_cell_coord(v: float) -> str:
    return f"{float(v):.{ERA5_COORD_DECIMALS}f}"


def era5_cell_id(lon: float, lat: float) -> str:
    return f"{fmt_cell_coord(lon)}:{fmt_cell_coord(lat)}"


def open_tp_dataset(path: Path) -> xr.Dataset:
    ds = xr.open_dataset(path)[["tp"]]
    if "valid_time" not in ds.coords and "time" in ds.coords:
        ds = ds.rename({"time": "valid_time"})
    if "valid_time" not in ds.coords:
        ds.close()
        raise ValueError(f"{path.name} has no valid_time/time coordinate")
    extra_coords = [coord for coord in ds.coords if coord not in ds.dims]
    if extra_coords:
        ds = ds.drop_vars(extra_coords)
    for coord in ("latitude", "longitude"):
        rounded = np.round(np.asarray(ds[coord].values, dtype="float64"), ERA5_COORD_DECIMALS)
        _, keep_idx = np.unique(rounded, return_index=True)
        if keep_idx.size != rounded.size:
            keep_idx.sort()
            ds = ds.isel({coord: keep_idx})
            rounded = rounded[keep_idx]
        ds = ds.assign_coords({coord: rounded})
    return ds


def load_country_configs(cfg_dir: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(cfg_dir.glob("*_datasets_2024_full_year.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        iso = str(doc.get("study_area", {}).get("country_code", "")).upper()
        if iso:
            out[iso] = {"path": path, "doc": doc}
    return out


def ensure_schema(conn: psycopg.Connection) -> None:
    sql = """
    CREATE SCHEMA IF NOT EXISTS eq;
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS pgrouting;

    CREATE TABLE IF NOT EXISTS eq.era5_precip_weekly_grid (
        country_code text NOT NULL,
        week_start date NOT NULL,
        cell_id text NOT NULL,
        cell_lon double precision NOT NULL,
        cell_lat double precision NOT NULL,
        tp_sum_weekly_mm double precision,
        tp_mean_hourly_mm double precision,
        tp_median_hourly_mm double precision,
        tp_1h_max_mm_per_h double precision,
        geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cell_lon, cell_lat), 4326)) STORED,
        PRIMARY KEY (country_code, week_start, cell_id)
    );
    CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_country_week_idx ON eq.era5_precip_weekly_grid (country_code, week_start);
    CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_cell_idx ON eq.era5_precip_weekly_grid (country_code, cell_id);
    CREATE INDEX IF NOT EXISTS era5_precip_weekly_grid_geometry_gist ON eq.era5_precip_weekly_grid USING GIST (geometry);

    CREATE TABLE IF NOT EXISTS eq.road_era5_cell_map (
        country_code text NOT NULL,
        road_row_id bigint NOT NULL,
        cell_id text NOT NULL,
        cell_lon double precision NOT NULL,
        cell_lat double precision NOT NULL,
        road_probe geometry(Point, 4326),
        cell_geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cell_lon, cell_lat), 4326)) STORED,
        PRIMARY KEY (country_code, road_row_id)
    );
    CREATE INDEX IF NOT EXISTS road_era5_cell_map_country_cell_idx ON eq.road_era5_cell_map (country_code, cell_id);
    CREATE INDEX IF NOT EXISTS road_era5_cell_map_probe_gist ON eq.road_era5_cell_map USING GIST (road_probe);

    CREATE TABLE IF NOT EXISTS eq.road_era5_cell_surface_summary (
        country_code text NOT NULL,
        cell_id text NOT NULL,
        road_count bigint NOT NULL,
        paved_road_count bigint NOT NULL,
        unpaved_road_count bigint NOT NULL,
        unknown_road_count bigint NOT NULL,
        has_paved boolean NOT NULL,
        has_unpaved boolean NOT NULL,
        has_unknown boolean NOT NULL,
        PRIMARY KEY (country_code, cell_id)
    );

    CREATE TABLE IF NOT EXISTS eq.era5_precip_cell_overlay (
        country_code text NOT NULL,
        week_start date NOT NULL,
        scenario text NOT NULL,
        surface_scope text NOT NULL,
        cell_id text NOT NULL,
        cell_lon double precision NOT NULL,
        cell_lat double precision NOT NULL,
        road_count bigint NOT NULL,
        tp_sum_weekly_mm double precision,
        tp_mean_hourly_mm double precision,
        tp_median_hourly_mm double precision,
        tp_1h_max_mm_per_h double precision,
        geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(cell_lon, cell_lat), 4326)) STORED,
        PRIMARY KEY (country_code, week_start, scenario, surface_scope, cell_id)
    );
    CREATE INDEX IF NOT EXISTS era5_precip_cell_overlay_country_week_idx ON eq.era5_precip_cell_overlay (country_code, week_start);
    CREATE INDEX IF NOT EXISTS era5_precip_cell_overlay_scope_idx ON eq.era5_precip_cell_overlay (country_code, scenario, surface_scope);
    CREATE INDEX IF NOT EXISTS era5_precip_cell_overlay_geometry_gist ON eq.era5_precip_cell_overlay USING GIST (geometry);

    CREATE TABLE IF NOT EXISTS eq.city_destinations (
        country_code text NOT NULL,
        geoname_id bigint NOT NULL,
        name text NOT NULL,
        ascii_name text,
        feature_class text,
        feature_code text,
        admin1_code text,
        population bigint,
        lon double precision NOT NULL,
        lat double precision NOT NULL,
        geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
        PRIMARY KEY (country_code, geoname_id)
    );
    CREATE INDEX IF NOT EXISTS city_destinations_country_pop_idx ON eq.city_destinations (country_code, population DESC);
    CREATE INDEX IF NOT EXISTS city_destinations_geometry_gist ON eq.city_destinations USING GIST (geometry);

    CREATE TABLE IF NOT EXISTS eq.crop_origin_candidates (
        country_code text NOT NULL,
        crop_code text NOT NULL,
        candidate_rank integer NOT NULL,
        harvested_area double precision NOT NULL,
        lon double precision NOT NULL,
        lat double precision NOT NULL,
        source_file text NOT NULL,
        geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
        PRIMARY KEY (country_code, crop_code, candidate_rank)
    );
    CREATE INDEX IF NOT EXISTS crop_origin_candidates_country_crop_idx ON eq.crop_origin_candidates (country_code, crop_code, candidate_rank);
    CREATE INDEX IF NOT EXISTS crop_origin_candidates_geometry_gist ON eq.crop_origin_candidates USING GIST (geometry);

    CREATE TABLE IF NOT EXISTS eq.boxplot_stats_weekly (
        country_code text NOT NULL,
        week_start date NOT NULL,
        scenario text NOT NULL,
        surface_scope text NOT NULL,
        factor text NOT NULL,
        n_values bigint NOT NULL,
        min_value double precision,
        q25 double precision,
        median double precision,
        q75 double precision,
        max_value double precision,
        PRIMARY KEY (country_code, week_start, scenario, surface_scope, factor)
    );
    CREATE INDEX IF NOT EXISTS boxplot_stats_weekly_country_week_idx ON eq.boxplot_stats_weekly (country_code, week_start);
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def load_road_surface(conn: psycopg.Connection, engine: sa.Engine, iso: str, force: bool) -> bool:
    table = f"road_surface_{iso.lower()}"
    if not force and table_exists(conn, "public", table):
        log(f"[skip] {iso} roads table exists: public.{table}")
        return True
    base = ROOT / "data/raw/road_surface" / iso
    parquet = base / f"heigit_{iso.lower()}_roadsurface_lines.parquet"
    gpkg = base / f"heigit_{iso.lower()}_roadsurface_lines.gpkg"
    if parquet.exists():
        src = parquet
        gdf = gpd.read_parquet(src)
    elif gpkg.exists():
        src = gpkg
        gdf = gpd.read_file(src)
    else:
        log(f"[skip] {iso} roads missing in {base}")
        return False
    t0 = time.time()
    log(f"[start] {iso} load roads src={src.name} rows={len(gdf):,}")
    gdf = gdf.to_crs("EPSG:4326")
    gdf.to_postgis(table, engine, if_exists="replace", index=True, index_label="id")
    with engine.begin() as sa_conn:
        sa_conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {table}_geometry_gist ON {table} USING GIST (geometry)"))
        sa_conn.execute(sa.text(f"ANALYZE {table}"))
    log(f"[done] {iso} load roads table=public.{table} elapsed_s={time.time() - t0:.1f}")
    return True


def load_era5_grid(conn: psycopg.Connection, iso: str, cfg: dict, force: bool) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT week_start FROM eq.era5_precip_weekly_grid WHERE country_code=%s ORDER BY week_start",
            (iso,),
        )
        existing_week_set = {row[0] for row in cur.fetchall()}
    existing_weeks = len(existing_week_set)
    if not force and existing_weeks >= 53:
        log(f"[skip] {iso} ERA5 weekly grid exists weeks={existing_weeks}")
        return True
    source_files = cfg["doc"].get("datasets", {}).get("era5", {}).get("source_files") or []
    paths_by_month: dict[tuple[int, int], Path] = {}
    for name in source_files:
        path = ROOT / "data/raw/era5" / name
        if not path.exists() or path.stat().st_size == 0:
            log(f"[skip] {iso} ERA5 missing file={name}")
            return False
        token = name.removesuffix(".nc").split("-")
        paths_by_month[(int(token[-2]), int(token[-1]))] = path
    if len(paths_by_month) < 12:
        log(f"[skip] {iso} ERA5 expected 12 monthly files, found={len(paths_by_month)}")
        return False

    t0 = time.time()
    log(f"[start] {iso} ERA5 weekly grid months={len(paths_by_month)}")
    if force:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM eq.era5_precip_weekly_grid WHERE country_code=%s", (iso,))
        conn.commit()
        existing_week_set.clear()
    elif existing_weeks > 0:
        log(f"[resume] {iso} ERA5 weekly grid existing_weeks={existing_weeks} missing={53 - existing_weeks}")

    ds_by_month = {key: open_tp_dataset(path) for key, path in sorted(paths_by_month.items())}
    lat_parts = [np.asarray(ds.latitude.values, dtype="float64") for ds in ds_by_month.values()]
    lon_parts = [np.asarray(ds.longitude.values, dtype="float64") for ds in ds_by_month.values()]
    first_lat = lat_parts[0]
    lat_ascending = bool(first_lat[0] < first_lat[-1]) if first_lat.size > 1 else True
    lats = np.array(sorted({round(float(v), ERA5_COORD_DECIMALS) for v in np.concatenate(lat_parts)}))
    if not lat_ascending:
        lats = lats[::-1]
    lons = np.array(sorted({round(float(v), ERA5_COORD_DECIMALS) for v in np.concatenate(lon_parts)}))
    cell_count = int(lats.size * lons.size)
    cell_ids = [era5_cell_id(lon, lat) for lat in lats for lon in lons]
    lon_flat = np.tile(lons, lats.size)
    lat_flat = np.repeat(lats, lons.size)
    copy_sql = """
    COPY eq.era5_precip_weekly_grid (
        country_code, week_start, cell_id, cell_lon, cell_lat,
        tp_sum_weekly_mm, tp_mean_hourly_mm, tp_median_hourly_mm, tp_1h_max_mm_per_h
    ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
    """
    with conn.cursor() as cur:
        for idx, ws in enumerate(week_starts(), start=1):
            if ws in existing_week_set:
                if idx == 1 or idx % 10 == 0:
                    log(f"[progress] {iso} ERA5 week {idx}/53 already exists elapsed_s={time.time() - t0:.1f}")
                continue
            we = week_end(ws)
            start_time = pd.Timestamp(ws) + pd.Timedelta(hours=1)
            end_time = pd.Timestamp(we) + pd.Timedelta(days=1)
            arrays = []
            for key in months_between(start_time, end_time):
                ds = ds_by_month.get(key)
                if ds is None:
                    continue
                da = ds["tp"].sel(valid_time=slice(start_time, end_time))
                if da.sizes.get("valid_time", 0):
                    arrays.append(da.reindex(latitude=lats, longitude=lons))
            if not arrays:
                raise RuntimeError(f"{iso} no ERA5 tp values for week {ws}")
            da_week = arrays[0] if len(arrays) == 1 else xr.concat(arrays, dim="valid_time", join="exact").sortby("valid_time")
            values = np.asarray(da_week.values, dtype="float32")
            inc = hourly_increment_mm(values)
            all_nan = np.isnan(inc).all(axis=0)
            with np.errstate(invalid="ignore"):
                sum_mm = np.nansum(inc, axis=0)
                mean_mm = np.nanmean(inc, axis=0)
                median_mm = np.nanmedian(inc, axis=0)
                max_mm = np.nanmax(inc, axis=0)
            sum_mm[all_nan] = np.nan
            sum_flat = sum_mm.reshape(-1)
            mean_flat = mean_mm.reshape(-1)
            median_flat = median_mm.reshape(-1)
            max_flat = max_mm.reshape(-1)
            buf = io.StringIO()
            week_s = ws.isoformat()
            for i in range(cell_count):
                buf.write(
                    f"{iso}\t{week_s}\t{cell_ids[i]}\t{lon_flat[i]:.6f}\t{lat_flat[i]:.6f}\t"
                    f"{fmt(sum_flat[i])}\t{fmt(mean_flat[i])}\t{fmt(median_flat[i])}\t{fmt(max_flat[i])}\n"
                )
            buf.seek(0)
            with cur.copy(copy_sql) as cp:
                cp.write(buf.getvalue())
            conn.commit()
            if idx == 1 or idx % 10 == 0:
                log(f"[progress] {iso} ERA5 week {idx}/53 rows={cell_count:,} elapsed_s={time.time() - t0:.1f}")
    for ds in ds_by_month.values():
        ds.close()
    with conn.cursor() as cur:
        cur.execute("ANALYZE eq.era5_precip_weekly_grid")
    conn.commit()
    log(f"[done] {iso} ERA5 weekly grid elapsed_s={time.time() - t0:.1f}")
    return True


def map_roads_to_cells(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    table = f"road_surface_{iso.lower()}"
    iso_sql = qiso_literal(iso)
    if not table_exists(conn, "public", table):
        log(f"[skip] {iso} road-cell map missing road table")
        return False
    road_count = scalar(conn, f"SELECT count(*) FROM public.{qident(table)}")
    mapped = scalar(conn, "SELECT count(*) FROM eq.road_era5_cell_map WHERE country_code=%s", (iso,))
    if not force and mapped == road_count and road_count > 0:
        log(f"[skip] {iso} road-cell map exists rows={mapped:,}")
        return True
    t0 = time.time()
    log(f"[start] {iso} road-cell map roads={road_count:,}")
    sql = f"""
    DELETE FROM eq.road_era5_cell_map WHERE country_code = {iso_sql};
    INSERT INTO eq.road_era5_cell_map (country_code, road_row_id, cell_id, cell_lon, cell_lat, road_probe)
    WITH meta AS (
        SELECT min(cell_lon)::numeric AS min_lon, max(cell_lon)::numeric AS max_lon,
               min(cell_lat)::numeric AS min_lat, max(cell_lat)::numeric AS max_lat,
               min(lon_step)::numeric AS lon_step,
               min(lat_step)::numeric AS lat_step
        FROM (
            SELECT cell_lon, cell_lat,
                   NULLIF(cell_lon - lag(cell_lon) OVER (ORDER BY cell_lon), 0.0) AS lon_step,
                   NULL::double precision AS lat_step
            FROM (SELECT DISTINCT cell_lon, cell_lat FROM eq.era5_precip_weekly_grid
                  WHERE country_code = {iso_sql}
                    AND week_start = (SELECT min(week_start) FROM eq.era5_precip_weekly_grid WHERE country_code = {iso_sql})) g
            UNION ALL
            SELECT cell_lon, cell_lat,
                   NULL::double precision AS lon_step,
                   NULLIF(cell_lat - lag(cell_lat) OVER (ORDER BY cell_lat), 0.0) AS lat_step
            FROM (SELECT DISTINCT cell_lon, cell_lat FROM eq.era5_precip_weekly_grid
                  WHERE country_code = {iso_sql}
                    AND week_start = (SELECT min(week_start) FROM eq.era5_precip_weekly_grid WHERE country_code = {iso_sql})) g
        ) steps
    ), dumped AS (
        SELECT r.id AS road_row_id, (d).geom AS geom
        FROM public.{qident(table)} r
        CROSS JOIN LATERAL ST_Dump(r.geometry) AS d
        WHERE r.geometry IS NOT NULL AND NOT ST_IsEmpty(r.geometry)
    ), probe AS (
        SELECT DISTINCT ON (road_row_id)
            road_row_id,
            ST_LineInterpolatePoint(geom, 0.5) AS road_probe
        FROM dumped
        WHERE GeometryType(geom) = 'LINESTRING'
        ORDER BY road_row_id, ST_Length(geom) DESC
    ), snapped AS (
        SELECT p.road_row_id, p.road_probe,
               LEAST(m.max_lon, GREATEST(m.min_lon, round(((ST_X(p.road_probe)::numeric - m.min_lon) / m.lon_step)) * m.lon_step + m.min_lon)) AS cell_lon,
               LEAST(m.max_lat, GREATEST(m.min_lat, round(((ST_Y(p.road_probe)::numeric - m.min_lat) / m.lat_step)) * m.lat_step + m.min_lat)) AS cell_lat
        FROM probe p CROSS JOIN meta m
    )
    SELECT {iso_sql}, road_row_id,
           to_char(round(cell_lon, 4), 'FM999990.0000') || ':' || to_char(round(cell_lat, 4), 'FM999990.0000') AS cell_id,
           cell_lon::double precision, cell_lat::double precision, road_probe
    FROM snapped;
    ANALYZE eq.road_era5_cell_map;
    """
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    new_rows = scalar(conn, "SELECT count(*) FROM eq.road_era5_cell_map WHERE country_code=%s", (iso,))
    log(f"[done] {iso} road-cell map rows={new_rows:,} elapsed_s={time.time() - t0:.1f}")
    return True


def build_cell_overlay_and_boxes(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    existing = scalar(conn, "SELECT count(*) FROM eq.boxplot_stats_weekly WHERE country_code=%s", (iso,))
    if not force and existing >= 1000:
        log(f"[skip] {iso} cell overlay/boxplots exist box_rows={existing:,}")
        return True
    table = f"road_surface_{iso.lower()}"
    if not table_exists(conn, "public", table):
        log(f"[skip] {iso} overlay missing road table")
        return False
    t0 = time.time()
    log(f"[start] {iso} cell surface summary")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM eq.era5_precip_cell_overlay WHERE country_code=%s", (iso,))
        cur.execute("DELETE FROM eq.road_era5_cell_surface_summary WHERE country_code=%s", (iso,))
        cur.execute("DELETE FROM eq.boxplot_stats_weekly WHERE country_code=%s", (iso,))
        cur.execute(
            f"""
            INSERT INTO eq.road_era5_cell_surface_summary (
                country_code, cell_id, road_count, paved_road_count, unpaved_road_count,
                unknown_road_count, has_paved, has_unpaved, has_unknown
            )
            WITH classified AS (
                SELECT m.country_code, m.cell_id,
                       CASE
                         WHEN lower(r.surface::text) IN ('paved', 'unpaved') THEN lower(r.surface::text)
                         WHEN lower(r.pred_label::text) IN ('paved', 'unpaved') THEN lower(r.pred_label::text)
                         WHEN lower(r.osm_surface_class::text) IN ('paved', 'unpaved') THEN lower(r.osm_surface_class::text)
                         WHEN lower(r.combined_surface_osm_priority::text) IN ('paved', 'unpaved') THEN lower(r.combined_surface_osm_priority::text)
                         WHEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority')) IN ('paved', 'unpaved')
                           THEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority'))
                         ELSE 'unknown'
                       END AS surface_group
                FROM eq.road_era5_cell_map m
                JOIN public.{qident(table)} r ON r.id = m.road_row_id
                WHERE m.country_code = %s
            )
            SELECT country_code, cell_id, count(*),
                   count(*) FILTER (WHERE surface_group='paved'),
                   count(*) FILTER (WHERE surface_group='unpaved'),
                   count(*) FILTER (WHERE surface_group='unknown'),
                   bool_or(surface_group='paved'),
                   bool_or(surface_group='unpaved'),
                   bool_or(surface_group='unknown')
            FROM classified
            GROUP BY country_code, cell_id
            """,
            (iso,),
        )
    conn.commit()
    log(f"[progress] {iso} cell overlay insert")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO eq.era5_precip_cell_overlay (
                country_code, week_start, scenario, surface_scope, cell_id, cell_lon, cell_lat, road_count,
                tp_sum_weekly_mm, tp_mean_hourly_mm, tp_median_hourly_mm, tp_1h_max_mm_per_h
            )
            WITH scoped AS (
                SELECT s.country_code, s.cell_id, v.scenario, v.surface_scope, v.scope_road_count
                FROM eq.road_era5_cell_surface_summary s
                CROSS JOIN LATERAL (
                    VALUES
                    ('actual_unpaved'::text, 'all'::text, s.road_count, true),
                    ('actual_unpaved'::text, 'paved'::text, s.paved_road_count, s.has_paved),
                    ('actual_unpaved'::text, 'unpaved'::text, s.unpaved_road_count, s.has_unpaved),
                    ('unknown_as_paved'::text, 'all'::text, s.road_count, true),
                    ('unknown_as_paved'::text, 'paved'::text, s.paved_road_count + s.unknown_road_count, s.has_paved OR s.has_unknown),
                    ('unknown_as_paved'::text, 'unpaved'::text, s.unpaved_road_count, s.has_unpaved),
                    ('unknown_as_unpaved'::text, 'all'::text, s.road_count, true),
                    ('unknown_as_unpaved'::text, 'paved'::text, s.paved_road_count, s.has_paved),
                    ('unknown_as_unpaved'::text, 'unpaved'::text, s.unpaved_road_count + s.unknown_road_count, s.has_unpaved OR s.has_unknown)
                ) AS v(scenario, surface_scope, scope_road_count, keep_row)
                WHERE s.country_code = %s AND v.keep_row
            )
            SELECT g.country_code, g.week_start, scoped.scenario, scoped.surface_scope, g.cell_id,
                   g.cell_lon, g.cell_lat, scoped.scope_road_count,
                   g.tp_sum_weekly_mm, g.tp_mean_hourly_mm, g.tp_median_hourly_mm, g.tp_1h_max_mm_per_h
            FROM eq.era5_precip_weekly_grid g
            JOIN scoped ON scoped.country_code = g.country_code AND scoped.cell_id = g.cell_id
            WHERE g.country_code = %s
            """,
            (iso, iso),
        )
        cur.execute("ANALYZE eq.era5_precip_cell_overlay")
    conn.commit()
    log(f"[progress] {iso} boxplot stats")
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH flat AS (
                SELECT country_code, week_start, scenario, surface_scope, factor.factor, factor.value
                FROM eq.era5_precip_cell_overlay o
                CROSS JOIN LATERAL (
                    VALUES
                    ('era5_tp_sum_weekly_mm'::text, o.tp_sum_weekly_mm),
                    ('era5_tp_mean_hourly_mm'::text, o.tp_mean_hourly_mm),
                    ('era5_tp_median_hourly_mm'::text, o.tp_median_hourly_mm),
                    ('era5_tp_1h_max_weekly_mm_per_h'::text, o.tp_1h_max_mm_per_h)
                ) AS factor(factor, value)
                WHERE o.country_code = %s AND factor.value IS NOT NULL
            ), agg AS (
                SELECT country_code, week_start, scenario, surface_scope, factor,
                       count(*) AS n_values,
                       min(value) AS min_value,
                       percentile_cont(0.25) WITHIN GROUP (ORDER BY value) AS q25,
                       percentile_cont(0.50) WITHIN GROUP (ORDER BY value) AS median,
                       percentile_cont(0.75) WITHIN GROUP (ORDER BY value) AS q75,
                       max(value) AS max_value
                FROM flat
                GROUP BY country_code, week_start, scenario, surface_scope, factor
            )
            INSERT INTO eq.boxplot_stats_weekly (
                country_code, week_start, scenario, surface_scope, factor,
                n_values, min_value, q25, median, q75, max_value
            )
            SELECT country_code, week_start, scenario, surface_scope, factor,
                   n_values, min_value, q25, median, q75, max_value
            FROM agg
            ON CONFLICT (country_code, week_start, scenario, surface_scope, factor) DO UPDATE SET
                n_values = EXCLUDED.n_values,
                min_value = EXCLUDED.min_value,
                q25 = EXCLUDED.q25,
                median = EXCLUDED.median,
                q75 = EXCLUDED.q75,
                max_value = EXCLUDED.max_value
            """,
            (iso,),
        )
        cur.execute("ANALYZE eq.boxplot_stats_weekly")
    conn.commit()
    box_rows = scalar(conn, "SELECT count(*) FROM eq.boxplot_stats_weekly WHERE country_code=%s", (iso,))
    log(f"[done] {iso} cell overlay/boxplots box_rows={box_rows:,} elapsed_s={time.time() - t0:.1f}")
    return True


def load_cities(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    existing = scalar(conn, "SELECT count(*) FROM eq.city_destinations WHERE country_code=%s", (iso,))
    if not force and existing > 0:
        log(f"[skip] {iso} cities exist rows={existing:,}")
        return True
    country = pycountry.countries.get(alpha_3=iso)
    if country is None:
        log(f"[skip] {iso} pycountry alpha2 missing")
        return False
    alpha2 = country.alpha_2
    rows = []
    with zipfile.ZipFile(ROOT / "data/raw/cities/global/cities500.zip") as zf:
        with zf.open("cities500.txt") as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"), delimiter="\t")
            for row in reader:
                if len(row) < 19 or row[8] != alpha2:
                    continue
                try:
                    pop = int(row[14] or 0)
                    lat = float(row[4])
                    lon = float(row[5])
                    geoname_id = int(row[0])
                except ValueError:
                    continue
                if pop >= CITY_POP_MIN:
                    rows.append((iso, geoname_id, row[1], row[2], row[6], row[7], row[10], pop, lon, lat))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM eq.city_destinations WHERE country_code=%s", (iso,))
        with cur.copy(
            """
            COPY eq.city_destinations (
                country_code, geoname_id, name, ascii_name, feature_class, feature_code,
                admin1_code, population, lon, lat
            ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
            """
        ) as cp:
            for row in rows:
                cp.write_row(row)
        cur.execute("ANALYZE eq.city_destinations")
    conn.commit()
    log(f"[done] {iso} cities rows={len(rows):,}")
    return bool(rows)


def load_crop_candidates(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    existing = scalar(conn, "SELECT count(*) FROM eq.crop_origin_candidates WHERE country_code=%s", (iso,))
    if not force and existing >= 100:
        log(f"[skip] {iso} crop candidates exist rows={existing:,}")
        return True
    gadm = ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg"
    if not gadm.exists():
        log(f"[skip] {iso} GADM missing")
        return False
    boundary = gpd.read_file(gadm, layer="ADM_ADM_0").to_crs("EPSG:4326")
    shapes = [geom.__geo_interface__ for geom in boundary.geometry]
    crop_rows = []
    for tif in sorted((ROOT / "spam_tifs").glob("spam2010V2r0_global_H_*_A.tif")):
        crop = tif.stem.split("_H_")[-1].removesuffix("_A").lower()
        with rasterio.open(tif) as src:
            arr, transform = mask(src, shapes, crop=True, filled=True, nodata=src.nodata)
            data = arr[0].astype("float64")
            valid = np.isfinite(data)
            if src.nodata is not None:
                valid &= data != float(src.nodata)
            valid &= data > 0
            if not valid.any():
                continue
            flat_values = data[valid]
            rows_idx, cols_idx = np.where(valid)
            n = min(TOP_N_CROP, flat_values.size)
            idx = np.argpartition(flat_values, -n)[-n:]
            idx = idx[np.argsort(flat_values[idx])[::-1]]
            for rank, i in enumerate(idx, start=1):
                r = int(rows_idx[i])
                c = int(cols_idx[i])
                lon, lat = rasterio.transform.xy(transform, r, c, offset="center")
                crop_rows.append((iso, crop, rank, float(flat_values[i]), float(lon), float(lat), tif.name))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM eq.crop_origin_candidates WHERE country_code=%s", (iso,))
        with cur.copy(
            """
            COPY eq.crop_origin_candidates (
                country_code, crop_code, candidate_rank, harvested_area, lon, lat, source_file
            ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
            """
        ) as cp:
            for row in crop_rows:
                cp.write_row(row)
        cur.execute("ANALYZE eq.crop_origin_candidates")
    conn.commit()
    log(f"[done] {iso} crop candidates rows={len(crop_rows):,}")
    return bool(crop_rows)


def build_graph(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    table = f"road_surface_{iso.lower()}"
    iso_sql = qiso_literal(iso)
    suffix = iso.lower()
    pgr_table = f"road_graph_edges_pgr_{suffix}"
    if not force and table_exists(conn, "eq", pgr_table):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(pgr_table)}")
        if rows > 0:
            log(f"[skip] {iso} graph exists edges={rows:,}")
            return True
    if not table_exists(conn, "public", table):
        log(f"[skip] {iso} graph missing road table")
        return False
    t0 = time.time()
    edges = f"road_graph_edges_{suffix}"
    nodes = f"road_graph_nodes_{suffix}"
    components = f"road_graph_components_{suffix}"
    sql = f"""
    DROP TABLE IF EXISTS eq.{qident(edges)};
    CREATE TABLE eq.{qident(edges)} AS
    WITH dumped AS (
        SELECT r.id AS road_row_id, r.highway,
               CASE
                 WHEN lower(r.surface::text) IN ('paved', 'unpaved') THEN lower(r.surface::text)
                 WHEN lower(r.pred_label::text) IN ('paved', 'unpaved') THEN lower(r.pred_label::text)
                 WHEN lower(r.osm_surface_class::text) IN ('paved', 'unpaved') THEN lower(r.osm_surface_class::text)
                 WHEN lower(r.combined_surface_osm_priority::text) IN ('paved', 'unpaved') THEN lower(r.combined_surface_osm_priority::text)
                 WHEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority')) IN ('paved', 'unpaved')
                   THEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority'))
                 ELSE 'unknown'
               END AS surface_group,
               (d).path[1] AS part_id,
               (d).geom::geometry(LineString, 4326) AS geometry
        FROM public.{qident(table)} r
        CROSS JOIN LATERAL ST_Dump(r.geometry) AS d
        WHERE r.geometry IS NOT NULL
          AND NOT ST_IsEmpty(r.geometry)
          AND GeometryType((d).geom) = 'LINESTRING'
          AND coalesce(lower(r.highway::text), '') NOT IN {NON_TRUCK_HIGHWAYS}
    ), linework AS (
        SELECT ST_UnaryUnion(ST_Collect(geometry)) AS geometry
        FROM dumped
    ), noded AS (
        SELECT row_number() OVER ()::bigint AS noded_part_id,
               (d).geom::geometry(LineString, 4326) AS geometry
        FROM linework
        CROSS JOIN LATERAL ST_Dump(ST_Node(linework.geometry)) AS d
        WHERE GeometryType((d).geom) = 'LINESTRING'
          AND ST_NPoints((d).geom) >= 2
          AND ST_Length((d).geom::geography) > 0
    ), attributed AS (
        SELECT DISTINCT ON (n.noded_part_id)
               d.road_row_id,
               n.noded_part_id::integer AS part_id,
               d.highway,
               d.surface_group,
               n.geometry
        FROM noded n
        JOIN dumped d
          ON d.geometry && n.geometry
         AND ST_CoveredBy(n.geometry, d.geometry)
        ORDER BY n.noded_part_id, d.road_row_id, d.part_id
    ), endpoints AS (
        SELECT road_row_id, part_id, highway, surface_group,
               ST_StartPoint(geometry) AS start_geom,
               ST_EndPoint(geometry) AS end_geom,
               NULLIF(ST_Length(geometry::geography) / 1000.0, 0.0) AS length_km,
               geometry
        FROM attributed
        WHERE ST_NPoints(geometry) >= 2
    ), speeded AS (
        SELECT road_row_id, part_id, highway, surface_group,
               CASE
                 WHEN highway IN ('motorway','motorway_link') THEN 90.0
                 WHEN highway IN ('trunk','trunk_link') THEN 80.0
                 WHEN highway IN ('primary','primary_link') THEN 70.0
                 WHEN highway IN ('secondary','secondary_link') THEN 60.0
                 WHEN highway IN ('tertiary','tertiary_link') THEN 50.0
                 WHEN highway IN ('unclassified','residential') THEN 35.0
                 WHEN highway IN ('service') THEN 25.0
                 WHEN highway IN ('track') THEN 20.0
                 ELSE 30.0
               END
               * CASE WHEN surface_group='unpaved' THEN 0.75 WHEN surface_group='unknown' THEN 0.85 ELSE 1.0 END AS base_speed_kmh,
               length_km, start_geom, end_geom, geometry
        FROM endpoints
        WHERE length_km IS NOT NULL
    )
    SELECT row_number() OVER ()::bigint AS edge_id, {iso_sql}::text AS country_code,
           road_row_id, part_id,
           md5(round(ST_X(start_geom)::numeric, 5)::text || ':' || round(ST_Y(start_geom)::numeric, 5)::text) AS source_node_id,
           md5(round(ST_X(end_geom)::numeric, 5)::text || ':' || round(ST_Y(end_geom)::numeric, 5)::text) AS target_node_id,
           ST_X(start_geom) AS source_lon, ST_Y(start_geom) AS source_lat,
           ST_X(end_geom) AS target_lon, ST_Y(end_geom) AS target_lat,
           highway, surface_group, base_speed_kmh, length_km,
           length_km / NULLIF(base_speed_kmh, 0.0) AS base_time_h,
           geometry
    FROM speeded;
    ALTER TABLE eq.{qident(edges)} ADD PRIMARY KEY (edge_id);
    CREATE INDEX {edges}_road_idx ON eq.{qident(edges)} (road_row_id);
    CREATE INDEX {edges}_source_idx ON eq.{qident(edges)} (source_node_id);
    CREATE INDEX {edges}_target_idx ON eq.{qident(edges)} (target_node_id);
    ANALYZE eq.{qident(edges)};

    DROP TABLE IF EXISTS eq.{qident(nodes)};
    CREATE TABLE eq.{qident(nodes)} AS
    WITH raw_nodes AS (
        SELECT source_node_id AS node_key, source_lon AS lon, source_lat AS lat FROM eq.{qident(edges)}
        UNION ALL
        SELECT target_node_id AS node_key, target_lon AS lon, target_lat AS lat FROM eq.{qident(edges)}
    ), grouped AS (
        SELECT node_key, avg(lon) AS lon, avg(lat) AS lat FROM raw_nodes GROUP BY node_key
    )
    SELECT row_number() OVER ()::bigint AS node_id, node_key, lon, lat,
           ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geometry(Point, 4326) AS geometry
    FROM grouped;
    ALTER TABLE eq.{qident(nodes)} ADD PRIMARY KEY (node_id);
    CREATE UNIQUE INDEX {nodes}_key_idx ON eq.{qident(nodes)} (node_key);
    CREATE INDEX {nodes}_geom_gist ON eq.{qident(nodes)} USING GIST (geometry);
    ANALYZE eq.{qident(nodes)};

    DROP TABLE IF EXISTS eq.{qident(pgr_table)};
    CREATE TABLE eq.{qident(pgr_table)} AS
    SELECT e.edge_id AS id, ns.node_id AS source, nt.node_id AS target,
           e.road_row_id, e.part_id, e.highway, e.surface_group, e.base_speed_kmh,
           e.length_km, e.base_time_h AS cost, e.base_time_h AS reverse_cost
    FROM eq.{qident(edges)} e
    JOIN eq.{qident(nodes)} ns ON ns.node_key = e.source_node_id
    JOIN eq.{qident(nodes)} nt ON nt.node_key = e.target_node_id
    WHERE e.base_time_h IS NOT NULL AND e.base_time_h > 0;
    ALTER TABLE eq.{qident(pgr_table)} ADD PRIMARY KEY (id);
    CREATE INDEX {pgr_table}_source_idx ON eq.{qident(pgr_table)} (source);
    CREATE INDEX {pgr_table}_target_idx ON eq.{qident(pgr_table)} (target);
    CREATE INDEX {pgr_table}_road_idx ON eq.{qident(pgr_table)} (road_row_id);
    ANALYZE eq.{qident(pgr_table)};

    DROP TABLE IF EXISTS eq.{qident(components)};
    CREATE TABLE eq.{qident(components)} AS
    SELECT * FROM pgr_connectedComponents('SELECT id, source, target, cost, reverse_cost FROM eq.{pgr_table}');
    CREATE INDEX {components}_node_idx ON eq.{qident(components)} (node);
    CREATE INDEX {components}_component_idx ON eq.{qident(components)} (component);
    ANALYZE eq.{qident(components)};
    """
    log(f"[start] {iso} graph build")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(pgr_table)}")
    log(f"[done] {iso} graph edges={rows:,} elapsed_s={time.time() - t0:.1f}")
    return True


def snap_and_select(conn: psycopg.Connection, iso: str, force: bool) -> bool:
    suffix = iso.lower()
    iso_sql = qiso_literal(iso)
    nodes = f"road_graph_nodes_{suffix}"
    components = f"road_graph_components_{suffix}"
    if not table_exists(conn, "eq", nodes):
        log(f"[skip] {iso} snap/select missing graph nodes")
        return False
    selected = f"crop_origin_selected_{suffix}"
    if not force and table_exists(conn, "eq", selected):
        rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(selected)}")
        if rows > 0:
            log(f"[skip] {iso} selected origins exist rows={rows:,}")
            return True
    origin_nodes = f"crop_origin_nodes_{suffix}"
    city_nodes = f"city_destination_nodes_{suffix}"
    city_components = f"city_destination_components_{suffix}"
    sql = f"""
    DROP TABLE IF EXISTS eq.{qident(origin_nodes)};
    CREATE TABLE eq.{qident(origin_nodes)} AS
    SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area, o.lon, o.lat,
           n.node_id, ST_Distance(o.geometry::geography, n.geometry::geography) AS node_distance_m, o.geometry
    FROM eq.crop_origin_candidates o
    CROSS JOIN LATERAL (
        SELECT node_id, geometry
        FROM eq.{qident(nodes)} n
        WHERE ST_DWithin(o.geometry::geography, n.geometry::geography, 2500.0)
        ORDER BY n.geometry <-> o.geometry
        LIMIT 1
    ) n
    WHERE o.country_code = {iso_sql};
    ALTER TABLE eq.{qident(origin_nodes)} ADD PRIMARY KEY (country_code, crop_code, candidate_rank);
    CREATE INDEX {origin_nodes}_node_idx ON eq.{qident(origin_nodes)} (node_id);
    ANALYZE eq.{qident(origin_nodes)};

    DROP TABLE IF EXISTS eq.{qident(city_nodes)};
    CREATE TABLE eq.{qident(city_nodes)} AS
    SELECT c.country_code, c.geoname_id, c.name, c.population, c.lon, c.lat,
           n.node_id, ST_Distance(c.geometry::geography, n.geometry::geography) AS node_distance_m, c.geometry
    FROM eq.city_destinations c
    CROSS JOIN LATERAL (
        SELECT node_id, geometry
        FROM eq.{qident(nodes)} n
        WHERE ST_DWithin(c.geometry::geography, n.geometry::geography, 2500.0)
        ORDER BY n.geometry <-> c.geometry
        LIMIT 1
    ) n
    WHERE c.country_code = {iso_sql};
    ALTER TABLE eq.{qident(city_nodes)} ADD PRIMARY KEY (country_code, geoname_id);
    CREATE INDEX {city_nodes}_node_idx ON eq.{qident(city_nodes)} (node_id);
    ANALYZE eq.{qident(city_nodes)};

    DROP TABLE IF EXISTS eq.{qident(city_components)};
    CREATE TABLE eq.{qident(city_components)} AS
    SELECT c.*, cc.component
    FROM eq.{qident(city_nodes)} c
    JOIN eq.{qident(components)} cc ON cc.node = c.node_id;
    ALTER TABLE eq.{qident(city_components)} ADD PRIMARY KEY (country_code, geoname_id);
    CREATE INDEX {city_components}_component_idx ON eq.{qident(city_components)} (component);
    CREATE INDEX {city_components}_node_idx ON eq.{qident(city_components)} (node_id);
    ANALYZE eq.{qident(city_components)};

    DROP TABLE IF EXISTS eq.{qident(selected)};
    CREATE TABLE eq.{qident(selected)} AS
    WITH city_components AS (
        SELECT DISTINCT component FROM eq.{qident(city_components)}
    ), origin_components AS (
        SELECT o.*, oc.component, (city_components.component IS NOT NULL) AS connected_to_city,
               CASE WHEN city_components.component IS NOT NULL THEN
                   row_number() OVER (
                       PARTITION BY o.crop_code, (city_components.component IS NOT NULL)
                       ORDER BY o.harvested_area DESC, o.candidate_rank
                   )
               END AS connected_rank
        FROM eq.{qident(origin_nodes)} o
        JOIN eq.{qident(components)} oc ON oc.node = o.node_id
        LEFT JOIN city_components ON city_components.component = oc.component
    )
    SELECT country_code, crop_code, candidate_rank, connected_rank AS selected_rank,
           harvested_area, lon, lat, node_id, component, node_distance_m, geometry
    FROM origin_components
    WHERE connected_to_city AND connected_rank <= {TOP_N_CONNECTED};
    ALTER TABLE eq.{qident(selected)} ADD PRIMARY KEY (country_code, crop_code, selected_rank);
    CREATE INDEX {selected}_node_idx ON eq.{qident(selected)} (node_id);
    CREATE INDEX {selected}_component_idx ON eq.{qident(selected)} (component);
    ANALYZE eq.{qident(selected)};
    """
    t0 = time.time()
    log(f"[start] {iso} snap origins/cities and select connected origins")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    rows = scalar(conn, f"SELECT count(*) FROM eq.{qident(selected)}")
    log(f"[done] {iso} selected origins rows={rows:,} elapsed_s={time.time() - t0:.1f}")
    return True


def run_country(conn: psycopg.Connection, engine: sa.Engine, iso: str, cfg: dict, force: bool) -> None:
    total_t0 = time.time()
    log(f"========== {iso} start ==========")
    if not load_road_surface(conn, engine, iso, force):
        log(f"========== {iso} skip: roads unavailable ==========")
        return
    if not load_era5_grid(conn, iso, cfg, force):
        log(f"========== {iso} skip: ERA5 unavailable ==========")
        return
    if not map_roads_to_cells(conn, iso, force):
        log(f"========== {iso} skip: road-cell failed ==========")
        return
    if not build_cell_overlay_and_boxes(conn, iso, force):
        log(f"========== {iso} skip: overlay failed ==========")
        return
    load_cities(conn, iso, force)
    load_crop_candidates(conn, iso, force)
    if not build_graph(conn, iso, force):
        log(f"========== {iso} skip: graph failed ==========")
        return
    snap_and_select(conn, iso, force)
    log(f"========== {iso} done elapsed_s={time.time() - total_t0:.1f} ==========")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DB-first country prep pipeline without Dijkstra accessibility.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--sa-url", default=DEFAULT_SA_URL)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CFG_DIR)
    parser.add_argument("--countries", default="all", help="Comma-separated ISO3 list or all.")
    parser.add_argument("--exclude", default="BRA", help="Comma-separated ISO3 list to skip.")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configs = load_country_configs(args.config_dir)
    exclude = {x.strip().upper() for x in args.exclude.split(",") if x.strip()}
    if args.countries.strip().lower() == "all":
        countries = [iso for iso in sorted(configs) if iso not in exclude]
    else:
        countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()]
    log(f"[pipeline] countries={','.join(countries)} exclude={','.join(sorted(exclude))} force={args.force}")
    engine = sa.create_engine(args.sa_url)
    with psycopg.connect(args.db_url) as conn:
        ensure_schema(conn)
        for iso in countries:
            cfg = configs.get(iso)
            if cfg is None:
                log(f"[skip] {iso} missing generated config")
                continue
            try:
                run_country(conn, engine, iso, cfg, args.force)
            except Exception as exc:
                conn.rollback()
                log(f"[error] {iso} {type(exc).__name__}: {exc}")
                log(f"========== {iso} failed; continuing ==========")
    log("[pipeline] complete")


if __name__ == "__main__":
    main()
