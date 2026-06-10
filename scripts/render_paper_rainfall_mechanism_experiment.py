#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from textwrap import fill

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import psycopg


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_SOURCE_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "visual_experiments"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "paper_experiment_rainfall_mechanism_v1"
DEFAULT_RAIN_SCENARIO = "unknown_as_unpaved"
DEFAULT_RAIN_SCOPE = "all"
DEFAULT_RAIN_FACTOR = "era5_tp_sum_weekly_mm"


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render rainfall-vs-burden mechanism checks from existing experiment outputs.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--rain-scenario", default=DEFAULT_RAIN_SCENARIO)
    parser.add_argument("--rain-scope", default=DEFAULT_RAIN_SCOPE)
    parser.add_argument("--rain-factor", default=DEFAULT_RAIN_FACTOR)
    parser.add_argument("--top-country-count", type=int, default=0)
    return parser.parse_args()


def load_source_inputs(source_dir: Path) -> dict[str, pd.DataFrame | dict]:
    cells = pd.read_csv(source_dir / "visual_experiment_cells.csv")
    dest_crop_points = pd.read_csv(source_dir / "visual_experiment_crop_points_cluster_weighted_by_dest.csv")
    manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
    cells["week_start"] = pd.to_datetime(cells["week_start"])
    return {"cells": cells, "dest_crop_points": dest_crop_points, "manifest": manifest}


def copy_source_artifacts(source_dir: Path, data_dir: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    for name in [
        "manifest.json",
        "visual_experiment_cells.csv",
        "visual_experiment_crop_points_cluster_weighted_by_dest.csv",
    ]:
        src = source_dir / name
        dst = data_dir / f"source_{name}"
        shutil.copy2(src, dst)
        copied[name] = str(dst)
    return copied


def fetch_country_weekly_rainfall(
    conn: psycopg.Connection,
    countries: list[str],
    scenario: str,
    surface_scope: str,
    factor: str,
) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT country_code, week_start, n_values, min_value, q25, median, q75, max_value
        FROM eq.boxplot_stats_weekly
        WHERE country_code = ANY(%(countries)s)
          AND scenario = %(scenario)s
          AND surface_scope = %(surface_scope)s
          AND factor = %(factor)s
        ORDER BY country_code, week_start
        """,
        conn,
        params={
            "countries": countries,
            "scenario": scenario,
            "surface_scope": surface_scope,
            "factor": factor,
        },
    )


def fetch_penalty_rules(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT road_type, min_weekly_mm, max_weekly_mm, speed_multiplier, effect_label
        FROM eq.weekly_rain_speed_penalty_rules
        WHERE min_weekly_mm > 0
        ORDER BY road_type, min_weekly_mm
        """,
        conn,
    )


def select_high_rain_contrast_weeks(subset: pd.DataFrame) -> dict[str, pd.Series]:
    if subset.empty:
        return {}
    rain_threshold = float(subset["median"].quantile(0.8))
    high_rain = subset[subset["median"] >= rain_threshold].copy()
    if high_rain.empty:
        high_rain = subset.copy()
    low_burden = high_rain.sort_values(["weekly_burden_h", "median", "week_start"], ascending=[True, False, True]).iloc[0]
    result = {
        "rain_threshold": pd.Series({"value": rain_threshold}),
        "high_rain_low_burden": low_burden,
    }
    high_burden = high_rain.sort_values(["weekly_burden_h", "median", "week_start"], ascending=[False, False, True]).iloc[0]
    same_week = pd.Timestamp(low_burden["week_start"]) == pd.Timestamp(high_burden["week_start"])
    same_values = float(low_burden["weekly_burden_h"]) == float(high_burden["weekly_burden_h"]) and float(low_burden["median"]) == float(high_burden["median"])
    if float(high_burden["weekly_burden_h"]) > 0.0 and not (same_week and same_values):
        result["high_rain_high_burden"] = high_burden
    return result


def build_country_rainfall_summary(weekly_rain: pd.DataFrame) -> pd.DataFrame:
    rain = weekly_rain.copy()
    rain["week_start"] = pd.to_datetime(rain["week_start"])
    summary = (
        rain.groupby("country_code", dropna=False)
        .agg(
            weeks=("week_start", "nunique"),
            total_weekly_median_mm=("median", "sum"),
            median_weekly_median_mm=("median", "median"),
            max_weekly_q75_mm=("q75", "max"),
            rainy_weeks_ge_75mm=("median", lambda s: int((s >= 75.0).sum())),
            extreme_weeks_ge_125mm_q75=("q75", lambda s: int((s >= 125.0).sum())),
        )
        .reset_index()
        .sort_values("total_weekly_median_mm", ascending=False)
        .reset_index(drop=True)
    )
    return summary


