"""Stream annual road/cell factor boxplots without materializing a full overlay."""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import resource
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import duckdb
import matplotlib
import numpy as np
import pandas as pd
import pyogrio
import yaml
from src.data import run_multisource_road_overlay as overlay
from src.data.config import load_config
from src.data.road_input_utils import (
    count_roads_postgis,
    country_layer,
    geometry_probe_point,
    load_roads,
    road_surface_class,
)

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

try:
    from tqdm.auto import tqdm
except Exception:  # pragma: no cover
    def tqdm(iterable=None, **kwargs):
        return iterable

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None


THRESHOLD_LEVELS = (
    "speed_reduction_1",
    "speed_reduction_2",
    "speed_reduction_3",
    "catastrophic_temporary",
    "catastrophic_permanent",
)
THRESHOLD_STYLES = {
    "speed_reduction_1": ("#3b7ddd", "--"),
    "speed_reduction_2": ("#e0a100", "--"),
    "speed_reduction_3": ("#d65f00", "-."),
    "catastrophic_temporary": ("#b00020", "-"),
    "catastrophic_permanent": ("#5c0011", "-"),
}
FACTOR_LABELS = {
    "flood_weekly": "Flood extent binary proxy",
    "flood_depth_weekly_max_m": "Flood depth",
    "visibility_weekly_min_m": "Visibility",
    "pavement_surface_temperature_weekly_max_c": "Pavement surface temperature proxy",
    "soil_moisture_weekly_local_percentile": "Soil moisture local percentile",
    "unpaved_erosion_rainfall_weekly_local_percentile": "Unpaved erosion rainfall local percentile",
    "era5_tp_daily_sum_weekly_max_mm": "ERA5 max daily precipitation total",
    "era5_tp_1h_max_weekly_mm_per_h": "ERA5 hourly precipitation intensity",
    "era5_crosswind_10m_weekly_max_m_s": "ERA5 10 m crosswind",
}
SCENARIO_LABELS = {
    "actual_unpaved": "Actual unpaved roads",
    "unknown_as_paved": "Unknown roads treated as paved",
    "unknown_as_unpaved": "Unknown roads treated as unpaved",
}


