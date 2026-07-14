#!/usr/bin/env python3
"""Render English compact 4:3 Liberia city accessibility heatmap."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import pandas as pd
import psycopg

import render_weekly_astar_accessibility_heatmaps as heatmaps


SCENARIO = "weekly_sum_penalty_v1"
ORIGIN_SCOPE = "cluster_connected_allclusters_10small_3large_3ports_3airports"
DEST_TYPES = ("city_5_100k", "city_100k_plus")
CROPS = ["avocado", "banana", "mango", "pineapple", "plantain"]
OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "outputs/astar_accessibility_weekly/lbr_two_city_heatmap/LBR_two_city_accessibility_heatmap_4x3_en.png"
)

heatmaps.MONTH_LABELS_RU.update(
    {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
)
heatmaps.CROP_LABELS.update({crop: crop for crop in CROPS})
heatmaps.DEST_TYPE_LABELS.update(
    {
        "city_5_100k": "small city, 5-100k",
        "city_100k_plus": "large city, 100k+",
    }
)
heatmaps.DAMAGE_CLASS_LABELS[:] = ["<3h", "3-6h", "6-9h", "9-12h", "12-24h", ">24h"]
heatmaps.agg_label_ru = lambda value: {"median": "median", "p90": "p90", "p95": "p95", "max": "maximum"}.get(value, value)


def main() -> None:
    with psycopg.connect(heatmaps.DEFAULT_DB_URL) as conn:
        frame = heatmaps.add_ratios(heatmaps.fetch_country(conn, "LBR", SCENARIO, ORIGIN_SCOPE))

    present_crops = set(frame["crop_code"].dropna())
    present_dest_types = set(frame["dest_type"].dropna())
    if missing := set(CROPS) - present_crops:
        raise ValueError(f"Missing Liberia crops: {sorted(missing)}")
    if missing := set(DEST_TYPES) - present_dest_types:
        raise ValueError(f"Missing Liberia destination types: {sorted(missing)}")

    frame = frame[frame["crop_code"].isin(CROPS) & frame["dest_type"].isin(DEST_TYPES)]
    weeks = [pd.Timestamp(value) for value in sorted(frame["week_start"].dropna().unique())]

    fig = plt.figure(figsize=(12, 9))
    grid = GridSpec(2, 2, figure=fig, width_ratios=[1, 0.035], hspace=0.34, wspace=0.05)
    top_ax = fig.add_subplot(grid[0, 0])
    bottom_ax = fig.add_subplot(grid[1, 0], sharex=top_ax)
    colorbar_ax = fig.add_subplot(grid[:, 1])

    images = [
        heatmaps.plot_heatmap(ax, frame, dest_type, weeks, CROPS, "delta_minutes", "median", 240.0)
        for ax, dest_type in zip((top_ax, bottom_ax), DEST_TYPES)
    ]
    for ax in (top_ax, bottom_ax):
        ax.set_ylabel("crop")
        ax.set_title(ax.get_title().replace("класс деградации доступности по дополнительным минутам", "accessibility degradation class by added minutes"))
    bottom_ax.set_xticks(range(len(weeks)))
    bottom_ax.set_xticklabels(heatmaps.week_labels(weeks), rotation=35, ha="right", fontsize=8)
    bottom_ax.tick_params(axis="x", labelbottom=True)
    bottom_ax.set_xlabel("week start")

    colorbar = fig.colorbar(images[0], cax=colorbar_ax)
    colorbar.set_ticks(range(len(heatmaps.DAMAGE_CLASS_LABELS)))
    colorbar.set_ticklabels(heatmaps.DAMAGE_CLASS_LABELS)
    colorbar.ax.tick_params(labelsize=8, pad=3)
    colorbar.set_label("Added-delay class", labelpad=8)

    fig.suptitle("Liberia: weekly degradation of city accessibility", fontsize=15, y=0.975)
    fig.subplots_adjust(left=0.10, right=0.91, top=0.91, bottom=0.13)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=200, facecolor="white")
    plt.close(fig)
    print(f"{OUTPUT} | weeks={len(weeks)} crops={len(CROPS)} destinations={len(DEST_TYPES)}")


if __name__ == "__main__":
    main()
