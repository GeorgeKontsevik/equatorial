"""Render crop-type accessibility summaries and origin maps."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from src.data.run_road_monthly_scenarios import _country_layers
from src.data.run_weekly_accessibility_pandana import _round_output_frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot crop-type weekly accessibility summaries.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--country-code", type=str, default="GAB")
    parser.add_argument("--overlay-gpkg", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve(path: Path, project_root: Path) -> Path:
    return path if path.is_absolute() else project_root / path


def _read_inputs(results_dir: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame, pd.DataFrame]:
    origins_path = results_dir / "origins_used.gpkg"
    cities_path = results_dir / "cities_used.gpkg"
    baseline_path = results_dir / "baseline_routes.csv"
    weekly_path = results_dir / "weekly_accessibility.csv"
    for path in [origins_path, cities_path, baseline_path, weekly_path]:
        if not path.exists():
            raise FileNotFoundError(f"Missing required result artifact: {path}")
    origins = gpd.read_file(origins_path)
    cities = gpd.read_file(cities_path)
    baseline = pd.read_csv(baseline_path)
    weekly = pd.read_csv(weekly_path)
    return origins, cities, baseline, weekly


def _crop_stats(origins: gpd.GeoDataFrame, baseline: pd.DataFrame, weekly: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    origin_cols = ["origin_id", "crop_code", "crop_name", "crop_rank", "source_crop_rank", "harvested_area_index"]
    available_cols = [col for col in origin_cols if col in origins.columns]
    origin_meta = pd.DataFrame(origins.drop(columns="geometry"))[available_cols].copy()
    base = baseline.rename(columns={"access_minutes": "baseline_access_minutes", "connected": "baseline_connected"})
    rows = weekly.merge(base[["origin_id", "baseline_access_minutes", "baseline_connected"]], on="origin_id", how="left")
    rows = rows.merge(origin_meta, on="origin_id", how="left")
    rows["delta_minutes"] = rows["access_minutes"] - rows["baseline_access_minutes"]
    rows["became_disconnected"] = rows["baseline_connected"].astype(bool) & ~rows["connected"].astype(bool)

    weekly_stats = (
        rows.groupby(["scenario", "week_start", "crop_code", "crop_name"], dropna=False)
        .agg(
            n_origins=("origin_id", "count"),
            connected_share=("connected", "mean"),
            baseline_median_minutes=("baseline_access_minutes", "median"),
            median_access_minutes=("access_minutes", "median"),
            mean_access_minutes=("access_minutes", "mean"),
            median_delta_minutes=("delta_minutes", "median"),
            mean_delta_minutes=("delta_minutes", "mean"),
            p90_delta_minutes=("delta_minutes", lambda s: float(s.quantile(0.9))),
            max_delta_minutes=("delta_minutes", "max"),
            n_became_disconnected=("became_disconnected", "sum"),
        )
        .reset_index()
        .sort_values(["scenario", "crop_code", "week_start"])
    )
    overall_stats = (
        weekly_stats.groupby(["scenario", "crop_code", "crop_name"], dropna=False)
        .agg(
            n_origins=("n_origins", "max"),
            min_connected_share=("connected_share", "min"),
            max_median_access_minutes=("median_access_minutes", "max"),
            max_median_delta_minutes=("median_delta_minutes", "max"),
            max_origin_delta_minutes=("max_delta_minutes", "max"),
            total_became_disconnected=("n_became_disconnected", "sum"),
        )
        .reset_index()
        .sort_values(["scenario", "crop_code"])
    )
    origin_summary = (
        rows.groupby(["scenario", "origin_id", "crop_code", "crop_name", "crop_rank"], dropna=False)
        .agg(
            baseline_access_minutes=("baseline_access_minutes", "first"),
            max_access_minutes=("access_minutes", "max"),
            max_delta_minutes=("delta_minutes", "max"),
            median_delta_minutes=("delta_minutes", "median"),
            min_connected=("connected", "min"),
            n_weeks_disconnected=("connected", lambda s: int((~s.astype(bool)).sum())),
        )
        .reset_index()
        .sort_values(["scenario", "crop_code", "crop_rank"])
    )
    return rows, weekly_stats, overall_stats, origin_summary


def _plot_roads(ax: plt.Axes, roads: gpd.GeoDataFrame | None) -> None:
    if roads is not None and not roads.empty:
        roads.plot(ax=ax, color="#d0d0d0", linewidth=0.12, alpha=0.45, zorder=1)


def _plot_origin_map(
    country: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame | None,
    cities: gpd.GeoDataFrame,
    origins: gpd.GeoDataFrame,
    values: pd.DataFrame,
    out_path: Path,
    *,
    title: str,
    value_col: str,
    label_col: str,
) -> None:
    plot_origins = origins.merge(values, on="origin_id", how="left")
    target_crs = country.crs
    if roads is not None:
        roads = roads.to_crs(target_crs)
    cities = cities.to_crs(target_crs)
    plot_origins = plot_origins.to_crs(target_crs)

    vals = pd.to_numeric(plot_origins[value_col], errors="coerce")
    vmax = float(vals.max()) if vals.notna().any() else 1.0
    vmax = 1.0 if not np.isfinite(vmax) or vmax <= 0 else vmax
    fig, ax = plt.subplots(figsize=(9.5, 8.8))
    country.boundary.plot(ax=ax, color="#222222", linewidth=1.1, zorder=2)
    _plot_roads(ax, roads)
    cities.plot(ax=ax, color="#111111", marker="s", markersize=34, edgecolor="white", linewidth=0.4, zorder=5)
    plot_origins.plot(
        ax=ax,
        column=value_col,
        cmap="viridis",
        vmin=0,
        vmax=vmax,
        markersize=78,
        edgecolor="white",
        linewidth=0.8,
        legend=True,
        legend_kwds={"label": label_col, "shrink": 0.72},
        zorder=6,
    )
    for row in plot_origins.itertuples():
        ax.text(row.geometry.x, row.geometry.y, f"{row.crop_code}{int(row.crop_rank)}", fontsize=7, ha="left", va="bottom", zorder=7)
    ax.set_title(title, fontsize=12)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _plot_crop_lines(stats: pd.DataFrame, out_path: Path, *, metric: str, ylabel: str, title: str) -> None:
    scenarios = stats["scenario"].drop_duplicates().tolist()
    fig, axes = plt.subplots(len(scenarios), 1, figsize=(11.5, 4.0 * len(scenarios)), sharex=True)
    if len(scenarios) == 1:
        axes = [axes]
    for ax, scenario in zip(axes, scenarios, strict=False):
        subset = stats.loc[stats["scenario"].eq(scenario)].copy()
        for crop in sorted(subset["crop_code"].dropna().unique()):
            part = subset.loc[subset["crop_code"].eq(crop)].sort_values("week_start")
            ax.plot(part["week_start"], part[metric], marker="o", linewidth=1.6, label=crop)
        ax.set_title(scenario)
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.25)
        ax.legend(ncol=3, fontsize=8)
    axes[-1].set_xlabel("Week start")
    fig.autofmt_xdate(rotation=45)
    fig.suptitle(title, y=0.995)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    results_dir = _resolve(args.results_dir, project_root)
    out_dir = _resolve(args.out_dir, project_root) if args.out_dir is not None else results_dir / "crop_type_maps"
    out_dir.mkdir(parents=True, exist_ok=True)

    origins, cities, baseline, weekly = _read_inputs(results_dir)
    rows, weekly_stats, overall_stats, origin_summary = _crop_stats(origins, baseline, weekly)
    _round_output_frame(rows).to_csv(out_dir / "crop_origin_weekly_accessibility.csv", index=False)
    _round_output_frame(weekly_stats).to_csv(out_dir / "crop_weekly_access_stats.csv", index=False)
    _round_output_frame(overall_stats).to_csv(out_dir / "crop_overall_access_stats.csv", index=False)
    _round_output_frame(origin_summary).to_csv(out_dir / "crop_origin_overall_stats.csv", index=False)

    country, _ = _country_layers(project_root, args.country_code.upper())
    country = country.to_crs("EPSG:4326")
    roads = None
    if args.overlay_gpkg is not None:
        overlay_path = _resolve(args.overlay_gpkg, project_root)
        if overlay_path.exists():
            roads = gpd.read_file(overlay_path)

    base_values = origins[["origin_id"]].merge(
        baseline[["origin_id", "access_minutes"]].rename(columns={"access_minutes": "baseline_access_minutes"}),
        on="origin_id",
        how="left",
    )
    _plot_origin_map(
        country,
        roads,
        cities,
        origins,
        base_values,
        out_dir / "crop_origin_baseline_access_map.png",
        title="Crop Origins: Baseline Access Minutes",
        value_col="baseline_access_minutes",
        label_col="baseline access minutes",
    )

    _plot_crop_lines(
        weekly_stats,
        out_dir / "crop_weekly_median_delta_minutes.png",
        metric="median_delta_minutes",
        ylabel="median delta minutes",
        title="Crop-Type Median Access Change",
    )
    _plot_crop_lines(
        weekly_stats,
        out_dir / "crop_weekly_connected_share.png",
        metric="connected_share",
        ylabel="connected share",
        title="Crop-Type Connected Share",
    )

    map_outputs = ["crop_origin_baseline_access_map.png", "crop_weekly_median_delta_minutes.png", "crop_weekly_connected_share.png"]
    for scenario in sorted(weekly["scenario"].dropna().unique()):
        scen_rows = rows.loc[rows["scenario"].eq(scenario)].copy()
        max_values = (
            scen_rows.groupby("origin_id", as_index=False)["delta_minutes"]
            .max()
            .rename(columns={"delta_minutes": "max_delta_minutes"})
        )
        max_path = out_dir / f"crop_origin_max_delta_map__{scenario}.png"
        _plot_origin_map(
            country,
            roads,
            cities,
            origins,
            max_values,
            max_path,
            title=f"Crop Origins: Max Access Delta ({scenario})",
            value_col="max_delta_minutes",
            label_col="max delta minutes",
        )
        map_outputs.append(max_path.name)

        worst = (
            weekly_stats.loc[weekly_stats["scenario"].eq(scenario)]
            .groupby("week_start", as_index=False)["median_delta_minutes"]
            .median()
            .sort_values("median_delta_minutes", ascending=False)
            .head(1)
        )
        if not worst.empty:
            week = str(worst.iloc[0]["week_start"])
            week_values = scen_rows.loc[scen_rows["week_start"].eq(week), ["origin_id", "delta_minutes"]].rename(
                columns={"delta_minutes": "week_delta_minutes"}
            )
            week_path = out_dir / f"crop_origin_delta_map__{scenario}__{week}.png"
            _plot_origin_map(
                country,
                roads,
                cities,
                origins,
                week_values,
                week_path,
                title=f"Crop Origins: Access Delta {week} ({scenario})",
                value_col="week_delta_minutes",
                label_col="delta minutes",
            )
            map_outputs.append(week_path.name)

    report = {
        "results_dir": str(results_dir.relative_to(project_root)),
        "out_dir": str(out_dir.relative_to(project_root)),
        "csv_outputs": [
            str((out_dir / "crop_origin_weekly_accessibility.csv").relative_to(project_root)),
            str((out_dir / "crop_weekly_access_stats.csv").relative_to(project_root)),
            str((out_dir / "crop_overall_access_stats.csv").relative_to(project_root)),
            str((out_dir / "crop_origin_overall_stats.csv").relative_to(project_root)),
        ],
        "png_outputs": [str((out_dir / name).relative_to(project_root)) for name in map_outputs],
    }
    (out_dir / "crop_access_plot_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