@dataclass(frozen=True, slots=True)
class Rule:
    key: str
    factor: str
    direction: str
    surface_scope: str
    thresholds: dict[str, float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build weekly road/cell factor boxplots with streaming chunked aggregation.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--country-code", type=str, required=True)
    parser.add_argument("--damage-config", type=Path, required=True)
    parser.add_argument("--thresholds-yaml", type=Path, required=True)
    parser.add_argument("--bbox", type=float, nargs=4, metavar=("MINX", "MINY", "MAXX", "MAXY"), default=None)
    parser.add_argument("--chunk-size", type=int, default=10000)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--value-round-decimals", type=int, default=6)
    parser.add_argument("--scenario", type=str, default="all")
    parser.add_argument("--max-chunks", type=int, default=None)
    parser.add_argument("--aggregation-unit", choices=("road", "cell"), default="road")
    parser.add_argument("--road-backend", choices=("parquet", "gpkg", "postgis"), default="parquet")
    parser.add_argument("--postgis-dsn", type=str, default="")
    parser.add_argument("--postgis-table", type=str, default="")
    parser.add_argument("--era5-cell-m", type=float, default=11000.0)
    parser.add_argument("--flood-cell-m", type=float, default=20.0)
    parser.add_argument("--visibility-cell-m", type=float, default=50000.0)
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _relpath(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(project_root.resolve()))
    except ValueError:
        return str(resolved)


def _study_bbox(config: dict[str, object], args_bbox: list[float] | None) -> tuple[float, float, float, float] | None:
    if args_bbox is not None:
        return tuple(float(value) for value in args_bbox)
    study_area = config.get("study_area", {}) if isinstance(config, dict) else {}
    raw_bbox = study_area.get("bbox") if isinstance(study_area, dict) else None
    if raw_bbox is None:
        return None
    bbox = tuple(float(value) for value in raw_bbox)
    return bbox if len(bbox) == 4 else None


def _week_end(week_start: date, end_date: date, step_days: int) -> date:
    return min(end_date, week_start + timedelta(days=step_days - 1))


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


def _scenario_names(value: str) -> list[str]:
    supported = ["actual_unpaved", "unknown_as_paved", "unknown_as_unpaved"]
    requested = [part.strip() for part in value.split(",") if part.strip()]
    if not requested or requested == ["all"]:
        return supported
    unknown = sorted(set(requested).difference(supported))
    if unknown:
        raise ValueError(f"Unsupported scenario(s): {', '.join(unknown)}")
    return requested


def _effective_surface(surface_group: pd.Series, scenario: str) -> pd.Series:
    surface = surface_group.astype("string").str.lower().fillna("unknown")
    if scenario == "actual_unpaved":
        return surface
    if scenario == "unknown_as_paved":
        return surface.where(surface != "unknown", "paved")
    if scenario == "unknown_as_unpaved":
        return surface.where(surface != "unknown", "unpaved")
    raise ValueError(f"Unsupported scenario: {scenario}")


def _surface_mask(surface_group: pd.Series, scenario: str, scope: str) -> np.ndarray:
    norm = str(scope or "all").strip().lower().replace("-", "_")
    effective = _effective_surface(surface_group, scenario)
    if norm in {"all", "any", "both", "*"}:
        return np.ones(len(surface_group), dtype=bool)
    if norm.startswith("actual_"):
        target = norm.removeprefix("actual_")
        return surface_group.astype("string").str.lower().fillna("unknown").eq(target).to_numpy(dtype=bool)
    if norm.startswith("effective_"):
        norm = norm.removeprefix("effective_")
    return effective.eq(norm).to_numpy(dtype=bool)


def _threshold_label(level: str) -> str:
    return {
        "speed_reduction_1": "10% speed loss",
        "speed_reduction_2": "25% speed loss",
        "speed_reduction_3": "40% speed loss",
        "catastrophic_temporary": "temporary closure",
        "catastrophic_permanent": "permanent closure",
    }.get(level, level)


def _pretty_factor_name(factor: str) -> str:
    return FACTOR_LABELS.get(factor, factor.replace("_", " "))


def _pretty_scenario_name(scenario: str) -> str:
    return SCENARIO_LABELS.get(scenario, scenario.replace("_", " "))


def _load_rules(path: Path) -> list[Rule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = payload.get("road_hazard_thresholds", payload) if isinstance(payload, dict) else {}
    raw_rules = root.get("rules", []) if isinstance(root, dict) else []
    rules: list[Rule] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        factor = str(item.get("factor", "")).strip()
        if not factor:
            continue
        scope = str(item.get("surface_scope", "all")).strip().lower().replace("-", "_")
        dedupe_key = (factor, scope)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        thresholds: dict[str, float] = {}
        for level in THRESHOLD_LEVELS:
            raw = (item.get("thresholds") or {}).get(level)
            try:
                thresholds[level] = float(raw) if raw is not None else math.nan
            except (TypeError, ValueError):
                thresholds[level] = math.nan
        rules.append(
            Rule(
                key=f"{scope}__{factor}",
                factor=factor,
                direction=str(item.get("direction", "gte")).strip().lower(),
                surface_scope=scope,
                thresholds=thresholds,
            )
        )
    return rules


def _road_sql(layer_name: str, include_orientation: bool) -> str:
    cols = [
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
    ]
    select = ["ST_Line_Interpolate_Point(geom, 0.5) AS geom", *cols]
    if include_orientation:
        select.extend(
            [
                "ST_X(ST_Line_Interpolate_Point(geom, 0.0)) AS start_x",
                "ST_Y(ST_Line_Interpolate_Point(geom, 0.0)) AS start_y",
                "ST_X(ST_Line_Interpolate_Point(geom, 1.0)) AS end_x",
                "ST_Y(ST_Line_Interpolate_Point(geom, 1.0)) AS end_y",
            ]
        )
    return "SELECT " + ", ".join(select) + f" FROM {layer_name}"


def _era5_month_key_from_path(path: Path) -> tuple[int, int] | None:
    name = path.stem.lower()
    parts = name.split("-")
    if len(parts) < 2:
        return None
    try:
        year = int(parts[-2])
        month = int(parts[-1])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    return year, month


def _era5_lookup(paths: list[Path]) -> dict[tuple[int, int], Path]:
    lookup: dict[tuple[int, int], Path] = {}
    for path in paths:
        key = _era5_month_key_from_path(path)
        if key is not None:
            lookup[key] = path
    return lookup


def _era5_paths_for_week(lookup: dict[tuple[int, int], Path], week_start: date, week_end: date) -> list[Path]:
    months = {(week_start.year, week_start.month), (week_end.year, week_end.month)}
    return [lookup[key] for key in sorted(months) if key in lookup]


def _road_chunk(
    project_root: Path,
    iso3: str,
    country: gpd.GeoDataFrame,
    *,
    skip_features: int,
    chunk_size: int,
    include_orientation: bool,
    road_backend: str,
    postgis_dsn: str,
    postgis_table: str,
) -> gpd.GeoDataFrame:
    columns = [
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
        "highway",
    ]
    geometry_mode = "line"
    frame = load_roads(
        project_root,
        iso3,
        country,
        geometry_mode=geometry_mode,
        skip_features=skip_features,
        max_features=chunk_size,
        columns=columns,
        road_backend=road_backend,
        postgis_dsn=postgis_dsn,
        postgis_table=postgis_table,
    )
    # Keep orientation inputs when requested.
    if include_orientation and road_backend == "gpkg":
        # Existing GPKG path keeps orientation via SQL in old implementation.
        # Fallback: approximate from geometry endpoints below if columns absent.
        pass
    if frame.crs is None:
        frame = frame.set_crs("EPSG:4326")
    return frame.to_crs("EPSG:4326")


def _road_orientation_vectors(frame: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    if not {"start_x", "start_y", "end_x", "end_y"}.issubset(frame.columns):
        return np.zeros(len(frame), dtype="float64"), np.ones(len(frame), dtype="float64")
    dx = pd.to_numeric(frame["end_x"], errors="coerce") - pd.to_numeric(frame["start_x"], errors="coerce")
    dy = pd.to_numeric(frame["end_y"], errors="coerce") - pd.to_numeric(frame["start_y"], errors="coerce")
    length = np.hypot(dx.to_numpy(dtype="float64"), dy.to_numpy(dtype="float64"))
    ux = np.zeros(len(frame), dtype="float64")
    uy = np.ones(len(frame), dtype="float64")
    valid = np.isfinite(length) & (length > 0)
    ux[valid] = dx.to_numpy(dtype="float64")[valid] / length[valid]
    uy[valid] = dy.to_numpy(dtype="float64")[valid] / length[valid]
    return ux, uy


def _valid_probe_point_mask(geoms: gpd.GeoSeries) -> np.ndarray:
    return np.asarray([geom is not None and not geom.is_empty for geom in geoms], dtype=bool)


def _source_for_factor(factor: str) -> str:
    if factor.startswith("era5_") or factor in {
        "soil_moisture_weekly_raw",
        "pavement_surface_temperature_weekly_max_c",
        "unpaved_erosion_rainfall_weekly_local_percentile",
        "soil_moisture_weekly_local_percentile",
    }:
        return "era5"
    if factor.startswith("flood"):
        return "flood"
    if factor.startswith("visibility"):
        return "visibility"
    return "road"


def _cell_keys(
    probe_points_wgs84: gpd.GeoSeries,
    *,
    cell_m: float,
) -> np.ndarray:
    if cell_m <= 0 or probe_points_wgs84.empty:
        return np.arange(len(probe_points_wgs84), dtype="int64").astype(str)
    points = probe_points_wgs84.to_crs("EPSG:3857")
    xs = np.asarray([geom.x if geom and not geom.is_empty else np.nan for geom in points], dtype="float64")
    ys = np.asarray([geom.y if geom and not geom.is_empty else np.nan for geom in points], dtype="float64")
    ix = np.floor(xs / float(cell_m)).astype("float64")
    iy = np.floor(ys / float(cell_m)).astype("float64")
    return (pd.Series(ix).astype("Int64").astype(str) + ":" + pd.Series(iy).astype("Int64").astype(str)).to_numpy(dtype=str)


def _unique_cell_values(
    values: np.ndarray,
    cell_keys: np.ndarray,
    *,
    mask: np.ndarray | None,
    seen: set[str],
) -> np.ndarray:
    finite_values = np.asarray(values, dtype="float64")
    keep = np.isfinite(finite_values)
    if mask is not None:
        keep &= np.asarray(mask, dtype=bool)
    if not keep.any():
        return np.asarray([], dtype="float64")
    keys = np.asarray(cell_keys, dtype=str)
    selected_values: list[float] = []
    for key, value in zip(keys[keep], finite_values[keep], strict=False):
        if key in seen:
            continue
        seen.add(str(key))
        selected_values.append(float(value))
    return np.asarray(selected_values, dtype="float64")


def _has_visibility_station_files(raw_root: Path, iso3: str, *, start_date: date, end_date: date) -> bool:
    root = raw_root / "visibility_noaa_isd" / iso3
    for year in range(start_date.year, end_date.year + 1):
        year_dir = root / str(year)
        if any(year_dir.glob("*.csv")):
            return True
    return False


def _has_usable_visibility_values(raw_root: Path, iso3: str, *, start_date: date, end_date: date) -> bool:
    root = raw_root / "visibility_noaa_isd" / iso3
    for year in range(start_date.year, end_date.year + 1):
        for csv_path in sorted((root / str(year)).glob("*.csv")):
            try:
                frame = pd.read_csv(csv_path, usecols=["VIS"], nrows=500)
            except Exception:
                continue
            if "VIS" not in frame.columns:
                continue
            values = frame["VIS"].astype("string").str.extract(r"^(\d+)")[0].pipe(pd.to_numeric, errors="coerce")
            if values.lt(999999).any():
                return True
    return False


def _base_factor_dependencies(rules: list[Rule]) -> set[str]:
    deps: set[str] = set()
    for rule in rules:
        if rule.factor == "unpaved_erosion_rainfall_weekly_local_percentile":
            deps.add("era5_tp_1h_max_weekly_mm_per_h")
        elif rule.factor == "soil_moisture_weekly_local_percentile":
            deps.add("soil_moisture_weekly_raw")
        else:
            deps.add(rule.factor)
    return deps


def _log_stage(message: str) -> None:
    print(message, flush=True)


def _memory_snapshot() -> str:
    parts: list[str] = []
    if psutil is not None:
        rss_gb = psutil.Process(os.getpid()).memory_info().rss / (1024**3)
        parts.append(f"rss={rss_gb:.2f} GB")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if peak:
        peak_gb = peak / (1024**3) if sys.platform == "darwin" else peak / (1024**2)
        parts.append(f"peak={peak_gb:.2f} GB")
    return " | ".join(parts) if parts else "memory=n/a"


def _open_db(path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(path))
    conn.execute("PRAGMA threads=4")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS counts (
            metric_key TEXT NOT NULL,
            week_start TEXT NOT NULL,
            value REAL NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY (metric_key, week_start, value)
        )
        """
    )
    return conn


def _update_counts(
    conn: duckdb.DuckDBPyConnection,
    metric_key: str,
    week_start: str,
    values: np.ndarray,
    *,
    decimals: int,
) -> None:
    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return
    rounded = np.round(finite, decimals=decimals)
    unique, counts = np.unique(rounded, return_counts=True)
    conn.executemany(
        """
        INSERT INTO counts(metric_key, week_start, value, count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(metric_key, week_start, value)
        DO UPDATE SET count = count + excluded.count
        """,
        [(metric_key, week_start, float(value), int(count)) for value, count in zip(unique, counts, strict=False)],
    )


def _query_counts(conn: duckdb.DuckDBPyConnection, metric_key: str, week_start: str) -> pd.DataFrame:
    return conn.execute(
        "SELECT value, count FROM counts WHERE metric_key = ? AND week_start = ? ORDER BY value",
        [metric_key, week_start],
    ).df()


def _weighted_stats(frame: pd.DataFrame) -> dict[str, float | int | None]:
    if frame.empty:
        return {"n_values": 0, "min": None, "q25": None, "median": None, "q75": None, "max": None}
    values = frame["value"].to_numpy(dtype="float64")
    counts = frame["count"].to_numpy(dtype="int64")
    total = int(counts.sum())
    cum = np.cumsum(counts)

    def pick(q: float) -> float:
        threshold = max(1, math.ceil(q * total))
        idx = int(np.searchsorted(cum, threshold, side="left"))
        return float(values[min(idx, len(values) - 1)])

    return {
        "n_values": total,
        "min": float(values[0]),
        "q25": pick(0.25),
        "median": pick(0.5),
        "q75": pick(0.75),
        "max": float(values[-1]),
    }


def _counts_to_percentiles(raw_counts: pd.DataFrame, scoped_counts: pd.DataFrame) -> pd.DataFrame:
    if raw_counts.empty or scoped_counts.empty:
        return pd.DataFrame(columns=["value", "count"])
    raw = raw_counts.sort_values("value").reset_index(drop=True)
    scoped = scoped_counts.sort_values("value").reset_index(drop=True)
    total = int(raw["count"].sum())
    if total <= 0:
        return pd.DataFrame(columns=["value", "count"])
    raw["cum"] = raw["count"].cumsum()
    percentile_by_value = {
        float(row.value): float(row.cum / total * 100.0)
        for row in raw.itertuples(index=False)
    }
    out = scoped.copy()
    out["value"] = out["value"].map(percentile_by_value)
    out = out.dropna(subset=["value"])
    return out.groupby("value", as_index=False)["count"].sum().sort_values("value").reset_index(drop=True)


def _plot_rule(
    weeks: list[str],
    weekly_stats: list[dict[str, object]],
    rule: Rule,
    scenario: str,
    out_path: Path,
    *,
    aggregation_unit: str,
) -> None:
    bxp_stats = []
    for week_start, stats in zip(weeks, weekly_stats, strict=False):
        if int(stats["n_values"] or 0) <= 0:
            continue
        bxp_stats.append(
            {
                "label": week_start,
                "med": stats["median"],
                "q1": stats["q25"],
                "q3": stats["q75"],
                "whislo": stats["min"],
                "whishi": stats["max"],
                "fliers": [],
            }
        )

    fig, ax = plt.subplots(figsize=(12.0, 5.8))
    if bxp_stats:
        ax.bxp(bxp_stats, showfliers=False)
    else:
        ax.text(0.5, 0.5, f"No finite {aggregation_unit} values", transform=ax.transAxes, ha="center", va="center")
    for level in THRESHOLD_LEVELS:
        value = rule.thresholds.get(level, math.nan)
        if not np.isfinite(value):
            continue
        color, linestyle = THRESHOLD_STYLES[level]
        ax.axhline(value, color=color, linestyle=linestyle, linewidth=1.4, label=f"{_threshold_label(level)}: {value:g}")
    ax.set_title(f"{_pretty_scenario_name(scenario)} | {rule.surface_scope} | {_pretty_factor_name(rule.factor)}")
    ax.set_xlabel("Week start")
    ax.set_ylabel(f"Factor value per {aggregation_unit}")
    ax.grid(alpha=0.22)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    _log_stage(f"Loading config: {args.config}")
    config = load_config(args.config, country_code_override=args.country_code.upper())
    iso3 = str(config.get("study_area", {}).get("country_code", args.country_code)).upper()
    study_bbox = _study_bbox(config, args.bbox)
    _log_stage(f"Loading damage config: {args.damage_config}")
    damage_cfg = yaml.safe_load(args.damage_config.read_text(encoding="utf-8"))["road_climate_damage"]
    analysis_period = damage_cfg.get("analysis_period", {})
    start_date = _analysis_date(analysis_period.get("start_date"), "2024-01-01")
    end_date = _analysis_date(analysis_period.get("end_date"), "2024-12-31")
    step_days = int(analysis_period.get("aggregation_period_days", 7))
    weeks = _period_week_starts(start_date, end_date, step_days)
    week_tokens = [week.isoformat() for week in weeks]
    _log_stage(f"Loading threshold rules: {args.thresholds_yaml}")
    rules = _load_rules(args.thresholds_yaml)
    scenarios = _scenario_names(args.scenario)
    if not rules:
        raise RuntimeError(f"No threshold rules loaded from {args.thresholds_yaml}")

    raw_root = project_root / "data" / "raw"
    datasets_cfg = dict(config.get("datasets", {}))
    flood_cfg = dict(datasets_cfg.get("flood", {}) or {})
    flood_enabled = bool(flood_cfg.get("enabled", True))
    if not flood_enabled:
        before = len(rules)
        rules = [rule for rule in rules if _source_for_factor(rule.factor) != "flood"]
        dropped = before - len(rules)
        if dropped:
            _log_stage(f"Dropped {dropped} flood rules because datasets.flood.enabled=false")

    base_factors = _base_factor_dependencies(rules)
    requires_orientation = "era5_crosswind_10m_weekly_max_m_s" in base_factors
    needs_flood = flood_enabled and any(_source_for_factor(factor) == "flood" for factor in base_factors)
    source_cell_m = {
        "era5": float(args.era5_cell_m),
        "visibility": float(args.visibility_cell_m),
        "road": 0.0,
    }
    if needs_flood:
        source_cell_m["flood"] = float(args.flood_cell_m)
    road_path = raw_root / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    if args.road_backend == "gpkg":
        _log_stage(f"Inspecting road layer: {road_path}")
        total_features = int(pyogrio.read_info(road_path)["features"])
    else:
        _log_stage(f"Inspecting road layer: postgis table={args.postgis_table or f'road_surface_{iso3.lower()}'}")
        total_features = count_roads_postgis(
            bbox=study_bbox if study_bbox is not None else tuple(country_layer(project_root, iso3).total_bounds),
            dsn=args.postgis_dsn,
            table=args.postgis_table or f"road_surface_{iso3.lower()}",
        )
    n_chunks = max(1, math.ceil(total_features / args.chunk_size))
    if args.max_chunks is not None:
        n_chunks = min(n_chunks, max(0, int(args.max_chunks)))

    period_label = (
        f"{analysis_period.get('start_date', 'na')}_to_{analysis_period.get('end_date', 'na')}_"
        f"{analysis_period.get('aggregation_period_days', 'na')}d"
    )
    out_dir = args.output_root or (project_root / "outputs" / "weekly_factor_boxplots_streaming" / iso3 / period_label)
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "value_counts.duckdb"
    if db_path.exists():
        db_path.unlink()
    conn = _open_db(db_path)

    missing_metrics: dict[str, str] = {}
    era5_paths = overlay._era5_paths_from_config(raw_root, dict(datasets_cfg.get("era5", {})), analysis_start=start_date, analysis_end=end_date)
    era5_ready = bool(era5_paths) and all(path.exists() for path in era5_paths)
    era5_lookup = _era5_lookup(era5_paths) if era5_ready else {}
    if era5_paths:
        _log_stage(f"Resolved ERA5 source files: {len(era5_paths)}")
    print(f"Country: {iso3}", flush=True)
    if study_bbox is not None:
        print(f"BBox: {list(study_bbox)}", flush=True)
    print(f"Scenarios: {', '.join(scenarios)}", flush=True)
    print(
        f"Rules: {len(rules)} | Weeks: {len(weeks)} | Chunk size: {args.chunk_size:,} | "
        f"Road chunks: {n_chunks:,} | Aggregation: {args.aggregation_unit}",
        flush=True,
    )
    if args.aggregation_unit == "cell":
        print(
            "Cell sizes: "
            + ", ".join(f"{source}={cell_m:g}m" for source, cell_m in source_cell_m.items() if source != "road"),
            flush=True,
        )
    road_label = road_path.name if args.road_backend == "gpkg" else (args.postgis_table or f"road_surface_{iso3.lower()}")
    print(f"Road layer: {road_label} | Features: {total_features:,}", flush=True)
    if era5_paths:
        print(f"ERA5 source files: {len(era5_paths)}", flush=True)
    else:
        print("ERA5 source files: 0", flush=True)
    flood_paths_by_week: dict[date, list[Path]] = {week: [] for week in weeks}
    if needs_flood:
        _log_stage("Resolving flood rasters by week...")
        flood_paths_by_week = overlay._flood_paths_by_week_start(
            project_root,
            raw_root,
            weeks,
            bbox=study_bbox,
            progress_label="Flood week mapping",
        )
        _log_stage(f"Flood weeks with data: {sum(1 for paths in flood_paths_by_week.values() if paths)} / {len(weeks)}")
    else:
        _log_stage("Skipping flood raster mapping (flood disabled or no flood rules).")
    print("Starting chunked aggregation...", flush=True)
    if needs_flood and "flood_weekly" in base_factors and not any(flood_paths_by_week.values()):
        missing_metrics.setdefault("flood_weekly", "missing_flood_weekly_catalog_mapping_or_files")
    if "visibility_weekly_min_m" in base_factors:
        if not _has_visibility_station_files(
            raw_root,
            iso3,
            start_date=start_date,
            end_date=end_date,
        ):
            missing_metrics.setdefault("visibility_weekly_min_m", "missing_visibility_station_csvs")
        elif not _has_usable_visibility_values(
            raw_root,
            iso3,
            start_date=start_date,
            end_date=end_date,
        ):
            missing_metrics.setdefault("visibility_weekly_min_m", "no_usable_visibility_values")

    seen_cells: dict[tuple[str, str, str, str], set[str]] = {}
    era5_dataset_cache: dict[tuple[str, ...], object] = {}

    def _cached_week_era5_dataset(paths: list[Path]):
        key = tuple(str(path) for path in paths)
        ds = era5_dataset_cache.get(key)
        if ds is None:
            ds = overlay._open_era5_dataset(paths)
            era5_dataset_cache[key] = ds
        return ds

    chunk_bar = tqdm(range(n_chunks), desc="Road chunks", unit="chunk")
    for chunk_idx in chunk_bar:
            skip = chunk_idx * args.chunk_size
            _log_stage(f"[chunk {chunk_idx + 1}/{n_chunks}] start | {_memory_snapshot()}")
            _log_stage(f"Reading road chunk {chunk_idx + 1}/{n_chunks} (skip={skip:,}, max={args.chunk_size:,})...")
            roads = _road_chunk(
                project_root,
                iso3,
                country_layer(project_root, iso3),
                skip_features=skip,
                chunk_size=args.chunk_size,
                include_orientation=requires_orientation,
                road_backend=args.road_backend,
                postgis_dsn=args.postgis_dsn,
                postgis_table=args.postgis_table,
            )
            if roads.empty:
                _log_stage(f"Chunk {chunk_idx + 1}/{n_chunks}: no roads loaded.")
                continue
            _log_stage(f"Chunk {chunk_idx + 1}/{n_chunks}: loaded {len(roads):,} roads.")
            valid_probe_mask = _valid_probe_point_mask(gpd.GeoSeries(roads.geometry, crs="EPSG:4326"))
            dropped = int((~valid_probe_mask).sum())
            if dropped:
                dropped_pct = 100.0 * dropped / len(roads)
                print(
                    f"Chunk {chunk_idx + 1}/{n_chunks}: dropped {dropped} roads "
                    f"({dropped_pct:.3f}%) with null/empty probe geometries before sampling.",
                    flush=True,
                )
                roads = roads.loc[valid_probe_mask].copy()
            if roads.empty:
                continue
            roads["surface_group"] = road_surface_class(roads)
            probe_points = gpd.GeoSeries(roads.geometry.apply(geometry_probe_point), crs="EPSG:4326")
            lons = np.asarray([geom.x for geom in probe_points], dtype="float64")
            lats = np.asarray([geom.y for geom in probe_points], dtype="float64")
            road_ux, road_uy = _road_orientation_vectors(roads)
            cell_keys_by_source: dict[str, np.ndarray] = {}
            if args.aggregation_unit == "cell":
                for source, cell_m in source_cell_m.items():
                    if source != "road":
                        cell_keys_by_source[source] = _cell_keys(probe_points, cell_m=cell_m)
            mask_keys = {(scenario, rule.surface_scope) for scenario in scenarios for rule in rules}
            masks = {
                key: _surface_mask(roads["surface_group"], key[0], key[1])
                for key in mask_keys
            }

            task_bar = tqdm(weeks, desc=f"Chunk {chunk_idx + 1}/{n_chunks} weeks", unit="week", leave=False)
            for week_idx, week_start in enumerate(task_bar, start=1):
                week_start_str = week_start.isoformat()
                week_end = _week_end(week_start, end_date, step_days)
                task_bar.set_postfix_str(week_start_str)
                if len(weeks) > 8 and (task_bar.n == 0 or (task_bar.n + 1) % 8 == 0):
                    _log_stage(f"Chunk {chunk_idx + 1}/{n_chunks}: sampling week {week_start_str}...")
                computed: dict[str, np.ndarray] = {}
                need_era5 = any(
                    factor in base_factors
                    for factor in {
                        "era5_tp_1h_max_weekly_mm_per_h",
                        "era5_tp_daily_sum_weekly_max_mm",
                        "soil_moisture_weekly_raw",
                        "pavement_surface_temperature_weekly_max_c",
                        "era5_crosswind_10m_weekly_max_m_s",
                    }
                )
                ds_era5_week = None
                if need_era5:
                    if not era5_lookup:
                        missing_metrics.setdefault("era5", "missing_era5_inputs")
                    else:
                        week_era5_paths = _era5_paths_for_week(era5_lookup, week_start, week_end)
                        if not week_era5_paths:
                            missing_metrics.setdefault("era5", f"missing_era5_month_for_{week_start_str}")
                        else:
                            ds_era5_week = _cached_week_era5_dataset(week_era5_paths)

                if "era5_tp_1h_max_weekly_mm_per_h" in base_factors:
                    if ds_era5_week is None:
                        missing_metrics.setdefault("era5_tp_1h_max_weekly_mm_per_h", "missing_era5_inputs")
                    else:
                        computed["era5_tp_1h_max_weekly_mm_per_h"] = overlay._sample_era5_tp_1h_max_mm_per_h(
                            ds_era5_week,
                            lons,
                            lats,
                            start_date=week_start,
                            end_date=week_end,
                        )

                if "era5_tp_daily_sum_weekly_max_mm" in base_factors:
                    if ds_era5_week is None:
                        missing_metrics.setdefault("era5_tp_daily_sum_weekly_max_mm", "missing_era5_inputs")
                    else:
                        computed["era5_tp_daily_sum_weekly_max_mm"] = overlay._sample_era5_tp_daily_sum_weekly_max_mm(
                            ds_era5_week,
                            lons,
                            lats,
                            start_date=week_start,
                            end_date=week_end,
                        )

                if "soil_moisture_weekly_raw" in base_factors:
                    if ds_era5_week is None or "swvl1" not in ds_era5_week:
                        missing_metrics.setdefault("soil_moisture_weekly_local_percentile", "missing_era5_swvl1")
                    else:
                        computed["soil_moisture_weekly_raw"] = overlay._sample_netcdf_var(
                            ds_era5_week["swvl1"], lons, lats, start_date=week_start, end_date=week_end, reducer="mean"
                        )

                if "pavement_surface_temperature_weekly_max_c" in base_factors:
                    if ds_era5_week is None or "skt" not in ds_era5_week:
                        missing_metrics.setdefault("pavement_surface_temperature_weekly_max_c", "missing_era5_skt")
                    else:
                        computed["pavement_surface_temperature_weekly_max_c"] = (
                            overlay._sample_netcdf_var(
                                ds_era5_week["skt"], lons, lats, start_date=week_start, end_date=week_end, reducer="max"
                            )
                            - 273.15
                        )

                if "era5_crosswind_10m_weekly_max_m_s" in base_factors:
                    if ds_era5_week is None:
                        missing_metrics.setdefault("era5_crosswind_10m_weekly_max_m_s", "missing_era5_inputs")
                    else:
                        computed["era5_crosswind_10m_weekly_max_m_s"] = overlay._sample_netcdf_crosswind_speed(
                            ds_era5_week,
                            lons,
                            lats,
                            road_ux,
                            road_uy,
                            start_date=week_start,
                            end_date=week_end,
                            reducer="max",
                        )

                if "visibility_weekly_min_m" in base_factors:
                    computed["visibility_weekly_min_m"] = overlay._sample_noaa_visibility_weekly_min_m(
                        raw_root,
                        iso3,
                        probe_points,
                        week_start=week_start,
                        week_end=week_end,
                    )

                if "flood_weekly" in base_factors:
                    flood_paths = flood_paths_by_week.get(week_start, [])
                    computed["flood_weekly"] = overlay._sample_raster_paths(
                        flood_paths,
                        probe_points,
                        reducer="max",
                        positive_only=True,
                        progress_label=f"Flood rasters {week_start_str}",
                    )

                if "flood_depth_weekly_max_m" in base_factors:
                    if not datasets_cfg.get("flood_depth", {}).get("enabled", False):
                        missing_metrics.setdefault("flood_depth_weekly_max_m", "disabled_in_dataset_config")
                    else:
                        flood_depth_paths = overlay._flood_depth_paths_by_week_start(raw_root, iso3, [week_start]).get(week_start, [])
                        computed["flood_depth_weekly_max_m"] = overlay._sample_raster_paths(
                            flood_depth_paths,
                            probe_points,
                            reducer="max",
                            positive_only=True,
                            progress_label=f"Flood-depth rasters {week_start_str}",
                        )

                for base_factor, values in computed.items():
                    scopes = {rule.surface_scope for rule in rules}
                    source = _source_for_factor(base_factor)
                    for scenario in scenarios:
                        metric_key = f"raw::{base_factor}::{scenario}::all"
                        if args.aggregation_unit == "cell" and source in cell_keys_by_source:
                            seen_key = (source, scenario, "all", week_start_str)
                            selected = _unique_cell_values(
                                values,
                                cell_keys_by_source[source],
                                mask=None,
                                seen=seen_cells.setdefault(seen_key, set()),
                            )
                            _update_counts(conn, metric_key, week_start_str, selected, decimals=args.value_round_decimals)
                        else:
                            _update_counts(conn, metric_key, week_start_str, values, decimals=args.value_round_decimals)
                        for scope in scopes:
                            mask = masks[(scenario, scope)]
                            metric_key = f"raw::{base_factor}::{scenario}::{scope}"
                            if args.aggregation_unit == "cell" and source in cell_keys_by_source:
                                seen_key = (source, scenario, scope, week_start_str)
                                selected = _unique_cell_values(
                                    values,
                                    cell_keys_by_source[source],
                                    mask=mask,
                                    seen=seen_cells.setdefault(seen_key, set()),
                                )
                                _update_counts(conn, metric_key, week_start_str, selected, decimals=args.value_round_decimals)
                            else:
                                _update_counts(conn, metric_key, week_start_str, values[mask], decimals=args.value_round_decimals)
                computed.clear()
                if week_idx % 8 == 0:
                    gc.collect()
                    conn.commit()
            del roads, probe_points, lons, lats, road_ux, road_uy, masks, cell_keys_by_source
            conn.commit()
            for cached in era5_dataset_cache.values():
                cached.close()
            era5_dataset_cache.clear()
            gc.collect()
            _log_stage(f"[chunk {chunk_idx + 1}/{n_chunks}] done | {_memory_snapshot()}")

    diagnostics_rows: list[dict[str, object]] = []
    png_dir = out_dir / "weekly_factor_value_boxplots"
    for scenario in scenarios:
        plot_bar = tqdm(rules, desc=f"Plot rules [{scenario}]", unit="rule")
        for rule in plot_bar:
            weekly_stats: list[dict[str, object]] = []
            for week_start_str in week_tokens:
                if rule.factor == "unpaved_erosion_rainfall_weekly_local_percentile":
                    raw_counts = _query_counts(conn, f"raw::era5_tp_1h_max_weekly_mm_per_h::{scenario}::all", week_start_str)
                    scoped_counts = _query_counts(conn, f"raw::era5_tp_1h_max_weekly_mm_per_h::{scenario}::{rule.surface_scope}", week_start_str)
                    counts = _counts_to_percentiles(raw_counts, scoped_counts)
                elif rule.factor == "soil_moisture_weekly_local_percentile":
                    raw_counts = _query_counts(conn, f"raw::soil_moisture_weekly_raw::{scenario}::all", week_start_str)
                    scoped_counts = _query_counts(conn, f"raw::soil_moisture_weekly_raw::{scenario}::{rule.surface_scope}", week_start_str)
                    counts = _counts_to_percentiles(raw_counts, scoped_counts)
                else:
                    counts = _query_counts(conn, f"raw::{rule.factor}::{scenario}::{rule.surface_scope}", week_start_str)
                stats = _weighted_stats(counts)
                row = {
                    "week_start": week_start_str,
                    "scenario": scenario,
                    "rule_key": rule.key,
                    "factor": rule.factor,
                    "surface_scope": rule.surface_scope,
                    "direction": rule.direction,
                    **stats,
                }
                for level in THRESHOLD_LEVELS:
                    row[f"{level}_threshold"] = rule.thresholds.get(level, math.nan)
                weekly_stats.append(row)
                diagnostics_rows.append(row)
            _plot_rule(
                week_tokens,
                weekly_stats,
                rule,
                scenario,
                png_dir / f"{scenario}__{rule.key}.png",
                aggregation_unit=args.aggregation_unit,
            )

    diagnostics = pd.DataFrame(diagnostics_rows)
    diagnostics_path = out_dir / "weekly_factor_value_diagnostics.csv"
    diagnostics.to_csv(diagnostics_path, index=False)
    summary = {
        "country_code": iso3,
        "analysis_period": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "aggregation_period_days": step_days,
        },
        "bbox": list(study_bbox) if study_bbox is not None else None,
        "chunk_size": args.chunk_size,
        "aggregation_unit": args.aggregation_unit,
        "source_cell_m": source_cell_m if args.aggregation_unit == "cell" else None,
        "scenarios": scenarios,
        "n_rules": len(rules),
        "n_weeks": len(weeks),
        "value_counts_duckdb": _relpath(db_path, project_root),
        "diagnostics_csv": _relpath(diagnostics_path, project_root),
        "png_dir": _relpath(png_dir, project_root),
        "missing_metrics": missing_metrics,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
