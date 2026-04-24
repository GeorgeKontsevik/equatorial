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


matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


THRESHOLD_LEVELS = ("minor", "moderate", "severe", "catastrophic")
FACTOR_PREFIXES = (
    "chirps_",
    "flood_",
    "landslide_",
    "gem_",
    "liquefaction_",
    "worldcover_",
    "soil_",
    "era5_",
    "cams_",
    "flopros_",
)
THRESHOLD_STYLES = {
    "minor": ("#3b7ddd", "--"),
    "moderate": ("#e0a100", "--"),
    "severe": ("#d65f00", "-."),
    "catastrophic": ("#b00020", "-"),
}
TEMPERATURE_FACTOR_MARKERS = ("era5_t2m_", "era5_skt_")
FACTOR_LABELS = {
    "flood_weekly": "Flood depth",
    "chirps_weekly_mm": "Weekly rainfall",
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
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--scenario", type=str, default="all", help="Scenario name, comma-separated names, or `all`.")
    parser.add_argument("--include-all-factors", action="store_true", help="Plot all numeric factor layers, not only threshold-configured factors.")
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


def _load_thresholds(path: Path) -> dict[str, dict[str, object]]:
    frame = pd.read_csv(path)
    required = {"factor", "direction", "threshold", "threshold_value"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"Threshold CSV is missing columns {sorted(missing)}: {path}")

    frame = frame.loc[frame["threshold"].isin(THRESHOLD_LEVELS)].copy()
    out: dict[str, dict[str, object]] = {}
    for factor, part in frame.groupby("factor", sort=True):
        directions = part["direction"].dropna().astype(str).str.lower().unique().tolist()
        if len(directions) != 1:
            raise ValueError(f"Factor `{factor}` must have exactly one direction in {path}")
        values = {level: np.nan for level in THRESHOLD_LEVELS}
        for row in part.itertuples(index=False):
            values[str(row.threshold)] = float(row.threshold_value)
        out[str(factor)] = {"direction": directions[0], "thresholds": values}
    return out


def _numeric_external_factors(roads: gpd.GeoDataFrame, threshold_factors: set[str]) -> list[str]:
    factors: set[str] = set(threshold_factors)
    for col in roads.columns:
        if not pd.api.types.is_numeric_dtype(roads[col]):
            continue
        if FLOOD_WEEK_RE.match(col):
            factors.add("flood_weekly")
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
    period = dict(summary["period"])
    start = datetime.strptime(str(period["start_date"]), "%Y-%m-%d").date()
    end = datetime.strptime(str(period["end_date"]), "%Y-%m-%d").date()
    step_days = int(period["step_days"])
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
        ax.axhline(value, color=color, linestyle=linestyle, linewidth=1.4, label=f"{level}: {value:g}")

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
                labels.append(f"{level} {direction} {value:g}")
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
        "minor": "#fee08b",
        "moderate": "#fdae61",
        "severe": "#f46d43",
        "catastrophic": "#a50026",
    }
    subset = roads.loc[mask].copy()
    subset["_threshold_class"] = _threshold_class(values.loc[mask], direction, thresholds)

    fig, ax = plt.subplots(figsize=(9.0, 8.0))
    roads.boundary.plot(ax=ax, linewidth=0.10, color="#eeeeee", alpha=0.35)
    for level, color in palette.items():
        part = subset.loc[subset["_threshold_class"] == level]
        if not part.empty:
            part.plot(ax=ax, color=color, linewidth=0.55, alpha=0.95)
    handles = [Line2D([0], [0], color=color, lw=2.5, label=level) for level, color in palette.items()]
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
    if overlay_path is None or not overlay_path.exists():
        raise FileNotFoundError(f"Missing overlay GPKG: {overlay_path}")
    if thresholds_path is None or not thresholds_path.exists():
        raise FileNotFoundError(f"Missing thresholds CSV: {thresholds_path}")

    out_dir = args.out_dir or (args.results_dir / "weekly_factor_value_boxplots")
    out_dir.mkdir(parents=True, exist_ok=True)
    _clear_generated_pngs(out_dir)
    map_dir = args.results_dir / "weekly_factor_value_maps"
    map_dir.mkdir(parents=True, exist_ok=True)
    _clear_generated_pngs(map_dir)

    roads = gpd.read_file(overlay_path)
    thresholds = _load_thresholds(thresholds_path)
    weeks = _parse_dates(summary)
    threshold_factors = set(thresholds.keys())
    if args.include_all_factors:
        factors = _numeric_external_factors(roads, threshold_factors)
    else:
        factors = sorted(threshold_factors)
    scenarios = _scenario_names(args.scenario)

    stat_rows: list[dict[str, object]] = []
    png_outputs: list[str] = []
    for scenario in scenarios:
        mask = _effective_unpaved_mask(roads, scenario)
        for factor in factors:
            cfg = thresholds.get(
                factor,
                {
                    "direction": "",
                    "thresholds": {level: np.nan for level in THRESHOLD_LEVELS},
                },
            )
            factor_rows: list[dict[str, object]] = []
            raw_by_week: dict[str, np.ndarray] = {}
            direction = str(cfg["direction"])
            factor_thresholds = dict(cfg["thresholds"])
            values_by_week: dict[str, pd.Series] = {}
            for week_start in weeks:
                values, value_source = _factor_values(roads, factor, week_start)
                values_by_week[week_start] = values
                selected = values.loc[mask]
                finite = selected.replace([np.inf, -np.inf], np.nan).dropna()
                raw_by_week[week_start] = finite.to_numpy(dtype="float64")
                row: dict[str, object] = {
                    "week_start": week_start,
                    "scenario": scenario,
                    "factor": factor,
                    "direction": direction,
                    "value_source": value_source,
                    "n_effective_unpaved_roads": int(mask.sum()),
                }
                row.update(_stats(selected))
                for level in THRESHOLD_LEVELS:
                    threshold_value = float(factor_thresholds[level])
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
            safe_factor = factor.replace("/", "_").replace(" ", "_")
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
        "thresholds_csv": str(thresholds_path),
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
