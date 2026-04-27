"""Plot road-level factor distributions against configured thresholds."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D
import yaml


matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


THRESHOLD_LEVELS = (
    "speed_reduction_1",
    "speed_reduction_2",
    "speed_reduction_3",
    "catastrophic_temporary",
    "catastrophic_permanent",
)
LEGACY_THRESHOLD_LEVEL_ALIASES = {
    "minor": "speed_reduction_1",
    "moderate": "speed_reduction_2",
    "severe": "speed_reduction_3",
    "catastrophic": "catastrophic_temporary",
}
FACTOR_PREFIXES = (
    "chirps_",
    "flood_",
    "visibility_",
    "pavement_",
    "landslide_",
    "gem_",
    "liquefaction_",
    "worldcover_",
    "soil_",
    "unpaved_erosion_",
    "era5_",
    "cams_",
    "flopros_",
)
THRESHOLD_STYLES = {
    "speed_reduction_1": ("#3b7ddd", "--"),
    "speed_reduction_2": ("#e0a100", "--"),
    "speed_reduction_3": ("#d65f00", "-."),
    "catastrophic_temporary": ("#b00020", "-"),
    "catastrophic_permanent": ("#5c0011", "-"),
}
THRESHOLD_LABELS = {
    "none": "below threshold",
    "speed_reduction_1": "10% speed loss",
    "speed_reduction_2": "25% speed loss",
    "speed_reduction_3": "40% speed loss",
    "catastrophic_temporary": "temporary closure",
    "catastrophic_permanent": "permanent closure",
}
TEMPERATURE_FACTOR_MARKERS = ("era5_t2m_", "era5_skt_")
FACTOR_LABELS = {
    "flood_weekly": "Flood extent binary proxy",
    "flood_depth_weekly_max_m": "Flood depth",
    "visibility_weekly_min_m": "Visibility",
    "pavement_surface_temperature_weekly_max_c": "Pavement surface temperature proxy",
    "soil_moisture_weekly_local_percentile": "Soil moisture local percentile",
    "unpaved_erosion_rainfall_weekly_local_percentile": "Unpaved erosion rainfall local percentile",
    "chirps_weekly_mm": "Weekly rainfall",
    "era5_tp_1h_max_weekly_mm_per_h": "ERA5 hourly precipitation intensity",
    "era5_crosswind_10m_weekly_max_m_s": "ERA5 10 m crosswind",
    "era5_t2m_weekly_mean": "Air temperature",
    "era5_t2m_weekly_max": "Air temperature",
    "era5_skt_weekly_mean": "Surface temperature",
    "era5_skt_weekly_max": "Surface temperature",
    "era5_tp_weekly_sum": "Weekly precipitation",
    "era5_swvl1_weekly_mean": "Soil moisture",
    "era5_u10_weekly_mean": "Wind U component",
    "era5_v10_weekly_mean": "Wind V component",
    "era5_wind_speed_weekly_mean": "Wind speed",
    "era5_wind_speed_weekly_max": "Wind speed",
    "cams_pm2p5_weekly_mean": "PM2.5",
    "cams_pm2p5_weekly_max": "PM2.5",
    "cams_pm10_weekly_mean": "PM10",
    "cams_pm10_weekly_max": "PM10",
    "cams_duaod550_weekly_mean": "Dust optical depth",
    "cams_duaod550_weekly_max": "Dust optical depth",
    "landslide_susceptibility": "Landslide susceptibility",
    "gem_pga_475y": "Seismic hazard (PGA)",
    "liquefaction_class": "Liquefaction susceptibility",
    "worldcover_class": "Land cover class",
    "flopros_merl_riv": "Flood protection level",
    "flopros_dl_max_riv": "Flood protection depth",
    "soil_bdod_0-5cm_Q0.5": "Soil bulk density",
    "soil_clay_0-5cm_Q0.5": "Soil clay content",
    "soil_sand_0-5cm_Q0.5": "Soil sand content",
    "soil_silt_0-5cm_Q0.5": "Soil silt content",
    "soil_soc_0-5cm_Q0.5": "Soil organic carbon",
    "era5_spi_1mo": "SPI 1-month",
    "era5_spi_2mo": "SPI 2-month",
    "era5_spi_3mo": "SPI 3-month",
    "era5_spi_6mo": "SPI 6-month",
    "era5_spi_9mo": "SPI 9-month",
    "era5_spi_12mo": "SPI 12-month",
}
FLOOD_WEEK_RE = re.compile(r"^flood_week_\d{4}_\d{2}_\d{2}$")
CHIRPS_WEEK_RE = re.compile(r"^chirps_week_\d{4}_\d{2}_\d{2}_mm$")
ERA5_WEEK_RE = re.compile(r"^era5_([a-z0-9_]+)_week_\d{4}_\d{2}_\d{2}_(mean|max|min|sum)$")
CAMS_WEEK_RE = re.compile(r"^cams_([a-z0-9_]+)_week_\d{4}_\d{2}_\d{2}_(mean|max|min|sum)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot weekly factor values and threshold lines for road overlays.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--overlay-gpkg", type=Path, default=None)
    parser.add_argument("--thresholds-csv", type=Path, default=None)
    parser.add_argument("--thresholds-yaml", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--scenario", type=str, default="all", help="Scenario name, comma-separated names, or `all`.")
    parser.add_argument("--include-all-factors", action="store_true", help="Plot all numeric factor layers, not only threshold-configured factors.")
    parser.add_argument(
        "--keep-redundant-scenarios",
        action="store_true",
        help="Render scenario/surface combinations even when they select the same road mask.",
    )
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_path(raw: str | None, base: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else base / path


def _clear_generated_pngs(path: Path) -> None:
    if not path.exists():
        return
    for png_path in path.glob("*.png"):
        png_path.unlink()


def _load_summary(results_dir: Path) -> dict[str, object]:
    path = results_dir / "summary.json"
    if not path.exists():
        raise FileNotFoundError(f"Missing run summary: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _empty_thresholds() -> dict[str, float]:
    return {level: np.nan for level in THRESHOLD_LEVELS}


def _threshold_level_label(level: str) -> str:
    return THRESHOLD_LABELS.get(level, level.replace("_", " "))


def _threshold_unit_for_factor(factor: str) -> str:
    if factor == "visibility_weekly_min_m" or factor.endswith("_m"):
        return "m"
    if factor.endswith("_mm"):
        return "mm"
    if factor.endswith("_mm_per_h"):
        return "mm/h"
    if factor.endswith("_m_s"):
        return "m/s"
    if factor.endswith("_c") or _is_temperature_factor(factor):
        return "C"
    if "percentile" in factor:
        return "percentile"
    return ""


def _threshold_legend_label(level: str, factor: str, direction: str, value: float) -> str:
    op = ">=" if direction in {"gte", "gt"} else "<="
    unit = _threshold_unit_for_factor(factor)
    value_text = f"{value:g} {unit}".strip()
    return f"{_threshold_level_label(level)}: {op} {value_text}"


def _normalise_threshold_level(level: object) -> str:
    return LEGACY_THRESHOLD_LEVEL_ALIASES.get(str(level), str(level))


def _load_thresholds_csv(path: Path) -> dict[str, dict[str, object]]:
    frame = pd.read_csv(path)
    required = {"factor", "direction", "threshold", "threshold_value"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Threshold CSV is missing columns {sorted(missing)}: {path}")

    frame["threshold"] = frame["threshold"].map(_normalise_threshold_level)
    frame = frame.loc[frame["threshold"].isin(THRESHOLD_LEVELS)].copy()
    out: dict[str, dict[str, object]] = {}
    for factor, part in frame.groupby("factor", sort=True):
        directions = part["direction"].dropna().astype(str).str.lower().unique().tolist()
        if len(directions) != 1:
            raise ValueError(f"Factor `{factor}` must have exactly one direction in {path}")
        values = _empty_thresholds()
        for row in part.itertuples(index=False):
            values[str(row.threshold)] = float(row.threshold_value)
        out[str(factor)] = {
            "direction": directions[0],
            "thresholds": values,
            "effects": {},
            "surface_scope": "all",
            "effect_interpolation": "step",
        }
    return out


def _load_thresholds_yaml(path: Path) -> dict[str, dict[str, object]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = payload.get("road_hazard_thresholds", payload) if isinstance(payload, dict) else {}
    raw_rules = root.get("rules", []) if isinstance(root, dict) else []
    out: dict[str, dict[str, object]] = {}
    for item in raw_rules:
        if not isinstance(item, dict):
            continue
        factor = str(item.get("factor", "")).strip()
        if not factor:
            continue
        values = _empty_thresholds()
        for raw_level, raw_value in (item.get("thresholds") or {}).items():
            level = _normalise_threshold_level(raw_level)
            if level not in values or raw_value is None:
                continue
            try:
                values[level] = float(raw_value)
            except (TypeError, ValueError):
                values[level] = np.nan
        if not any(np.isfinite(value) for value in values.values()):
            continue
        direction = str(item.get("direction", "gte")).lower()
        effects = item.get("effects") if isinstance(item.get("effects"), dict) else {}
        surface_scope = str(item.get("surface_scope", "all")).strip().lower()
        interpolation = str(item.get("effect_interpolation", item.get("interpolation", "step"))).strip().lower()
        hazard = str(item.get("hazard", factor)).strip() or factor
        surface = str(item.get("surface", surface_scope)).strip() or surface_scope
        key = f"{surface_scope}__{hazard}__{factor}"
        out[key] = {
            "factor": factor,
            "direction": direction,
            "thresholds": values,
            "effects": effects,
            "surface_scope": surface_scope,
            "surface": surface,
            "hazard": hazard,
            "effect_interpolation": interpolation,
        }
    return out


def _numeric_external_factors(roads: gpd.GeoDataFrame, threshold_factors: set[str]) -> list[str]:
    factors: set[str] = set(threshold_factors)
    for col in roads.columns:
        if not pd.api.types.is_numeric_dtype(roads[col]):
            continue
        if FLOOD_WEEK_RE.match(col):
            factors.add("flood_weekly")
            continue
        if re.match(r"^flood_depth_week_\d{4}_\d{2}_\d{2}_max_m$", col):
            factors.add("flood_depth_weekly_max_m")
            continue
        if re.match(r"^visibility_week_\d{4}_\d{2}_\d{2}_min_m$", col):
            factors.add("visibility_weekly_min_m")
            continue
        if re.match(r"^pavement_surface_temperature_week_\d{4}_\d{2}_\d{2}_max_c$", col):
            factors.add("pavement_surface_temperature_weekly_max_c")
            continue
        if re.match(r"^soil_moisture_week_\d{4}_\d{2}_\d{2}_local_percentile$", col):
            factors.add("soil_moisture_weekly_local_percentile")
            continue
        if re.match(r"^unpaved_erosion_rainfall_week_\d{4}_\d{2}_\d{2}_local_percentile$", col):
            factors.add("unpaved_erosion_rainfall_weekly_local_percentile")
            continue
        if CHIRPS_WEEK_RE.match(col):
            factors.add("chirps_weekly_mm")
            continue
        match = ERA5_WEEK_RE.match(col)
        if match:
            factors.add(f"era5_{match.group(1)}_weekly_{match.group(2)}")
            continue
        match = CAMS_WEEK_RE.match(col)
        if match:
            factors.add(f"cams_{match.group(1)}_weekly_{match.group(2)}")
            continue
        if col.startswith(FACTOR_PREFIXES):
            factors.add(col)
    return sorted(factors)


def _parse_dates(summary: dict[str, object]) -> list[str]:
    period = dict(summary.get("period") or summary.get("analysis_period") or {})
    start = datetime.strptime(str(period["start_date"]), "%Y-%m-%d").date()
    end = datetime.strptime(str(period["end_date"]), "%Y-%m-%d").date()
    step_days = int(period.get("step_days") or period.get("aggregation_period_days"))
    weeks: list[str] = []
    cursor = start
    while cursor <= end:
        weeks.append(cursor.isoformat())
        cursor += timedelta(days=step_days)
    return weeks


def _scenario_names(value: str) -> list[str]:
    supported = ["actual_unpaved", "unknown_as_paved", "unknown_as_unpaved"]
    requested = [part.strip() for part in value.split(",") if part.strip()]
    if not requested or requested == ["all"]:
        return supported
    unknown = sorted(set(requested).difference(supported))
    if unknown:
        raise ValueError(f"Unsupported scenario(s): {', '.join(unknown)}")
    return requested


def _is_redundant_scenario(scenario: str, scenarios: list[str], surface_scope: str, keep_redundant: bool) -> bool:
    if keep_redundant or set(scenarios) != {"actual_unpaved", "unknown_as_paved", "unknown_as_unpaved"}:
        return False
    normalized = str(surface_scope or "all").strip().lower().replace("-", "_")
    if normalized in {"all", "any", "both", "*"}:
        return scenario != "actual_unpaved"
    if normalized in {"paved", "effective_paved"}:
        return scenario == "unknown_as_unpaved"
    if normalized in {"unpaved", "effective_unpaved"}:
        return scenario == "unknown_as_paved"
    if normalized.startswith("actual_"):
        return scenario != "actual_unpaved"
    return False


def _week_token(week_start: str) -> str:
    return week_start.replace("-", "_")


def _is_temperature_factor(factor: str) -> bool:
    return factor.startswith(TEMPERATURE_FACTOR_MARKERS)


def _pretty_factor_name(factor: str) -> str:
    label = FACTOR_LABELS.get(factor)
    if label is not None:
        return label
    return factor.replace("_", " ")


def _factor_time_note(factor: str) -> str:
    if factor in {"flood_weekly", "chirps_weekly_mm"}:
        return "weekly parameter"
    if "_weekly_" in factor:
        return "weekly parameter"
    if factor.startswith("era5_spi_"):
        return "monthly parameter"
    if factor.startswith(("landslide_", "gem_", "liquefaction_", "worldcover_", "soil_", "flopros_")):
        return "static layer"
    return ""


def _pretty_factor_title(factor: str) -> str:
    note = _factor_time_note(factor)
    label = _pretty_factor_name(factor)
    if note:
        return f"{label} ({note})"
    return label


def _pretty_scenario_name(scenario: str) -> str:
    labels = {
        "actual_unpaved": "Actual unpaved roads",
        "unknown_as_paved": "Unknown roads treated as paved",
        "unknown_as_unpaved": "Unknown roads treated as unpaved",
    }
    return labels.get(scenario, scenario.replace("_", " "))


def _factor_values(roads: gpd.GeoDataFrame, factor: str, week_start: str) -> tuple[pd.Series, str]:
    token = _week_token(week_start)
    if factor == "flood_weekly":
        weekly_col = f"flood_week_{token}"
        if weekly_col in roads.columns:
            return pd.to_numeric(roads[weekly_col], errors="coerce"), weekly_col
    if factor == "chirps_weekly_mm":
        col = f"chirps_week_{token}_mm"
        if col in roads.columns:
            return pd.to_numeric(roads[col], errors="coerce"), col
    special = {
        "flood_depth_weekly_max_m": f"flood_depth_week_{token}_max_m",
        "visibility_weekly_min_m": f"visibility_week_{token}_min_m",
        "pavement_surface_temperature_weekly_max_c": f"pavement_surface_temperature_week_{token}_max_c",
        "soil_moisture_weekly_local_percentile": f"soil_moisture_week_{token}_local_percentile",
        "unpaved_erosion_rainfall_weekly_local_percentile": f"unpaved_erosion_rainfall_week_{token}_local_percentile",
        "era5_tp_1h_max_weekly_mm_per_h": f"era5_tp_1h_max_week_{token}_mm_per_h",
        "era5_crosswind_10m_weekly_max_m_s": f"era5_crosswind_10m_week_{token}_max",
        "era5_wind_gust_weekly_max_m_s": f"era5_wind_gust_week_{token}_max",
        "era5_max_total_precip_rate_weekly_mm_per_h": f"era5_max_total_precip_rate_week_{token}_mm_per_h",
    }
    col = special.get(factor)
    if col is not None and col in roads.columns:
        return pd.to_numeric(roads[col], errors="coerce"), col
    if "_weekly_" in factor and (factor.startswith("era5_") or factor.startswith("cams_")):
        col = factor.replace("_weekly_", f"_week_{token}_", 1)
        if col in roads.columns:
            values = pd.to_numeric(roads[col], errors="coerce")
            if _is_temperature_factor(factor):
                values = values - 273.15
            return values, col
    if factor in roads.columns:
        values = pd.to_numeric(roads[factor], errors="coerce")
        if _is_temperature_factor(factor):
            values = values - 273.15
        return values, factor
    return pd.Series(np.nan, index=roads.index, dtype="float64"), "missing"


def _effective_unpaved_mask(roads: gpd.GeoDataFrame, scenario: str) -> pd.Series:
    surface = roads["surface_group"].astype("string").str.lower().fillna("unknown")
    if scenario == "actual_unpaved":
        effective = surface
    elif scenario == "unknown_as_paved":
        effective = surface.where(surface != "unknown", "paved")
    elif scenario == "unknown_as_unpaved":
        effective = surface.where(surface != "unknown", "unpaved")
    else:
        raise ValueError(f"Unsupported scenario: {scenario}")
    return effective == "unpaved"


def _effective_scope_mask(roads: gpd.GeoDataFrame, scenario: str, scope: str) -> pd.Series:
    normalized = str(scope or "all").strip().lower().replace("-", "_")
    surface = roads["surface_group"].astype("string").str.lower().fillna("unknown")
    if scenario == "actual_unpaved":
        effective = surface
    elif scenario == "unknown_as_paved":
        effective = surface.where(surface != "unknown", "paved")
    elif scenario == "unknown_as_unpaved":
        effective = surface.where(surface != "unknown", "unpaved")
    else:
        raise ValueError(f"Unsupported scenario: {scenario}")
    if normalized in {"all", "any", "both", "*"}:
        return pd.Series(True, index=roads.index)
    if normalized.startswith("actual_"):
        target = normalized.removeprefix("actual_")
        return surface == target
    if normalized.startswith("effective_"):
        normalized = normalized.removeprefix("effective_")
    return effective == normalized


def _stats(values: pd.Series) -> dict[str, float | int | None]:
    finite = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if finite.empty:
        return {
            "n_values": 0,
            "min": None,
            "p05": None,
            "q25": None,
            "median": None,
            "q75": None,
            "p95": None,
            "max": None,
        }
    quantiles = finite.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
    return {
        "n_values": int(finite.size),
        "min": float(finite.min()),
        "p05": float(quantiles.loc[0.05]),
        "q25": float(quantiles.loc[0.25]),
        "median": float(quantiles.loc[0.5]),
        "q75": float(quantiles.loc[0.75]),
        "p95": float(quantiles.loc[0.95]),
        "max": float(finite.max()),
    }


def _plot_factor(
    rows: pd.DataFrame,
    raw_values: dict[str, np.ndarray],
    factor: str,
    scenario: str,
    direction: str,
    thresholds: dict[str, float],
    out_path: Path,
) -> None:
    weeks = sorted(raw_values.keys())
    series = [raw_values[week] for week in weeks]

    fig, ax = plt.subplots(figsize=(12.0, 5.8))
    if any(len(vals) for vals in series):
        ax.boxplot(series, tick_labels=weeks, showfliers=False)
    else:
        ax.text(0.5, 0.5, "No finite road values", transform=ax.transAxes, ha="center", va="center")

    for level in THRESHOLD_LEVELS:
        value = thresholds[level]
        if not np.isfinite(value):
            continue
        color, linestyle = THRESHOLD_STYLES[level]
        ax.axhline(
            value,
            color=color,
            linestyle=linestyle,
            linewidth=1.4,
            label=_threshold_legend_label(level, factor, direction, value),
        )

    ax.set_title(f"{_pretty_scenario_name(scenario)} | {_pretty_factor_title(factor)}")
    ax.set_xlabel("Week start")
    if _is_temperature_factor(factor):
        ax.set_ylabel("Factor value on road section (C)")
    else:
        ax.set_ylabel("Factor value on road section")
    ax.grid(alpha=0.22)
    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def _threshold_class(values: pd.Series, direction: str, thresholds: dict[str, float]) -> pd.Series:
    out = pd.Series("none", index=values.index, dtype="object")
    finite = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    for level in THRESHOLD_LEVELS:
        value = thresholds.get(level, np.nan)
        if not np.isfinite(value):
            continue
        if direction == "gte":
            active = finite >= value
        else:
            active = finite <= value
        out.loc[active.fillna(False)] = level
    return out


def _effect_float(effects: dict[str, object], level: str, field: str) -> float | None:
    raw = effects.get(level)
    if not isinstance(raw, dict):
        return None
    value = raw.get(field)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def _continuous_effect_values(
    values: pd.Series,
    direction: str,
    thresholds: dict[str, float],
    effects: dict[str, object],
    field: str,
    interpolation: str,
) -> pd.Series:
    out = pd.Series(0.0, index=values.index, dtype="float64")
    if interpolation != "linear":
        return out
    anchors: list[tuple[float, float]] = []
    for level in THRESHOLD_LEVELS:
        threshold = thresholds.get(level, np.nan)
        effect = _effect_float(effects, level, field)
        if np.isfinite(threshold) and effect is not None:
            anchors.append((float(threshold), float(effect)))
    if not anchors:
        return out
    x = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    if direction in {"gte", "gt"}:
        pts = sorted(anchors)
    elif direction in {"lte", "lt"}:
        x = -x
        pts = sorted((-threshold, effect) for threshold, effect in anchors)
    else:
        return out
    xp = np.asarray([p[0] for p in pts], dtype=float)
    fp = np.asarray([p[1] for p in pts], dtype=float)
    finite = np.isfinite(x)
    arr = np.zeros(x.shape, dtype=float)
    arr[finite] = np.interp(x[finite], xp, fp, left=0.0, right=float(fp[-1]))
    return pd.Series(arr, index=values.index)


def _plot_factor_map(
    roads: gpd.GeoDataFrame,
    values: pd.Series,
    mask: pd.Series,
    factor: str,
    title_suffix: str,
    out_path: Path,
    *,
    thresholds: dict[str, float] | None = None,
    direction: str | None = None,
) -> None:
    subset = roads.loc[mask].copy()
    subset["_plot_value"] = pd.to_numeric(values.loc[mask], errors="coerce")

    fig, ax = plt.subplots(figsize=(9.0, 8.0))
    roads.boundary.plot(ax=ax, linewidth=0.10, color="#dddddd", alpha=0.35)
    finite = subset.loc[subset["_plot_value"].replace([np.inf, -np.inf], np.nan).notna()].copy()
    if finite.empty:
        ax.text(0.5, 0.5, "No finite road values", transform=ax.transAxes, ha="center", va="center")
    else:
        finite.plot(ax=ax, column="_plot_value", cmap="viridis", linewidth=0.55, legend=True)
    ax.set_title(f"{_pretty_factor_title(factor)} | {title_suffix}")
    ax.set_axis_off()
    if thresholds and direction:
        handles = []
        labels = []
        for level in THRESHOLD_LEVELS:
            value = thresholds.get(level, np.nan)
            if np.isfinite(value):
                handles.append(Line2D([0], [0], color="black", lw=1.6, linestyle=THRESHOLD_STYLES[level][1]))
                labels.append(_threshold_legend_label(level, factor, direction, value))
        if handles:
            ax.legend(handles, labels, loc="lower left", fontsize=8, frameon=True)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def _plot_threshold_class_map(
    roads: gpd.GeoDataFrame,
    values: pd.Series,
    mask: pd.Series,
    factor: str,
    direction: str,
    thresholds: dict[str, float],
    out_path: Path,
) -> None:
    palette = {
        "none": "#d9d9d9",
        "speed_reduction_1": "#fee08b",
        "speed_reduction_2": "#fdae61",
        "speed_reduction_3": "#f46d43",
        "catastrophic_temporary": "#a50026",
        "catastrophic_permanent": "#5c0011",
    }
    subset = roads.loc[mask].copy()
    subset["_threshold_class"] = _threshold_class(values.loc[mask], direction, thresholds)

    fig, ax = plt.subplots(figsize=(9.0, 8.0))
    roads.boundary.plot(ax=ax, linewidth=0.10, color="#eeeeee", alpha=0.35)
    for level, color in palette.items():
        part = subset.loc[subset["_threshold_class"] == level]
        if not part.empty:
            part.plot(ax=ax, color=color, linewidth=0.55, alpha=0.95)
    handles = [Line2D([0], [0], color=color, lw=2.5, label=_threshold_level_label(level)) for level, color in palette.items()]
    ax.legend(handles=handles, loc="lower left", fontsize=8, frameon=True)
    ax.set_title(f"{_pretty_factor_title(factor)} | highest triggered threshold ({direction})")
    ax.set_axis_off()
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    summary = _load_summary(args.results_dir)
    overlay_path = args.overlay_gpkg or _resolve_path(str(summary.get("overlay_source", "")), project_root)
    thresholds_path = args.thresholds_csv or _resolve_path(str(summary.get("thresholds_csv_path", "")), project_root)
    thresholds_yaml_path = args.thresholds_yaml or _resolve_path(str(summary.get("thresholds_yaml_path", "")), project_root)
    if overlay_path is None or not overlay_path.exists():
        raise FileNotFoundError(f"Missing overlay GPKG: {overlay_path}")
    if thresholds_path is not None and thresholds_path.exists():
        thresholds = _load_thresholds_csv(thresholds_path)
        threshold_source = thresholds_path
    elif thresholds_yaml_path is not None and thresholds_yaml_path.exists():
        thresholds = _load_thresholds_yaml(thresholds_yaml_path)
        threshold_source = thresholds_yaml_path
    else:
        raise FileNotFoundError(f"Missing thresholds CSV/YAML: {thresholds_path} / {thresholds_yaml_path}")

    out_dir = args.out_dir or (args.results_dir / "weekly_factor_value_boxplots")
    out_dir.mkdir(parents=True, exist_ok=True)
    _clear_generated_pngs(out_dir)
    map_dir = args.results_dir / "weekly_factor_value_maps"
    map_dir.mkdir(parents=True, exist_ok=True)
    _clear_generated_pngs(map_dir)

    roads = gpd.read_file(overlay_path)
    weeks = _parse_dates(summary)
    threshold_factors = {str(cfg.get("factor", key)) for key, cfg in thresholds.items()}
    if args.include_all_factors:
        factors = sorted(set(thresholds.keys()) | set(_numeric_external_factors(roads, threshold_factors)))
    else:
        factors = sorted(thresholds.keys())
    scenarios = _scenario_names(args.scenario)

    stat_rows: list[dict[str, object]] = []
    png_outputs: list[str] = []
    for scenario in scenarios:
        for factor_key in factors:
            cfg = thresholds.get(
                factor_key,
                {
                    "factor": factor_key,
                    "direction": "",
                    "thresholds": _empty_thresholds(),
                    "effects": {},
                    "surface_scope": "all",
                    "effect_interpolation": "step",
                },
            )
            factor_rows: list[dict[str, object]] = []
            raw_by_week: dict[str, np.ndarray] = {}
            factor = str(cfg.get("factor", factor_key))
            direction = str(cfg["direction"])
            factor_thresholds = dict(cfg["thresholds"])
            factor_effects = dict(cfg.get("effects") or {})
            surface_scope = str(cfg.get("surface_scope", "all"))
            if _is_redundant_scenario(scenario, scenarios, surface_scope, args.keep_redundant_scenarios):
                continue
            effect_interpolation = str(cfg.get("effect_interpolation", "step"))
            values_by_week: dict[str, pd.Series] = {}
            mask = _effective_scope_mask(roads, scenario, surface_scope)
            for week_start in weeks:
                values, value_source = _factor_values(roads, factor, week_start)
                values_by_week[week_start] = values
                selected = values.loc[mask]
                finite = selected.replace([np.inf, -np.inf], np.nan).dropna()
                speed_effect = _continuous_effect_values(
                    values,
                    direction,
                    factor_thresholds,
                    factor_effects,
                    "speed_penalty_fraction",
                    effect_interpolation,
                ).loc[mask]
                damage_effect = _continuous_effect_values(
                    values,
                    direction,
                    factor_thresholds,
                    factor_effects,
                    "damage_index_fraction",
                    effect_interpolation,
                ).loc[mask]
                raw_by_week[week_start] = finite.to_numpy(dtype="float64")
                row: dict[str, object] = {
                    "week_start": week_start,
                    "scenario": scenario,
                    "factor": factor_key,
                    "source_factor": factor,
                    "hazard": cfg.get("hazard"),
                    "surface": cfg.get("surface"),
                    "direction": direction,
                    "surface_scope": surface_scope,
                    "effect_interpolation": effect_interpolation,
                    "value_source": value_source,
                    "n_applicable_roads": int(mask.sum()),
                    "interpolated_speed_penalty_mean": float(speed_effect.mean()) if len(speed_effect) else 0.0,
                    "interpolated_speed_penalty_max": float(speed_effect.max()) if len(speed_effect) else 0.0,
                    "interpolated_damage_index_mean": float(damage_effect.mean()) if len(damage_effect) else 0.0,
                    "interpolated_damage_index_max": float(damage_effect.max()) if len(damage_effect) else 0.0,
                }
                row.update(_stats(selected))
                for level in THRESHOLD_LEVELS:
                    threshold_value = float(factor_thresholds.get(level, np.nan))
                    row[f"{level}_threshold"] = threshold_value
                    if direction in {"gte", "lte"} and np.isfinite(threshold_value):
                        if direction == "gte":
                            row[f"{level}_triggered"] = int((finite >= threshold_value).sum())
                        else:
                            row[f"{level}_triggered"] = int((finite <= threshold_value).sum())
                    else:
                        row[f"{level}_triggered"] = 0
                factor_rows.append(row)
                stat_rows.append(row)

            factor_frame = pd.DataFrame(factor_rows)
            safe_factor = factor_key.replace("/", "_").replace(" ", "_")
            out_path = out_dir / f"{scenario}__{safe_factor}.png"
            _plot_factor(
                factor_frame,
                raw_by_week,
                factor,
                scenario,
                direction,
                factor_thresholds,
                out_path,
            )
            png_outputs.append(str(out_path))
            first_week = weeks[0]
            last_week = weeks[-1]
            first_values = values_by_week[first_week]
            value_map = map_dir / f"{scenario}__{safe_factor}__{first_week}_values.png"
            _plot_factor_map(
                roads,
                first_values,
                mask,
                factor,
                f"values on {first_week}",
                value_map,
                thresholds=factor_thresholds if direction in {"gte", "lte"} else None,
                direction=direction if direction in {"gte", "lte"} else None,
            )
            png_outputs.append(str(value_map))

            stack = pd.concat(values_by_week, axis=1)
            variability = stack.max(axis=1, skipna=True) - stack.min(axis=1, skipna=True)
            variability_map = map_dir / f"{scenario}__{safe_factor}__weekly_range.png"
            _plot_factor_map(roads, variability, mask, factor, f"weekly range {first_week}..{last_week}", variability_map)
            png_outputs.append(str(variability_map))

            if direction in {"gte", "lte"}:
                class_map = map_dir / f"{scenario}__{safe_factor}__{first_week}_threshold_class.png"
                _plot_threshold_class_map(roads, first_values, mask, factor, direction, factor_thresholds, class_map)
                png_outputs.append(str(class_map))

    stats = pd.DataFrame(stat_rows)
    stats_path = args.results_dir / "weekly_factor_value_diagnostics.csv"
    stats.to_csv(stats_path, index=False)
    variability_rows: list[dict[str, object]] = []
    for (scenario, factor), part in stats.groupby(["scenario", "factor"], sort=True):
        variability_rows.append(
            {
                "scenario": scenario,
                "factor": factor,
                "n_weeks": int(part["week_start"].nunique()),
                "median_unique_values_over_weeks": int(part["median"].nunique(dropna=True)),
                "min_unique_values_over_weeks": int(part["min"].nunique(dropna=True)),
                "max_unique_values_over_weeks": int(part["max"].nunique(dropna=True)),
                "is_static_over_weeks": bool(
                    part["median"].nunique(dropna=True) <= 1
                    and part["min"].nunique(dropna=True) <= 1
                    and part["max"].nunique(dropna=True) <= 1
                ),
            }
        )
    variability_path = args.results_dir / "weekly_factor_variability_summary.csv"
    pd.DataFrame(variability_rows).to_csv(variability_path, index=False)
    manifest = {
        "overlay_gpkg": str(overlay_path),
        "thresholds_source": str(threshold_source),
        "diagnostics_csv": str(stats_path),
        "variability_csv": str(variability_path),
        "png_dir": str(out_dir),
        "map_png_dir": str(args.results_dir / "weekly_factor_value_maps"),
        "png_count": len(png_outputs),
        "png_outputs": png_outputs,
    }
    manifest_path = args.results_dir / "weekly_factor_value_diagnostics.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
