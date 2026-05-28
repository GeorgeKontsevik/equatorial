#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from textwrap import fill

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap, LogNorm
from matplotlib.gridspec import GridSpec
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import psycopg

from render_weekly_astar_accessibility_heatmaps import (
    CROP_ORDER,
    DAMAGE_CLASS_BINS_MIN,
    DAMAGE_CLASS_COLORS,
    DAMAGE_CLASS_LABELS,
    DEFAULT_DB_URL,
    crop_order,
    week_labels,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ORIGIN_SCOPE = "cluster_connected_allclusters_10small_3large_3ports_3airports"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "visual_experiments"
DEST_TYPE_LABELS = {
    "city_5_100k": "small city",
    "city_100k_plus": "large city",
    "port": "port",
    "airport": "airport",
}
DEST_TYPE_FACET_LABELS = {
    "city_5_100k": "10 small cities",
    "city_100k_plus": "3 large cities",
    "port": "3 ports",
    "airport": "3 airports",
}
CROP_COLORS = {
    "avocado": "#38c7d8",
    "banana": "#ffe100",
    "mango": "#ff2b67",
    "pineapple": "#6ee600",
    "plantain": "#9c31ff",
}
PRECIP_TOP_LABEL_COUNTRIES = ["COL", "PNG", "MYS", "BRN", "LBR"]


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render exploratory visual summaries for weekly accessibility damage.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default="weekly_sum_penalty_v1")
    parser.add_argument("--origin-scope", default=DEFAULT_ORIGIN_SCOPE)
    parser.add_argument("--min-weeks", type=int, default=53)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def fetch_rows(conn: psycopg.Connection, scenario: str, origin_scope: str, min_weeks: int) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        WITH loaded AS (
            SELECT country_code
            FROM eq.crop_accessibility_weekly_astar
            WHERE scenario = %(scenario)s
              AND origin_scope = %(origin_scope)s
            GROUP BY country_code
            HAVING count(DISTINCT week_start) >= %(min_weeks)s
        ),
        base AS (
            SELECT
                r.country_code,
                r.week_start,
                r.crop_code,
                r.candidate_rank,
                r.cluster_cell_count,
                r.harvested_area,
                r.cluster_share,
                r.dest_type,
                r.dest_rank,
                r.dest_id,
                r.route_status,
                r.travel_time_h,
                concat_ws(
                    '|',
                    r.country_code,
                    r.crop_code,
                    r.candidate_rank,
                    r.dest_type,
                    r.dest_rank,
                    r.dest_id
                ) AS od_key
            FROM eq.crop_accessibility_weekly_astar r
            JOIN loaded l ON l.country_code = r.country_code
            WHERE r.scenario = %(scenario)s
              AND r.origin_scope = %(origin_scope)s
        ),
        baseline AS (
            SELECT od_key, min(travel_time_h) AS baseline_h
            FROM base
            WHERE route_status = 'ok'
              AND travel_time_h IS NOT NULL
              AND travel_time_h > 0
            GROUP BY od_key
        )
        SELECT
            b.country_code,
            b.week_start,
            b.crop_code,
            b.candidate_rank,
            b.cluster_cell_count,
            b.harvested_area,
            b.cluster_share,
            b.dest_type,
            bl.baseline_h,
            (b.travel_time_h - bl.baseline_h) * 60.0 AS delta_minutes
        FROM base b
        JOIN baseline bl ON b.od_key = bl.od_key
        WHERE b.route_status = 'ok'
          AND b.travel_time_h IS NOT NULL
          AND b.travel_time_h > 0
        ORDER BY b.country_code, b.week_start, b.crop_code, b.dest_type
        """,
        conn,
        params={"scenario": scenario, "origin_scope": origin_scope, "min_weeks": min_weeks},
    )


def damage_class(value: float) -> int:
    if not np.isfinite(value):
        return -1
    return int(np.digitize([value], DAMAGE_CLASS_BINS_MIN, right=False)[0])


def add_common_time_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["week_start"] = pd.to_datetime(frame["week_start"])
    return frame


def summarize_cells(frame: pd.DataFrame) -> pd.DataFrame:
    cells = (
        frame.groupby(["country_code", "week_start", "crop_code", "dest_type"], dropna=False)
        .agg(
            median_delta_minutes=("delta_minutes", "median"),
            max_delta_minutes=("delta_minutes", "max"),
            median_baseline_h=("baseline_h", "median"),
            od_rows=("delta_minutes", "size"),
        )
        .reset_index()
    )
    cells["damage_class"] = cells["median_delta_minutes"].map(damage_class)
    return cells


def ordered_countries(cells: pd.DataFrame) -> list[str]:
    ranking = (
        cells.groupby("country_code")
        .agg(
            cells_ge_6h=("median_delta_minutes", lambda s: int((s >= 360.0).sum())),
            cells_ge_3h=("median_delta_minutes", lambda s: int((s >= 180.0).sum())),
            max_median_delta=("median_delta_minutes", "max"),
        )
        .reset_index()
        .sort_values(["cells_ge_6h", "cells_ge_3h", "max_median_delta", "country_code"], ascending=[False, False, False, True])
    )
    return ranking["country_code"].tolist()


def plot_country_heatmap_grid(cells: pd.DataFrame, out_path: Path, scenario: str, origin_scope: str) -> dict[str, object]:
    weeks = [pd.Timestamp(x) for x in sorted(cells["week_start"].dropna().unique())]
    countries = ordered_countries(cells)
    all_crops = crop_order(sorted(cells["crop_code"].dropna().unique()))
    ncols = 3
    nrows = int(np.ceil(len(countries) / ncols))
    fig = plt.figure(figsize=(18.0, 2.15 * nrows + 1.6))
    grid = GridSpec(nrows, ncols + 1, figure=fig, width_ratios=[1.0, 1.0, 1.0, 0.035], hspace=0.52, wspace=0.12)
    cmap = ListedColormap(DAMAGE_CLASS_COLORS)
    cmap.set_bad("#d9d9d9")
    norm = BoundaryNorm(np.arange(-0.5, len(DAMAGE_CLASS_LABELS) + 0.5), cmap.N)
    image = None

    for idx, iso in enumerate(countries):
        ax = fig.add_subplot(grid[idx // ncols, idx % ncols])
        subset = cells[cells["country_code"].eq(iso)]
        country_crops = [crop for crop in all_crops if crop in set(subset["crop_code"])]
        matrix = (
            subset.groupby(["crop_code", "week_start"])["median_delta_minutes"]
            .max()
            .reset_index()
            .pivot(index="crop_code", columns="week_start", values="median_delta_minutes")
            .reindex(index=country_crops, columns=weeks)
        )
        class_values = np.vectorize(damage_class)(matrix.to_numpy(dtype=float)).astype(float)
        class_values[class_values < 0] = np.nan
        image = ax.imshow(np.ma.masked_invalid(class_values), aspect="auto", interpolation="nearest", cmap=cmap, norm=norm)
        max_delta = float(subset["median_delta_minutes"].max(skipna=True) or 0.0)
        ge6 = int((subset["median_delta_minutes"] >= 360.0).sum())
        ax.set_title(f"{iso}  max median={max_delta / 60.0:.1f}h  cells>=6h={ge6}", fontsize=9)
        ax.set_yticks(np.arange(len(country_crops)))
        ax.set_yticklabels(country_crops, fontsize=7)
        ax.set_xticks(np.arange(len(weeks)))
        ax.set_xticklabels([])
        ax.tick_params(axis="x", length=2)
        if idx // ncols == nrows - 1:
            ax.set_xticklabels(week_labels(weeks), rotation=45, ha="right", fontsize=6)
            ax.tick_params(axis="x", labelbottom=True)
        else:
            ax.tick_params(axis="x", labelbottom=False)

    for idx in range(len(countries), nrows * ncols):
        fig.add_subplot(grid[idx // ncols, idx % ncols]).set_axis_off()

    if image is not None:
        cbar_ax = fig.add_subplot(grid[:, -1])
        cbar = fig.colorbar(image, cax=cbar_ax)
        cbar.set_ticks(np.arange(len(DAMAGE_CLASS_LABELS)))
        cbar.set_ticklabels(DAMAGE_CLASS_LABELS)
        cbar.ax.tick_params(labelsize=8)
        cbar.set_label("Worst destination-type median delay class per crop/week")

    fig.suptitle(
        f"Country crop-week accessibility damage dynamics | {scenario} | {origin_scope}",
        y=0.992,
        fontsize=14,
    )
    fig.text(
        0.055,
        0.030,
        "Each country panel: x = week, y = crop, color = worst destination-type median extra route delay. "
        "Baseline is the best week for the same OD route.",
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.055, right=0.94, top=0.94, bottom=0.08)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "countries": len(countries), "weeks": len(weeks), "crops": all_crops}


def plot_bubble_timeline(cells: pd.DataFrame, out_path: Path, scenario: str, origin_scope: str) -> dict[str, object]:
    weeks = [pd.Timestamp(x) for x in sorted(cells["week_start"].dropna().unique())]
    countries = ordered_countries(cells)
    week_index = {week: idx for idx, week in enumerate(weeks)}
    country_index = {iso: idx for idx, iso in enumerate(countries)}
    summary = (
        cells.groupby(["country_code", "week_start"], dropna=False)
        .agg(
            cells_ge_3h=("median_delta_minutes", lambda s: int((s >= 180.0).sum())),
            cells_ge_6h=("median_delta_minutes", lambda s: int((s >= 360.0).sum())),
            mean_affected_delta=("median_delta_minutes", lambda s: float(s[s >= 180.0].mean() if (s >= 180.0).any() else 0.0)),
            mean_delta=("median_delta_minutes", "mean"),
        )
        .reset_index()
    )
    plotted = summary[summary["cells_ge_3h"] > 0].copy()
    plotted["x"] = plotted["week_start"].map(week_index)
    plotted["y"] = plotted["country_code"].map(country_index)
    plotted["size"] = 12.0 + plotted["cells_ge_3h"].clip(upper=20) * 16.0
    color_values = plotted["mean_affected_delta"].clip(lower=180.0)

    fig, ax = plt.subplots(figsize=(17.0, 8.8))
    scatter = ax.scatter(
        plotted["x"],
        plotted["y"],
        s=plotted["size"],
        c=color_values,
        cmap="YlOrRd",
        norm=LogNorm(vmin=180.0, vmax=max(1440.0, float(color_values.max(skipna=True) or 1440.0))),
        alpha=0.86,
        edgecolor="#222222",
        linewidth=0.4,
    )
    ax.set_yticks(np.arange(len(countries)))
    ax.set_yticklabels(countries)
    ax.invert_yaxis()
    ax.set_xticks(np.arange(len(weeks)))
    ax.set_xticklabels(week_labels(weeks), rotation=45, ha="right")
    ax.set_xlabel("week start")
    ax.set_title("Weekly damage timeline: where disruption appears and how broad it is")
    ax.grid(axis="x", color="#dddddd", linewidth=0.7)
    ax.grid(axis="y", color="#eeeeee", linewidth=0.5)
    cbar = fig.colorbar(scatter, ax=ax, pad=0.012)
    cbar.set_label("Mean affected crop/destination median delay in country-week, minutes")
    cbar.set_ticks([180.0, 360.0, 720.0, 1440.0, 2880.0])
    cbar.set_ticklabels(["3h", "6h", "12h", "24h", "48h"])

    handles = [
        plt.scatter([], [], s=12.0 + value * 16.0, edgecolor="#222222", facecolor="#fdae61", alpha=0.86, label=f"{value} cells >=3h")
        for value in [1, 5, 10, 20]
    ]
    ax.legend(handles=handles, title="Bubble size", loc="upper right", frameon=True)
    fig.suptitle(f"{scenario} | {origin_scope}", y=0.985, fontsize=11)
    fig.text(
        0.070,
        0.030,
        "Bubble appears only when at least one crop/destination median cell is >=3h. Size counts affected cells; color shows the mean delay among affected cells in that country-week.",
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.07, right=0.91, top=0.91, bottom=0.14)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "bubbles": int(len(plotted)),
        "max_cells_ge_3h": int(plotted["cells_ge_3h"].max(skipna=True) or 0),
        "max_cells_ge_6h": int(plotted["cells_ge_6h"].max(skipna=True) or 0),
    }


def plot_crop_bubble_scatter(cells: pd.DataFrame, out_path: Path, scenario: str, origin_scope: str) -> dict[str, object]:
    summary = (
        cells.groupby(["country_code", "crop_code", "dest_type"], dropna=False)
        .agg(
            median_baseline_h=("median_baseline_h", "median"),
            clean_share=("median_delta_minutes", lambda s: float(np.mean(s < 180.0) * 100.0)),
            cells_ge_3h=("median_delta_minutes", lambda s: int((s >= 180.0).sum())),
            cells_ge_6h=("median_delta_minutes", lambda s: int((s >= 360.0).sum())),
            max_median_delta=("median_delta_minutes", "max"),
            median_delta=("median_delta_minutes", "median"),
        )
        .reset_index()
    )
    summary = summary[summary["median_baseline_h"].gt(0)].copy()
    summary["size"] = 12.0 + np.sqrt(summary["max_median_delta"].clip(lower=0.0)) * 3.2

    fig, ax = plt.subplots(figsize=(14.0, 9.0))
    for crop in crop_order(sorted(summary["crop_code"].dropna().unique())):
        subset = summary[summary["crop_code"].eq(crop)]
        ax.scatter(
            subset["median_baseline_h"],
            subset["clean_share"],
            s=subset["size"],
            c=CROP_COLORS.get(crop, "#777777"),
            alpha=0.86,
            edgecolor="#111111",
            linewidth=0.45,
            label=crop,
        )
    ax.set_xscale("log")
    ax.set_xlabel("Median baseline travel time for country/crop/destination, hours (log scale)")
    ax.set_ylabel("Weeks with median delay <3h, percent")
    ax.set_title("Crop risk structure: baseline remoteness vs weekly reliability")
    ax.grid(True, color="#dddddd", linewidth=0.8)
    ax.set_ylim(-3, 103)
    ax.legend(title="Crop", loc="lower left", frameon=True, ncols=2)

    size_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#bbbbbb",
            markeredgecolor="#111111",
            markersize=np.sqrt(12.0 + np.sqrt(minutes) * 3.2),
            label=label,
        )
        for minutes, label in [(180.0, "max 3h"), (720.0, "max 12h"), (1440.0, "max 24h"), (2880.0, "max 48h")]
    ]
    legend2 = ax.legend(handles=size_handles, title="Bubble size", loc="lower right", frameon=True)
    ax.add_artist(legend2)
    fig.suptitle(f"{scenario} | {origin_scope}", y=0.985, fontsize=11)
    fig.text(
        0.080,
        0.035,
        "Point = country/crop/destination type. Color = crop. Y is the share of weeks without >=3h median damage; size follows worst median delay.",
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.08, right=0.96, top=0.91, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "points": int(len(summary)),
        "min_clean_share": float(summary["clean_share"].min(skipna=True) or 0.0),
        "max_median_baseline_h": float(summary["median_baseline_h"].max(skipna=True) or 0.0),
    }


def plot_crop_annual_damage_matrix(cells: pd.DataFrame, out_path: Path, scenario: str, origin_scope: str) -> dict[str, object]:
    annual = cells.copy()
    annual["severe_burden_h"] = (annual["median_delta_minutes"] / 60.0 - 3.0).clip(lower=0.0)
    annual["affected"] = annual["median_delta_minutes"].ge(180.0)
    burden = (
        annual.groupby(["country_code", "crop_code"], dropna=False)
        .agg(
            annual_severe_burden_h=("severe_burden_h", "sum"),
            affected_weeks=("week_start", lambda s: int(annual.loc[s.index, "affected"].groupby(annual.loc[s.index, "week_start"]).any().sum())),
            peak_median_delay_h=("median_delta_minutes", lambda s: float(s.max(skipna=True) / 60.0 if len(s) else 0.0)),
        )
        .reset_index()
    )
    countries = ordered_countries(cells)
    crops = crop_order(sorted(cells["crop_code"].dropna().unique()))
    matrix = (
        burden.pivot(index="country_code", columns="crop_code", values="annual_severe_burden_h")
        .reindex(index=countries, columns=crops)
        .fillna(0.0)
    )
    weeks_matrix = (
        burden.pivot(index="country_code", columns="crop_code", values="affected_weeks")
        .reindex(index=countries, columns=crops)
        .fillna(0)
        .astype(int)
    )
    peak_matrix = (
        burden.pivot(index="country_code", columns="crop_code", values="peak_median_delay_h")
        .reindex(index=countries, columns=crops)
        .fillna(0.0)
    )

    positive = matrix.to_numpy(dtype=float)
    vmax = float(np.nanpercentile(positive[positive > 0], 97)) if np.any(positive > 0) else 1.0
    fig, ax = plt.subplots(figsize=(9.8, 10.5))
    cmap = plt.get_cmap("YlOrRd").copy()
    image = ax.imshow(matrix.to_numpy(dtype=float), aspect="auto", interpolation="nearest", cmap=cmap, vmin=0.0, vmax=vmax)
    ax.set_xticks(np.arange(len(crops)))
    ax.set_xticklabels(crops, rotation=35, ha="right")
    ax.set_yticks(np.arange(len(countries)))
    ax.set_yticklabels(countries)
    ax.set_title("Annual crop accessibility disruption burden")
    ax.set_xlabel("crop")
    ax.set_ylabel("country")
    ax.set_xticks(np.arange(-0.5, len(crops), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(countries), 1), minor=True)
    ax.grid(which="minor", color="#ffffff", linewidth=1.5)
    ax.tick_params(which="minor", bottom=False, left=False)

    for row_idx, iso in enumerate(countries):
        for col_idx, crop in enumerate(crops):
            weeks = int(weeks_matrix.loc[iso, crop])
            peak = float(peak_matrix.loc[iso, crop])
            burden_h = float(matrix.loc[iso, crop])
            if weeks == 0:
                continue
            text_color = "#111111" if burden_h < vmax * 0.58 else "#ffffff"
            ax.text(col_idx, row_idx, str(weeks), ha="center", va="center", fontsize=8.2, color=text_color)
            if peak >= 24.0:
                ax.scatter(col_idx + 0.32, row_idx - 0.30, s=22, c="#111111", marker="s", linewidth=0)
            elif peak >= 12.0:
                ax.scatter(col_idx + 0.32, row_idx - 0.30, s=22, c="#111111", marker="o", linewidth=0)

    cbar = fig.colorbar(image, ax=ax, pad=0.018, shrink=0.82)
    cbar.set_label("Annual severe burden, hours over 3h threshold")
    legend_handles = [
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#111111", markersize=6, label="peak >=12h"),
        Line2D([0], [0], marker="s", color="none", markerfacecolor="#111111", markersize=6, label="peak >=24h"),
    ]
    ax.legend(handles=legend_handles, loc="lower right", frameon=True)
    fig.suptitle(f"{scenario} | {origin_scope}", y=0.982, fontsize=11)
    fig.text(
        0.105,
        0.035,
        "Cell color = sum over weeks and destination types of max(median extra route hours - 3h, 0). "
        "Cell number = affected weeks for that crop with any destination type >=3h.",
        ha="left",
        fontsize=8.7,
    )
    fig.subplots_adjust(left=0.14, right=0.90, top=0.91, bottom=0.13)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "countries": len(countries),
        "crops": crops,
        "max_annual_severe_burden_h": float(matrix.max().max()),
        "max_affected_weeks": int(weeks_matrix.max().max()),
    }


def annual_crop_points(cells: pd.DataFrame) -> pd.DataFrame:
    weekly = (
        cells.groupby(["country_code", "crop_code", "week_start"], dropna=False)
        .agg(
            week_median_delay_h=("median_delta_minutes", lambda s: float(np.nanmedian(s) / 60.0)),
            week_max_delay_h=("median_delta_minutes", lambda s: float(np.nanmax(s) / 60.0)),
            destination_types=("dest_type", "nunique"),
        )
        .reset_index()
    )
    weekly["affected"] = weekly["week_max_delay_h"].ge(3.0)
    weekly["severe_burden_h"] = (weekly["week_max_delay_h"] - 3.0).clip(lower=0.0)
    points = (
        weekly.groupby(["country_code", "crop_code"], dropna=False)
        .agg(
            affected_weeks=("affected", "sum"),
            mean_affected_delay_h=("week_max_delay_h", lambda s: float(s[weekly.loc[s.index, "affected"]].mean() if weekly.loc[s.index, "affected"].any() else 0.0)),
            peak_delay_h=("week_max_delay_h", "max"),
            annual_severe_burden_h=("severe_burden_h", "sum"),
            weeks=("week_start", "nunique"),
        )
        .reset_index()
    )
    return points[points["affected_weeks"].gt(0)].copy()


def weighted_mean(values: pd.Series, weights: pd.Series) -> float:
    clean = pd.DataFrame({"value": values, "weight": weights}).dropna()
    clean = clean[clean["weight"].gt(0)]
    if clean.empty:
        return float(values.mean(skipna=True) or 0.0)
    return float(np.average(clean["value"], weights=clean["weight"]))


def weighted_annual_crop_points(frame: pd.DataFrame, vulnerability: pd.DataFrame | None, label: str) -> pd.DataFrame:
    weighted = frame.copy()
    weighted["cluster_weight"] = weighted["cluster_cell_count"].fillna(1.0).clip(lower=1.0)
    weighted["delta_h"] = weighted["delta_minutes"] / 60.0
    if vulnerability is not None:
        weighted = weighted.merge(vulnerability[["crop_code", "vulnerability_score"]], on="crop_code", how="left")
        weighted["vulnerability_score"] = weighted["vulnerability_score"].fillna(1.0)
    else:
        weighted["vulnerability_score"] = 1.0

    weekly_dest = (
        weighted.groupby(["country_code", "crop_code", "week_start", "dest_type"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "weighted_delay_h": weighted_mean(g["delta_h"], g["cluster_weight"]),
                    "weighted_degraded_delay_h": weighted_mean(g["delta_h"] * g["vulnerability_score"], g["cluster_weight"]),
                    "cluster_weight_sum": float(g["cluster_weight"].sum()),
                    "od_rows": int(len(g)),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )
    delay_col = "weighted_degraded_delay_h" if vulnerability is not None else "weighted_delay_h"
    weekly = (
        weekly_dest.groupby(["country_code", "crop_code", "week_start"], dropna=False)
        .agg(
            week_delay_h=(delay_col, "mean"),
            week_physical_delay_h=("weighted_delay_h", "mean"),
            destination_types=("dest_type", "nunique"),
            cluster_weight_sum=("cluster_weight_sum", "sum"),
        )
        .reset_index()
    )
    weekly["affected"] = weekly["week_physical_delay_h"].ge(3.0)
    weekly["severe_burden_h"] = (weekly["week_delay_h"] - 3.0).clip(lower=0.0)
    points = (
        weekly.groupby(["country_code", "crop_code"], dropna=False)
        .agg(
            affected_weeks=("affected", "sum"),
            mean_affected_delay_h=("week_delay_h", lambda s: float(s[weekly.loc[s.index, "affected"]].mean() if weekly.loc[s.index, "affected"].any() else 0.0)),
            peak_delay_h=("week_delay_h", "max"),
            annual_severe_burden_h=("severe_burden_h", "sum"),
            weeks=("week_start", "nunique"),
            total_cluster_weight=("cluster_weight_sum", "sum"),
        )
        .reset_index()
    )
    affected_weight = (
        weekly.loc[weekly["affected"]]
        .groupby(["country_code", "crop_code"], dropna=False)["cluster_weight_sum"]
        .sum()
        .rename("affected_cluster_weight")
        .reset_index()
    )
    points = points.merge(affected_weight, on=["country_code", "crop_code"], how="left")
    points["affected_cluster_weight"] = points["affected_cluster_weight"].fillna(0.0)
    points["weighting"] = label
    return points[points["affected_weeks"].gt(0)].copy()


def weighted_annual_crop_points_by_dest(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    weighted = frame.copy()
    weighted["cluster_weight"] = weighted["cluster_cell_count"].fillna(1.0).clip(lower=1.0)
    weighted["delta_h"] = weighted["delta_minutes"] / 60.0

    weekly = (
        weighted.groupby(["country_code", "crop_code", "week_start", "dest_type"], dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "week_delay_h": weighted_mean(g["delta_h"], g["cluster_weight"]),
                    "cluster_weight_sum": float(g["cluster_weight"].sum()),
                    "od_rows": int(len(g)),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )
    weekly["affected"] = weekly["week_delay_h"].ge(3.0)
    weekly["severe_burden_h"] = (weekly["week_delay_h"] - 3.0).clip(lower=0.0)
    points = (
        weekly.groupby(["country_code", "crop_code", "dest_type"], dropna=False)
        .agg(
            affected_weeks=("affected", "sum"),
            mean_affected_delay_h=("week_delay_h", lambda s: float(s[weekly.loc[s.index, "affected"]].mean() if weekly.loc[s.index, "affected"].any() else 0.0)),
            peak_delay_h=("week_delay_h", "max"),
            annual_severe_burden_h=("severe_burden_h", "sum"),
            weeks=("week_start", "nunique"),
            total_cluster_weight=("cluster_weight_sum", "sum"),
        )
        .reset_index()
    )
    affected_weight = (
        weekly.loc[weekly["affected"]]
        .groupby(["country_code", "crop_code", "dest_type"], dropna=False)["cluster_weight_sum"]
        .sum()
        .rename("affected_cluster_weight")
        .reset_index()
    )
    points = points.merge(affected_weight, on=["country_code", "crop_code", "dest_type"], how="left")
    points["affected_cluster_weight"] = points["affected_cluster_weight"].fillna(0.0)
    points["weighting"] = label
    return points[points["affected_weeks"].gt(0)].copy()


def label_important_points(
    ax: plt.Axes,
    points: pd.DataFrame,
    top_n: int = 5,
    fontsize: float = 8.0,
    label_country_codes: list[str] | None = None,
) -> None:
    if points.empty:
        return
    if label_country_codes is None:
        top_countries = (
            points.groupby("country_code", dropna=False)["mean_affected_delay_h"]
            .max()
            .sort_values(ascending=False)
            .head(top_n)
            .index
        )
    else:
        top_countries = label_country_codes
    labels = points[points["country_code"].isin(top_countries)].sort_values(["country_code", "crop_code"])
    for row in labels.itertuples(index=False):
        label = f"{row.country_code} {row.crop_code}"
        dx = 0.25 if row.affected_weeks < 32 else -0.25
        ha = "left" if dx > 0 else "right"
        ax.annotate(
            label,
            xy=(row.affected_weeks, row.mean_affected_delay_h),
            xytext=(row.affected_weeks + dx, row.mean_affected_delay_h + 0.25),
            fontsize=fontsize,
            ha=ha,
            va="bottom",
        )


def plot_crop_duration_intensity_facets(
    points: pd.DataFrame,
    out_path: Path,
    scenario: str,
    origin_scope: str,
    size_metric: str,
    size_label: str,
    size_legend_values: list[float] | None = None,
) -> dict[str, object]:
    dest_order = ["city_5_100k", "city_100k_plus", "port", "airport"]
    plotted = points[points["dest_type"].isin(dest_order)].copy()
    max_size_metric = float(plotted[size_metric].max(skipna=True) or 1.0)
    plotted["size"] = 20.0 + 520.0 * np.sqrt(plotted[size_metric].clip(lower=0.0) / max_size_metric)
    y_upper = max(10.0, float(plotted["mean_affected_delay_h"].max(skipna=True) or 0.0) * 1.18)

    fig, axes = plt.subplots(2, 2, figsize=(16.0, 10.8), sharex=True, sharey=True)
    for ax, dest_type in zip(axes.flat, dest_order):
        subset = plotted[plotted["dest_type"].eq(dest_type)]
        for crop in crop_order(sorted(plotted["crop_code"].dropna().unique())):
            crop_subset = subset[subset["crop_code"].eq(crop)]
            if crop_subset.empty:
                continue
            ax.scatter(
                crop_subset["affected_weeks"],
                crop_subset["mean_affected_delay_h"],
                s=crop_subset["size"],
                c=CROP_COLORS.get(crop, "#777777"),
                alpha=0.82,
                edgecolor="#111111",
                linewidth=0.50,
            )
        label_important_points(ax, subset, top_n=2, fontsize=7.0, label_country_codes=PRECIP_TOP_LABEL_COUNTRIES)
        ax.set_title(DEST_TYPE_FACET_LABELS.get(dest_type, dest_type), fontsize=11)
        ax.set_xscale("linear")
        ax.set_yscale("log", base=np.e)
        ax.set_xlim(-0.5, 54)
        ax.set_ylim(2.6, y_upper)
        ax.set_xticks([0, 10, 20, 30, 40, 50])
        y_ticks = [tick for tick in [3, 5, 10, 20, 50, 100] if tick <= y_upper]
        ax.set_yticks(y_ticks)
        ax.set_yticklabels([str(tick) for tick in y_ticks])
        ax.grid(True, color="#e7e7e7", linewidth=0.8)

    for ax in axes[:, 0]:
        ax.set_ylabel("Mean delay during affected weeks, hours")
    for ax in axes[-1, :]:
        ax.set_xlabel("Affected weeks in 2024, count")

    crop_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor=CROP_COLORS.get(crop, "#777777"),
            markeredgecolor="#111111",
            markersize=7,
            label=crop,
        )
        for crop in crop_order(sorted(plotted["crop_code"].dropna().unique()))
    ]
    fig.legend(handles=crop_handles, title="Crop", loc="upper center", bbox_to_anchor=(0.50, 0.94), frameon=True, ncols=5)

    size_values = size_legend_values or [50.0, 250.0, 1000.0, max_size_metric]
    size_values = sorted({round(v, 1) for v in size_values if v <= max_size_metric})
    if max_size_metric not in size_values:
        size_values.append(round(max_size_metric, 1))
    size_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#bbbbbb",
            markeredgecolor="#111111",
            markersize=np.sqrt(20.0 + 520.0 * np.sqrt(value / max_size_metric)),
            label=f"{value:,.0f}",
        )
        for value in size_values
    ]
    fig.legend(handles=size_handles, title=size_label, loc="lower right", bbox_to_anchor=(0.965, 0.105), frameon=True)
    fig.suptitle(f"Cluster-weighted accessibility disruption by destination group | {scenario} | {origin_scope}", y=0.985, fontsize=12)
    fig.text(
        0.075,
        0.030,
        "Each panel uses one destination group only. X = physical affected weeks. Y = cluster-weighted route delay in physical hours, natural-log scale. "
        "Bubble size = affected crop-cluster exposure. No crop vulnerability multiplier.",
        ha="left",
        fontsize=8.8,
    )
    fig.subplots_adjust(left=0.075, right=0.965, top=0.82, bottom=0.105, hspace=0.24, wspace=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "points": int(len(plotted)),
        "facets": dest_order,
        "max_affected_weeks": int(plotted["affected_weeks"].max(skipna=True) or 0),
        "max_mean_affected_delay_h": float(plotted["mean_affected_delay_h"].max(skipna=True) or 0.0),
        "max_annual_severe_burden_h": float(plotted["annual_severe_burden_h"].max(skipna=True) or 0.0),
        "size_metric": size_metric,
        "max_size_metric": max_size_metric,
    }


def plot_crop_duration_intensity_bubble(
    points: pd.DataFrame,
    out_path: Path,
    scenario: str,
    origin_scope: str,
    title: str = "Crop accessibility disruption: duration vs intensity",
    footnote: str | None = None,
    log_axes: bool = True,
    size_metric: str = "annual_severe_burden_h",
    size_label: str = "Annual burden",
    size_legend_values: list[float] | None = None,
    figsize: tuple[float, float] = (14.5, 9.0),
    x_log: bool = False,
) -> dict[str, object]:
    fig, ax = plt.subplots(figsize=figsize)
    max_size_metric = float(points[size_metric].max(skipna=True) or 1.0)
    points = points.copy()
    points["size"] = 28.0 + 780.0 * np.sqrt(points[size_metric].clip(lower=0.0) / max_size_metric)
    for crop in crop_order(sorted(points["crop_code"].dropna().unique())):
        subset = points[points["crop_code"].eq(crop)]
        ax.scatter(
            subset["affected_weeks"],
            subset["mean_affected_delay_h"],
            s=subset["size"],
            c=CROP_COLORS.get(crop, "#777777"),
            alpha=0.82,
            edgecolor="#111111",
            linewidth=0.55,
            label=crop,
        )

    label_important_points(ax, points, label_country_codes=PRECIP_TOP_LABEL_COUNTRIES)
    if log_axes:
        y_upper = max(10.0, float(points["mean_affected_delay_h"].max(skipna=True) or 0.0) * 1.22)
        if x_log:
            ax.set_xscale("log")
            ax.set_xlim(0.85, 56)
            ax.set_xticks([1, 2, 5, 10, 20, 50])
            ax.set_xticklabels(["1", "2", "5", "10", "20", "50"])
        else:
            ax.set_xscale("linear")
            ax.set_xlim(-0.5, 54)
            ax.set_xticks([0, 10, 20, 30, 40, 50])
            ax.set_xticklabels(["0", "10", "20", "30", "40", "50"])
        ax.set_yscale("log", base=np.e)
        ax.set_ylim(2.6, y_upper)
        y_ticks = [tick for tick in [3, 5, 10, 20, 50, 100] if tick <= y_upper]
        ax.set_yticks(y_ticks)
        ax.set_yticklabels([str(tick) for tick in y_ticks])
    else:
        xmax = float(points["affected_weeks"].max(skipna=True) or 0.0)
        ax.set_xlim(-0.5, min(53.0, max(10.0, xmax * 1.12)))
        ax.set_ylim(0, max(10.0, float(points["mean_affected_delay_h"].max(skipna=True) or 0.0) * 1.16))
    ax.set_xlabel("Affected weeks in 2024, count")
    ax.set_ylabel("Mean delay during affected weeks, hours")
    ax.set_title(title)
    ax.grid(True, color="#e7e7e7", linewidth=0.8)
    crop_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor=CROP_COLORS.get(crop, "#777777"),
            markeredgecolor="#111111",
            markersize=8,
            label=crop,
        )
        for crop in crop_order(sorted(points["crop_code"].dropna().unique()))
    ]
    crop_legend = ax.legend(handles=crop_handles, title="Crop", loc="upper left", frameon=True, ncols=2)
    ax.add_artist(crop_legend)
    size_values = size_legend_values or [50.0, 250.0, 1000.0, max_size_metric]
    size_values = sorted({round(v, 1) for v in size_values if v <= max_size_metric})
    if max_size_metric not in size_values:
        size_values.append(round(max_size_metric, 1))
    size_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#bbbbbb",
            markeredgecolor="#111111",
            markersize=np.sqrt(28.0 + 780.0 * np.sqrt(value / max_size_metric)),
            label=f"{value:,.0f}",
        )
        for value in size_values
    ]
    if size_handles:
        legend2 = ax.legend(handles=size_handles, title=size_label, loc="lower right", frameon=True)
        ax.add_artist(legend2)
    fig.suptitle(f"{scenario} | {origin_scope}", y=0.985, fontsize=11)
    fig.text(
        0.075,
        0.035,
        fill(
            footnote
            or "Point = country/crop. X = weeks where at least one destination type has median delay >=3h. "
            "Y = mean of those weekly max destination-type delays. Bubble size = annual severe burden above 3h.",
            width=150,
        ),
        ha="left",
        fontsize=8.8,
    )
    fig.subplots_adjust(left=0.075, right=0.965, top=0.91, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "points": int(len(points)),
        "max_affected_weeks": int(points["affected_weeks"].max(skipna=True) or 0),
        "max_mean_affected_delay_h": float(points["mean_affected_delay_h"].max(skipna=True) or 0.0),
        "max_annual_severe_burden_h": float(points["annual_severe_burden_h"].max(skipna=True) or 0.0),
        "size_metric": size_metric,
        "max_size_metric": max_size_metric,
        "x_scale": "log" if x_log else "linear",
        "y_scale": "log_e" if log_axes else "linear",
    }


def manifest_summary(frame: pd.DataFrame, cells: pd.DataFrame, plots: list[dict[str, object]], scenario: str, origin_scope: str) -> dict[str, object]:
    countries = ordered_countries(cells)
    top = (
        cells.groupby("country_code")
        .agg(
            cells_total=("median_delta_minutes", "size"),
            cells_ge_3h=("median_delta_minutes", lambda s: int((s >= 180.0).sum())),
            cells_ge_6h=("median_delta_minutes", lambda s: int((s >= 360.0).sum())),
            max_median_delta_minutes=("median_delta_minutes", "max"),
        )
        .reset_index()
        .sort_values(["cells_ge_6h", "cells_ge_3h", "max_median_delta_minutes"], ascending=False)
        .head(8)
    )
    return {
        "scenario": scenario,
        "origin_scope": origin_scope,
        "source_rows": int(len(frame)),
        "cell_rows": int(len(cells)),
        "countries": countries,
        "weeks": int(cells["week_start"].nunique()),
        "crops": crop_order(sorted(cells["crop_code"].dropna().unique())),
        "dest_types": sorted(cells["dest_type"].dropna().unique()),
        "plots": plots,
        "top_country_damage": top.to_dict(orient="records"),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(args.db_url) as conn:
        frame = add_common_time_columns(fetch_rows(conn, args.scenario, args.origin_scope, args.min_weeks))
    if frame.empty:
        raise SystemExit("No rows loaded for requested scenario/origin scope.")

    cells = summarize_cells(frame)
    cells_csv = out_dir / "visual_experiment_cells.csv"
    cells.to_csv(cells_csv, index=False)
    crop_points = weighted_annual_crop_points(frame, None, "cluster_cell_count")
    crop_points_csv = out_dir / "visual_experiment_crop_points_cluster_weighted.csv"
    crop_points.to_csv(crop_points_csv, index=False)
    dest_crop_points = weighted_annual_crop_points_by_dest(frame, "cluster_cell_count_by_destination_group")
    dest_crop_points_csv = out_dir / "visual_experiment_crop_points_cluster_weighted_by_dest.csv"
    dest_crop_points.to_csv(dest_crop_points_csv, index=False)
    size_breaks = [
        float(crop_points["affected_cluster_weight"].quantile(q))
        for q in [0.50, 0.80, 0.95]
        if crop_points["affected_cluster_weight"].gt(0).any()
    ]
    dest_size_breaks = [
        float(dest_crop_points["affected_cluster_weight"].quantile(q))
        for q in [0.50, 0.80, 0.95]
        if dest_crop_points["affected_cluster_weight"].gt(0).any()
    ]
    plots = [
        plot_bubble_timeline(cells, out_dir / "02_bubble_timeline.png", args.scenario, args.origin_scope),
        plot_crop_duration_intensity_facets(
            dest_crop_points,
            out_dir / "07_crop_duration_intensity_crop_degradation.png",
            args.scenario,
            args.origin_scope,
            size_metric="affected_cluster_weight",
            size_label="Affected crop-cluster exposure",
            size_legend_values=dest_size_breaks,
        ),
        plot_crop_duration_intensity_bubble(
            crop_points,
            out_dir / "07b_crop_duration_intensity_crop_degradation_linear_vertical.png",
            args.scenario,
            args.origin_scope,
            title="Cluster-weighted accessibility disruption: duration vs intensity (linear)",
            footnote="Linear-scale companion to 07. X = physical affected weeks. Y = cluster-weighted route delay in physical hours. Bubble size = affected crop-cluster exposure. No crop vulnerability multiplier.",
            log_axes=False,
            size_metric="affected_cluster_weight",
            size_label="Affected crop-cluster exposure",
            size_legend_values=size_breaks,
            figsize=(10.5, 13.0),
        ),
    ]
    manifest = manifest_summary(frame, cells, plots, args.scenario, args.origin_scope)
    manifest["cells_csv"] = str(cells_csv)
    manifest["crop_points_csv"] = str(crop_points_csv)
    manifest["dest_crop_points_csv"] = str(dest_crop_points_csv)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"[done] rows={len(frame):,} cells={len(cells):,} out_dir={out_dir}")
    for plot in plots:
        log(f"[plot] {plot['path']}")


if __name__ == "__main__":
    main()
