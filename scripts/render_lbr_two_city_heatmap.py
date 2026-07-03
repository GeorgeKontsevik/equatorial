#!/usr/bin/env python3
"""Render the compact 4:3 Liberia heatmap used in the chapter figure."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import pandas as pd
import psycopg

from render_weekly_astar_accessibility_heatmaps import (
    DAMAGE_CLASS_LABELS,
    DEFAULT_DB_URL,
    add_ratios,
    fetch_country,
    plot_heatmap,
    week_labels,
)


SCENARIO = "weekly_sum_penalty_v1"
ORIGIN_SCOPE = "cluster_connected_allclusters_10small_3large_3ports_3airports"
DEST_TYPES = ("city_5_100k", "city_100k_plus")
CROPS = ["avocado", "banana", "mango", "pineapple", "plantain"]
OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "outputs/astar_accessibility_weekly/lbr_two_city_heatmap/LBR_two_city_accessibility_heatmap_4x3.png"
)


def main() -> None:
    with psycopg.connect(DEFAULT_DB_URL) as conn:
        frame = add_ratios(fetch_country(conn, "LBR", SCENARIO, ORIGIN_SCOPE))

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
        plot_heatmap(ax, frame, dest_type, weeks, CROPS, "delta_minutes", "median", 240.0)
        for ax, dest_type in zip((top_ax, bottom_ax), DEST_TYPES)
    ]
    bottom_ax.set_xticks(range(len(weeks)))
    bottom_ax.set_xticklabels(week_labels(weeks), rotation=35, ha="right", fontsize=8)
    bottom_ax.tick_params(axis="x", labelbottom=True)
    bottom_ax.set_xlabel("начало недели")

    colorbar = fig.colorbar(images[0], cax=colorbar_ax)
    colorbar.set_ticks(range(len(DAMAGE_CLASS_LABELS)))
    colorbar.set_ticklabels(DAMAGE_CLASS_LABELS)
    colorbar.ax.tick_params(labelsize=8, pad=3)
    colorbar.set_label("Класс дополнительной задержки", labelpad=8)

    fig.suptitle("Либерия: недельная деградация доступности городов", fontsize=15, y=0.975)
    fig.subplots_adjust(left=0.10, right=0.91, top=0.91, bottom=0.13)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=200, facecolor="white")
    plt.close(fig)
    print(f"{OUTPUT} | weeks={len(weeks)} crops={len(CROPS)} destinations={len(DEST_TYPES)}")


if __name__ == "__main__":
    main()
