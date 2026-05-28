#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from render_weekly_astar_accessibility_heatmaps import DEFAULT_DB_URL, week_labels  # noqa: E402
from render_weekly_crop_accessibility_summary import fetch_rows, summarize  # noqa: E402

SCENARIO = "weekly_sum_penalty_v1"
ORIGIN_SCOPE = "top5_per_crop"
OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "aggregate_crop_weekly_summary"

BANDS = [
    (0.0, 3.0, "#fffdf2", "0-3h"),
    (3.0, 6.0, "#fee08b", "3-6h"),
    (6.0, 12.0, "#fdae61", "6-12h"),
    (12.0, 24.0, "#f46d43", "12-24h"),
    (24.0, 80.0, "#d73027", ">24h"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render weekly crop accessibility boxplots across countries/crops.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default=SCENARIO)
    parser.add_argument("--origin-scope", default=ORIGIN_SCOPE)
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    return parser.parse_args()


def draw_bands(ax: plt.Axes, ymax: float) -> None:
    for lo, hi, color, label in BANDS:
        top = min(hi, ymax)
        if lo >= ymax:
            continue
        ax.axhspan(lo, top, color=color, alpha=0.22 if lo > 0 else 0.08, linewidth=0)
        if top > lo + 0.5:
            ax.text(
                0.995,
                (lo + top) / 2.0,
                label,
                transform=ax.get_yaxis_transform(),
                ha="right",
                va="center",
                fontsize=8,
                color="#3f2f20",
                alpha=0.75,
            )


def plot_dest(ax: plt.Axes, country_week_crop: pd.DataFrame, weeks: list[pd.Timestamp], dest_type: str, ymax: float) -> dict[str, float]:
    subset = country_week_crop[country_week_crop["dest_type"].eq(dest_type)].copy()
    subset["delay_h"] = subset["country_median_delta"].clip(lower=0.0) / 60.0
    positive = subset[subset["delay_h"].gt(0.0)]

    draw_bands(ax, ymax)
    data = []
    positions = []
    positive_counts = []
    total_by_week = []
    median_positive_by_week = []
    for i, week in enumerate(weeks):
        week_values = positive.loc[positive["week_start"].eq(week), "delay_h"].to_numpy(dtype=float)
        total = float(subset.loc[subset["week_start"].eq(week), "delay_h"].sum())
        total_by_week.append(total)
        positive_counts.append(int(len(week_values)))
        median_positive_by_week.append(float(np.median(week_values)) if len(week_values) else np.nan)
        if len(week_values):
            data.append(week_values)
            positions.append(i)

    if data:
        box = ax.boxplot(
            data,
            positions=positions,
            widths=0.55,
            patch_artist=True,
            manage_ticks=False,
            showfliers=True,
            flierprops={"marker": "o", "markersize": 2.0, "markerfacecolor": "#5b3a29", "markeredgewidth": 0, "alpha": 0.40},
            medianprops={"color": "#151515", "linewidth": 1.2},
            whiskerprops={"color": "#4a4a4a", "linewidth": 0.8},
            capprops={"color": "#4a4a4a", "linewidth": 0.8},
        )
        for patch in box["boxes"]:
            patch.set_facecolor("#6baed6")
            patch.set_edgecolor("#1f4e68")
            patch.set_alpha(0.58)

    ax.plot(range(len(weeks)), median_positive_by_week, color="#08306b", linewidth=1.4, label="median positive delay")
    twin = ax.twinx()
    twin.plot(range(len(weeks)), total_by_week, color="#99000d", linewidth=1.8, alpha=0.72, label="total delay hours")
    twin.set_ylabel("total h", color="#99000d")
    twin.tick_params(axis="y", colors="#99000d", labelsize=8)
    twin.spines["right"].set_color("#99000d")
    twin.set_ylim(0.0, max(total_by_week) * 1.12 if total_by_week and max(total_by_week) > 0 else 1.0)

    ax.set_title(f"{dest_type}: positive country-crop median delays by week; red line = total hours")
    ax.set_ylabel("extra route delay, h")
    ax.set_ylim(0.0, ymax)
    ax.grid(axis="y", color="#000000", alpha=0.12, linewidth=0.6)
    ax.set_xlim(-0.5, len(weeks) - 0.5)

    # Light count bars at the bottom show how many country-crop observations were non-zero.
    max_count = max(positive_counts) if positive_counts else 0
    if max_count:
        scaled = np.array(positive_counts, dtype=float) / max_count * (ymax * 0.055)
        ax.bar(range(len(weeks)), scaled, width=0.75, color="#222222", alpha=0.13, linewidth=0)
        ax.text(0.0, ymax * 0.060, "non-zero count bars", fontsize=7.5, color="#444444")

    return {
        "positive_country_crop_rows": int(len(positive)),
        "max_positive_delay_h": float(positive["delay_h"].max(skipna=True) or 0.0),
        "max_week_total_h": float(max(total_by_week) if total_by_week else 0.0),
        "max_positive_count_week": int(max_count),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_png = out_dir / f"{args.scenario}_{args.origin_scope}_all_countries_crop_weekly_delay_boxplots.png"
    out_csv = out_dir / f"{args.scenario}_{args.origin_scope}_all_countries_crop_weekly_delay_boxplot_source.csv"
    out_manifest = out_dir / f"{args.scenario}_{args.origin_scope}_all_countries_crop_weekly_delay_boxplots_manifest.json"

    with psycopg.connect(args.db_url) as conn:
        rows = fetch_rows(conn, args.scenario, args.origin_scope)
    if rows.empty:
        raise SystemExit("No accessibility rows found for requested scenario/scope.")
    rows["week_start"] = pd.to_datetime(rows["week_start"])
    country_week_crop, _summary = summarize(rows)
    country_week_crop["week_start"] = pd.to_datetime(country_week_crop["week_start"])
    country_week_crop["delay_h"] = country_week_crop["country_median_delta"].clip(lower=0.0) / 60.0
    weeks = [pd.Timestamp(x) for x in sorted(country_week_crop["week_start"].dropna().unique())]
    max_delay = float(country_week_crop["delay_h"].max(skipna=True) or 0.0)
    ymax = max(24.0, min(80.0, np.ceil(max_delay / 6.0) * 6.0))

    fig, axes = plt.subplots(2, 1, figsize=(16.0, 9.5), sharex=True)
    stats = {}
    for ax, dest_type in zip(axes, ["city", "port"], strict=True):
        stats[dest_type] = plot_dest(ax, country_week_crop, weeks, dest_type, ymax)

    axes[-1].set_xticks(np.arange(len(weeks)))
    axes[-1].set_xticklabels(week_labels(weeks), rotation=35, ha="right", fontsize=8)
    axes[-1].set_xlabel("week start")

    handles = [
        plt.Line2D([0], [0], color="#08306b", lw=1.4, label="median positive delay"),
        plt.Line2D([0], [0], color="#99000d", lw=1.8, label="total delay hours"),
    ]
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.955, 0.965), frameon=False, fontsize=9)
    fig.suptitle(
        f"All countries 2024 crop accessibility weekly delays | {args.scenario} (unknown as unpaved) | {args.origin_scope}",
        y=0.984,
    )
    fig.text(
        0.065,
        0.035,
        "Boxplots use positive country-crop median delays in hours; each observation is one country x crop x week. "
        "The red line is total positive country-crop median delay hours for the week.",
        ha="left",
        va="center",
        fontsize=8.5,
    )
    fig.subplots_adjust(left=0.075, right=0.925, top=0.90, bottom=0.145, hspace=0.33)
    fig.savefig(out_png, dpi=180)
    plt.close(fig)

    source_cols = ["country_code", "week_start", "crop_code", "dest_type", "country_median_delta", "delay_h", "od_rows"]
    country_week_crop[source_cols].to_csv(out_csv, index=False)
    manifest = {
        "png": str(out_png),
        "csv": str(out_csv),
        "weeks": len(weeks),
        "country_week_crop_rows": int(len(country_week_crop)),
        "max_delay_h": max_delay,
        "ymax_h": ymax,
        "stats": stats,
    }
    out_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[done] png={out_png}")
    print(f"[done] source_rows={len(country_week_crop):,} manifest={out_manifest}")


if __name__ == "__main__":
    main()