def build_country_burden_summary(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    raise NotImplementedError("Use build_country_burden_summary_from_weekly or build_country_burden_summary_from_destinations.")


def build_country_burden_summary_from_destinations(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    return (
        dest_crop_points.groupby("country_code", dropna=False)
        .agg(
            regime_count=("annual_severe_burden_h", "size"),
            total_affected_exposure=("affected_cluster_weight", "sum"),
            max_delay_h=("mean_affected_delay_h", "max"),
            median_affected_weeks=("affected_weeks", "median"),
        )
        .reset_index()
    )


def build_country_burden_summary_from_weekly(
    cells: pd.DataFrame,
    dest_crop_points: pd.DataFrame,
    country_codes: list[str],
) -> pd.DataFrame:
    burden = cells.copy()
    burden["severe_burden_h"] = (burden["median_delta_minutes"] / 60.0 - 3.0).clip(lower=0.0)
    weekly = (
        burden.groupby(["country_code", "week_start"], dropna=False)
        .agg(
            weekly_burden_h=("severe_burden_h", "sum"),
            mean_delay_affected_h=("median_delta_minutes", lambda s: float((s[s >= 180.0] / 60.0).mean() if (s >= 180.0).any() else 0.0)),
        )
        .reset_index()
    )
    weekly_summary = (
        weekly.groupby("country_code", dropna=False)
        .agg(
            total_burden_h=("weekly_burden_h", "sum"),
            max_delay_h=("mean_delay_affected_h", "max"),
            median_affected_weeks=("weekly_burden_h", lambda s: float((s > 0).sum())),
        )
        .reset_index()
    )
    exposure_summary = build_country_burden_summary_from_destinations(dest_crop_points)
    summary = pd.DataFrame({"country_code": sorted(country_codes)}).merge(weekly_summary, on="country_code", how="left").merge(
        exposure_summary[["country_code", "regime_count", "total_affected_exposure"]],
        on="country_code",
        how="left",
    )
    summary["regime_count"] = summary["regime_count"].fillna(0).astype(int)
    for col in ["total_burden_h", "total_affected_exposure", "max_delay_h", "median_affected_weeks"]:
        summary[col] = summary[col].fillna(0.0)
    return summary.sort_values("total_burden_h", ascending=False).reset_index(drop=True)


def combine_country_mechanism_summary(rainfall_summary: pd.DataFrame, burden_summary: pd.DataFrame) -> pd.DataFrame:
    combined = burden_summary.merge(rainfall_summary, on="country_code", how="inner")
    combined["rain_rank"] = combined["total_weekly_median_mm"].rank(method="dense", ascending=False).astype(int)
    combined["burden_rank"] = combined["total_burden_h"].rank(method="dense", ascending=False).astype(int)
    combined["rank_gap"] = combined["rain_rank"] - combined["burden_rank"]
    combined["burden_to_rain_ratio"] = combined["total_burden_h"] / combined["total_weekly_median_mm"].replace(0, np.nan)
    return combined.sort_values(["total_burden_h", "total_weekly_median_mm"], ascending=[False, False]).reset_index(drop=True)


def build_weekly_country_mechanism(cells: pd.DataFrame, weekly_rain: pd.DataFrame) -> pd.DataFrame:
    burden = cells.copy()
    burden["severe_burden_h"] = (burden["median_delta_minutes"] / 60.0 - 3.0).clip(lower=0.0)
    weekly_burden = (
        burden.groupby(["country_code", "week_start"], dropna=False)
        .agg(
            weekly_burden_h=("severe_burden_h", "sum"),
            affected_cells_ge_3h=("median_delta_minutes", lambda s: int((s >= 180.0).sum())),
            mean_delay_affected_h=("median_delta_minutes", lambda s: float((s[s >= 180.0] / 60.0).mean() if (s >= 180.0).any() else 0.0)),
        )
        .reset_index()
    )
    rain = weekly_rain.copy()
    rain["week_start"] = pd.to_datetime(rain["week_start"])
    return weekly_burden.merge(
        rain[["country_code", "week_start", "n_values", "min_value", "q25", "median", "q75", "max_value"]],
        on=["country_code", "week_start"],
        how="inner",
    )


def penalty_multiplier(value_mm: float, road_type: str, penalty_rules: pd.DataFrame) -> float:
    if not np.isfinite(value_mm):
        return np.nan
    rules = penalty_rules[penalty_rules["road_type"].eq(road_type)].sort_values("min_weekly_mm")
    for row in rules.itertuples(index=False):
        lower = float(row.min_weekly_mm)
        upper = None if pd.isna(row.max_weekly_mm) else float(row.max_weekly_mm)
        if value_mm >= lower and (upper is None or value_mm < upper):
            return float(row.speed_multiplier)
    return 1.0


def plot_rainfall_vs_burden(country_mechanism: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = country_mechanism.copy()
    max_exposure = float(plotted["total_affected_exposure"].max() or 1.0)
    plotted["size"] = 30.0 + 900.0 * np.sqrt(plotted["total_affected_exposure"].clip(lower=0.0) / max_exposure)
    fig, ax = plt.subplots(figsize=(13.5, 9.5))
    ax.scatter(
        plotted["total_weekly_median_mm"],
        plotted["total_burden_h"],
        s=plotted["size"],
        c=plotted["rank_gap"],
        cmap="coolwarm",
        alpha=0.84,
        edgecolor="#111111",
        linewidth=0.5,
    )
    for row in plotted.head(10).itertuples(index=False):
        ax.annotate(row.country_code, (row.total_weekly_median_mm, row.total_burden_h), xytext=(4, 4), textcoords="offset points", fontsize=8)
    ax.set_xlabel("Country rainfall severity: sum of weekly median precipitation, mm")
    ax.set_ylabel("Country accessibility burden: total severe burden, hours")
    ax.set_title("Rainfall alone does not fully explain accessibility burden")
    ax.grid(True, color="#e6e6e6", linewidth=0.8)
    corr = float(plotted["total_weekly_median_mm"].corr(plotted["total_burden_h"], method="spearman"))
    fig.text(
        0.08,
        0.04,
        fill(
            f"Each point is one country. X uses the sum of weekly median precipitation from the weekly boxplot table. "
            f"Y uses the accessibility severe burden already computed from the routing experiment. "
            f"Spearman correlation is {corr:.2f}; departures from the diagonal relationship support the claim that rainfall alone is not the whole explanation.",
            width=145,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.08, right=0.97, top=0.92, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "spearman_corr": corr, "points": int(len(plotted))}


def plot_rank_mismatch(country_mechanism: pd.DataFrame, out_path: Path, top_country_count: int) -> dict[str, object]:
    plotted = country_mechanism.nsmallest(top_country_count, "burden_rank").copy()
    plotted = plotted.sort_values("burden_rank")
    y = np.arange(len(plotted))
    fig, ax = plt.subplots(figsize=(12.5, 7.5))
    for idx, row in enumerate(plotted.itertuples(index=False)):
        ax.plot([row.rain_rank, row.burden_rank], [idx, idx], color="#777777", linewidth=1.2)
        ax.scatter(row.rain_rank, idx, color="#5dade2", s=70, edgecolor="#111111", linewidth=0.4, zorder=3)
        ax.scatter(row.burden_rank, idx, color="#d62828", s=70, edgecolor="#111111", linewidth=0.4, zorder=3)
    ax.set_yticks(y)
    ax.set_yticklabels(plotted["country_code"])
    ax.invert_yaxis()
    ax.set_xlabel("Rank (smaller = higher)")
    ax.set_title("Rank mismatch: high burden countries are not just the rainiest countries")
    ax.grid(axis="x", color="#e6e6e6", linewidth=0.8)
    handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#5dade2", markeredgecolor="#111111", label="rainfall rank"),
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#d62828", markeredgecolor="#111111", label="burden rank"),
    ]
    ax.legend(handles=handles, loc="lower right", frameon=True)
    fig.text(
        0.08,
        0.04,
        fill(
            "For the top burden countries, the blue point shows rainfall rank and the red point shows burden rank. "
            "Large gaps indicate that the burden ordering is not a simple rainfall ordering.",
            width=140,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.12, right=0.97, top=0.90, bottom=0.14)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "countries": plotted["country_code"].tolist()}


def plot_weekly_country_small_multiples(
    weekly_country: pd.DataFrame,
    penalty_rules: pd.DataFrame,
    out_path: Path,
    top_country_count: int,
) -> dict[str, object]:
    totals = weekly_country.groupby("country_code", dropna=False)["weekly_burden_h"].sum().sort_values(ascending=False)
    count = len(totals) if top_country_count <= 0 else min(int(top_country_count), len(totals))
    countries = totals.head(count).index.tolist()
    plotted = weekly_country[weekly_country["country_code"].isin(countries)].copy()
    threshold_values = sorted(
        {
            float(v)
            for v in penalty_rules["min_weekly_mm"].dropna().tolist()
            if float(v) > 0
        }
    )
    global_rain_max = max(
        float(plotted["median"].max() * 1.05) if not plotted.empty else 0.0,
        threshold_values[-1] if threshold_values else 0.0,
        320.0,
    )
    global_burden_max = max(float(plotted["weekly_burden_h"].max() * 1.05) if not plotted.empty else 0.0, 1.0)
    ncols = 4 if len(countries) > 12 else 2
    nrows = int(np.ceil(len(countries) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(18.0 if ncols == 4 else 14.0, 2.35 * nrows + 1.2), sharex=True)
    axes_flat = np.atleast_1d(axes).ravel()
    show_annotation_box = len(countries) <= 12
    for ax, iso in zip(axes_flat, countries):
        subset = plotted[plotted["country_code"].eq(iso)].sort_values("week_start")
        weeks = list(subset["week_start"])
        x = np.arange(len(weeks))
        contrast = select_high_rain_contrast_weeks(subset)
        bands = [0.0] + threshold_values + [global_rain_max]
        band_colors = ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c"]
        for i in range(len(bands) - 1):
            ax.axhspan(bands[i], bands[i + 1], color=band_colors[min(i, len(band_colors) - 1)], alpha=0.35, zorder=0)
        for y in threshold_values:
            ax.axhline(y, color="#4d4d4d", linewidth=0.7, linestyle="--", alpha=0.65, zorder=1)
        ax.plot(x, subset["median"], color="#08519c", linewidth=1.8, zorder=2)
        ax.set_xlim(-0.5, len(weeks) - 0.5)
        ax.set_xticks(np.arange(len(weeks)))
        labels = [week.strftime("%b %d") if (i == 0 or week.month != weeks[i - 1].month) else "" for i, week in enumerate(weeks)]
        ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7)
        ax.set_ylim(0, global_rain_max)
        ax.set_ylabel("mm/week", fontsize=8)
        ax2 = ax.twinx()
        ax2.plot(x, subset["weekly_burden_h"], color="#7f0000", linewidth=1.6, zorder=3)
        ax2.set_ylim(0, global_burden_max)
        marker_specs = [
            ("high_rain_low_burden", "#2ca25f", "rain high, burden low"),
            ("high_rain_high_burden", "#f16913", "critical week"),
        ]
        annotation_lines = []
        for key, color, label in marker_specs:
            row = contrast.get(key)
            if row is None:
                continue
            week_idx = next((i for i, week in enumerate(weeks) if week == row["week_start"]), None)
            if week_idx is None:
                continue
            ax.axvline(week_idx, color=color, linewidth=0.9, linestyle=":", alpha=0.8, zorder=1)
            annotation_lines.append(f"{label}: {row['week_start'].strftime('%b %d')}")
        ax.set_title(iso, fontsize=9 if ncols == 4 else 10)
        if annotation_lines and show_annotation_box:
            ax.text(
                0.01,
                0.98,
                "\n".join(annotation_lines),
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=7,
                bbox={"facecolor": "white", "alpha": 0.72, "edgecolor": "#d9d9d9", "boxstyle": "round,pad=0.25"},
            )
        ax.grid(True, color="#eeeeee", linewidth=0.7)
        ax.tick_params(axis="y", labelsize=6 if ncols == 4 else 7)
        ax2.tick_params(axis="y", labelsize=6 if ncols == 4 else 7)
    for ax in axes_flat[len(countries):]:
        ax.set_axis_off()
    fig.suptitle("Weekly rainfall and burden across all countries", y=0.985, fontsize=12)
    fig.text(
        0.07,
        0.04,
        "Blue = weekly median precipitation. Background shading follows the precipitation thresholds used by the project penalty rules. Dark red = weekly severe accessibility burden. Green and orange vertical markers flag selected contrast weeks rather than line maxima.",
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.05, right=0.95, top=0.93, bottom=0.09, hspace=0.50 if ncols == 4 else 0.42, wspace=0.28)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "countries": countries}


def main() -> None:
    args = parse_args()
    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)
    data_dir = out_dir / "data"
    figures_dir = out_dir / "figures"
    data_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    loaded = load_source_inputs(source_dir)
    copied = copy_source_artifacts(source_dir, data_dir)
    cells = loaded["cells"]
    dest_crop_points = loaded["dest_crop_points"]
    countries = sorted(set(cells["country_code"].dropna().unique().tolist()) | set(dest_crop_points["country_code"].dropna().unique().tolist()))

    with psycopg.connect(args.db_url) as conn:
        weekly_rain = fetch_country_weekly_rainfall(conn, countries, args.rain_scenario, args.rain_scope, args.rain_factor)
        penalty_rules = fetch_penalty_rules(conn)

    weekly_rain_csv = data_dir / "source_country_weekly_rainfall.csv"
    penalty_rules_csv = data_dir / "source_weekly_rain_speed_penalty_rules.csv"
    weekly_rain.to_csv(weekly_rain_csv, index=False)
    penalty_rules.to_csv(penalty_rules_csv, index=False)
    rainfall_summary = build_country_rainfall_summary(weekly_rain)
    burden_summary = build_country_burden_summary_from_weekly(cells, dest_crop_points, countries)
    country_mechanism = combine_country_mechanism_summary(rainfall_summary, burden_summary)
    weekly_country = build_weekly_country_mechanism(cells, weekly_rain)

    rainfall_summary_csv = data_dir / "country_rainfall_summary.csv"
    burden_summary_csv = data_dir / "country_burden_summary.csv"
    country_mechanism_csv = data_dir / "country_mechanism_summary.csv"
    weekly_country_csv = data_dir / "weekly_country_mechanism.csv"
    rainfall_summary.to_csv(rainfall_summary_csv, index=False)
    burden_summary.to_csv(burden_summary_csv, index=False)
    country_mechanism.to_csv(country_mechanism_csv, index=False)
    weekly_country.to_csv(weekly_country_csv, index=False)

    plots = [
        plot_rainfall_vs_burden(country_mechanism, figures_dir / "01_rainfall_vs_burden_scatter.png"),
        plot_rank_mismatch(country_mechanism, figures_dir / "02_rank_mismatch_top_burden_countries.png", args.top_country_count),
        plot_weekly_country_small_multiples(
            weekly_country,
            penalty_rules,
            figures_dir / "03_weekly_rain_vs_burden_top_countries.png",
            args.top_country_count,
        ),
    ]

    manifest = {
        "source_dir": str(source_dir),
        "out_dir": str(out_dir),
        "rain_scenario": args.rain_scenario,
        "rain_scope": args.rain_scope,
        "rain_factor": args.rain_factor,
        "copied_source_artifacts": copied,
        "derived_tables": {
            "weekly_rain_csv": str(weekly_rain_csv),
            "penalty_rules_csv": str(penalty_rules_csv),
            "rainfall_summary_csv": str(rainfall_summary_csv),
            "burden_summary_csv": str(burden_summary_csv),
            "country_mechanism_csv": str(country_mechanism_csv),
            "weekly_country_csv": str(weekly_country_csv),
        },
        "plots": plots,
        "headline_numbers": {
            "country_count": int(len(country_mechanism)),
            "spearman_rain_vs_burden": float(country_mechanism["total_weekly_median_mm"].corr(country_mechanism["total_burden_h"], method="spearman")),
            "top_burden_country": str(country_mechanism.iloc[0]["country_code"]) if not country_mechanism.empty else None,
            "largest_positive_rank_gap_country": str(country_mechanism.sort_values("rank_gap", ascending=False).iloc[0]["country_code"]) if not country_mechanism.empty else None,
            "largest_negative_rank_gap_country": str(country_mechanism.sort_values("rank_gap", ascending=True).iloc[0]["country_code"]) if not country_mechanism.empty else None,
        },
    }
    (out_dir / "experiment_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log(f"[done] source_dir={source_dir}")
    log(f"[done] out_dir={out_dir}")
    for plot in plots:
        log(f"[plot] {plot['path']}")


if __name__ == "__main__":
    main()
