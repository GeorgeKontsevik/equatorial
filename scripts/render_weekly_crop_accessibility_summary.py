#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.gridspec import GridSpec
import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from render_weekly_astar_accessibility_heatmaps import (  # noqa: E402
    CROP_ORDER,
    DEFAULT_DB_URL,
    crop_order,
    week_labels,
)


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render one all-country crop x week accessibility summary heatmap.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default="weekly_sum_penalty_v1")
    parser.add_argument("--origin-scope", default="top5_per_crop")
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "outputs" / "astar_accessibility_weekly" / "aggregate_crop_weekly_summary"),
    )
    return parser.parse_args()


def fetch_rows(conn: psycopg.Connection, scenario: str, origin_scope: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        WITH base AS (
            SELECT
                country_code,
                week_start,
                crop_code,
                candidate_rank,
                dest_type,
                dest_rank,
                dest_id,
                route_status,
                travel_time_h,
                concat_ws('|', country_code, crop_code, candidate_rank, dest_type, dest_rank, dest_id) AS od_key
            FROM eq.crop_accessibility_weekly_astar
            WHERE scenario = %(scenario)s
              AND origin_scope = %(origin_scope)s
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
            b.dest_type,
            (b.travel_time_h - bl.baseline_h) * 60.0 AS delta_minutes
        FROM base b
        JOIN baseline bl ON b.od_key = bl.od_key
        WHERE b.route_status = 'ok'
          AND b.travel_time_h IS NOT NULL
          AND b.travel_time_h > 0
        ORDER BY b.dest_type, b.crop_code, b.week_start, b.country_code
        """,
        conn,
        params={"scenario": scenario, "origin_scope": origin_scope},
    )


def summarize(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    country_week_crop = (
        frame.groupby(["country_code", "week_start", "crop_code", "dest_type"], dropna=False)
        .agg(country_median_delta=("delta_minutes", "median"), od_rows=("delta_minutes", "size"))
        .reset_index()
    )
    country_week_crop["positive_country_median_delta_h"] = country_week_crop["country_median_delta"].clip(lower=0.0) / 60.0
    summary = (
        country_week_crop.groupby(["week_start", "crop_code", "dest_type"], dropna=False)
        .agg(
            total_country_median_delay_h=("positive_country_median_delta_h", "sum"),
            median_delta_minutes=("country_median_delta", "median"),
            q75_delta_minutes=("country_median_delta", lambda s: float(np.nanpercentile(s, 75))),
            max_country_median_delta_minutes=("country_median_delta", "max"),
            share_ge_3h=("country_median_delta", lambda s: float(np.mean(s >= 180.0))),
            share_ge_6h=("country_median_delta", lambda s: float(np.mean(s >= 360.0))),
            share_ge_12h=("country_median_delta", lambda s: float(np.mean(s >= 720.0))),
            n_countries=("country_code", "nunique"),
            country_week_rows=("country_code", "size"),
            od_rows=("od_rows", "sum"),
        )
        .reset_index()
    )
    return country_week_crop, summary


TOTAL_DELAY_BOUNDS_H = [0.0, 3.0, 6.0, 12.0, 24.0, 48.0, 72.0]
TOTAL_DELAY_COLORS = ["#fffdf2", "#fee08b", "#fdae61", "#f46d43", "#d73027", "#a50026", "#4d0000"]


def plot_panel(
    ax: plt.Axes,
    summary: pd.DataFrame,
    weeks: list[pd.Timestamp],
    crops: list[str],
    dest_type: str,
) -> object | None:
    subset = summary[summary["dest_type"].eq(dest_type)]
    if subset.empty:
        ax.text(0.5, 0.5, f"No {dest_type} rows", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return None

    matrix = (
        subset.pivot(index="crop_code", columns="week_start", values="total_country_median_delay_h")
        .reindex(index=crops, columns=weeks)
        .to_numpy(dtype=float)
    )
    values = np.ma.masked_invalid(matrix)
    cmap = ListedColormap(TOTAL_DELAY_COLORS)
    cmap.set_bad("#d9d9d9")
    norm = BoundaryNorm(TOTAL_DELAY_BOUNDS_H, cmap.N, extend="max")
    image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap=cmap, norm=norm)

    ax.set_title(f"{dest_type}: total extra hours by crop/week, summed across country medians")
    ax.set_ylabel("crop")
    ax.set_yticks(np.arange(len(crops)))
    ax.set_yticklabels(crops)
    ax.set_xticks(np.arange(len(weeks)))
    ax.set_xticklabels([])
    ax.tick_params(axis="x", labelbottom=False)
    return image


def plot_summary(
    summary: pd.DataFrame,
    country_week_crop: pd.DataFrame,
    out_path: Path,
    scenario: str,
    origin_scope: str,
) -> dict[str, object]:
    summary = summary.copy()
    summary["week_start"] = pd.to_datetime(summary["week_start"])
    weeks = [pd.Timestamp(x) for x in sorted(summary["week_start"].dropna().unique())]
    crops = crop_order(sorted(summary["crop_code"].dropna().unique().tolist()))

    fig = plt.figure(figsize=(16.0, 8.8))
    grid = GridSpec(2, 2, figure=fig, width_ratios=[1.0, 0.030], height_ratios=[1.0, 1.0], hspace=0.34, wspace=0.025)
    city_ax = fig.add_subplot(grid[0, 0])
    port_ax = fig.add_subplot(grid[1, 0], sharex=city_ax)
    cbar_ax = fig.add_subplot(grid[:, 1])

    images = []
    for ax, dest_type in [(city_ax, "city"), (port_ax, "port")]:
        image = plot_panel(ax, summary, weeks, crops, dest_type)
        if image is not None:
            images.append(image)

    port_ax.set_xticks(np.arange(len(weeks)))
    port_ax.set_xticklabels(week_labels(weeks), rotation=35, ha="right", fontsize=8)
    port_ax.tick_params(axis="x", labelbottom=True)
    port_ax.set_xlabel("week start")

    if images:
        cbar = fig.colorbar(images[0], cax=cbar_ax, orientation="vertical", extend="max")
        cbar.set_ticks([0.0, 3.0, 6.0, 12.0, 24.0, 48.0, 72.0])
        cbar.set_ticklabels(["0", "3", "6", "12", "24", "48", "72+"])
        cbar.ax.tick_params(labelsize=8)
        cbar.set_label("Total extra route delay, hours")

    coverage = (
        country_week_crop.groupby("dest_type")
        .agg(countries=("country_code", "nunique"), country_week_crop_rows=("country_code", "size"), od_rows=("od_rows", "sum"))
        .reset_index()
        .sort_values("dest_type")
    )
    coverage_text = "; ".join(
        f"{row.dest_type}: {int(row.countries)} countries"
        for row in coverage.itertuples(index=False)
    )
    max_total_h = float(summary["total_country_median_delay_h"].max(skipna=True) or 0.0)
    max_delta = float(summary["max_country_median_delta_minutes"].max(skipna=True) or 0.0)

    fig.suptitle(
        f"All countries 2024 crop weekly accessibility impact | {scenario} (unknown as unpaved) | {origin_scope} | "
        f"weeks={len(weeks)} | max total={max_total_h:.1f} h | max country median={max_delta / 60.0:.1f} h",
        y=0.982,
    )
    fig.text(
        0.065,
        0.040,
        "Cell = sum of positive country-level median route delays in hours; median is computed across OD routes first.\n"
        "This keeps countries comparable while showing aggregate crop/week disruption magnitude. Coverage: "
        + coverage_text,
        ha="left",
        va="center",
        fontsize=8.5,
    )
    fig.subplots_adjust(left=0.07, right=0.95, top=0.90, bottom=0.15)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "png": str(out_path),
        "weeks": len(weeks),
        "crops": len(crops),
        "rows": int(len(summary)),
        "country_week_crop_rows": int(len(country_week_crop)),
        "max_total_country_median_delay_h": max_total_h,
        "max_country_median_delta_minutes": max_delta,
        "coverage": coverage.to_dict(orient="records"),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_path = out_dir / f"{args.scenario}_{args.origin_scope}_all_countries_crop_weekly_damage_summary.png"
    with psycopg.connect(args.db_url) as conn:
        rows = fetch_rows(conn, args.scenario, args.origin_scope)
    if rows.empty:
        raise SystemExit("No accessibility rows found for requested scenario/scope.")
    rows["week_start"] = pd.to_datetime(rows["week_start"])
    country_week_crop, summary = summarize(rows)
    item = plot_summary(summary, country_week_crop, out_path, args.scenario, args.origin_scope)
    (out_dir / "manifest.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    summary.to_csv(out_dir / f"{args.scenario}_{args.origin_scope}_all_countries_crop_weekly_damage_summary.csv", index=False)
    log(f"[done] rows={len(rows):,} summary_rows={len(summary):,} png={out_path}")
    log(f"[done] manifest={out_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
