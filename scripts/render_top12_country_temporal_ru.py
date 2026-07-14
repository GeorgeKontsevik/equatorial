#!/usr/bin/env python3
"""Render the Russian top-12 country rainfall/accessibility-delay panel."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT
    / "outputs/astar_accessibility_weekly/paper_experiment_country_mechanism_structural_v1/data/weekly_country_mechanism.csv"
)
OUTPUT = (
    ROOT
    / "outputs/astar_accessibility_weekly/paper_experiment_country_mechanism_structural_v1/figures_ru/ch4_temporal_rain_burden_top12_ru.png"
)
COUNTRIES = ["COL", "PNG", "MYS", "LBR", "GUY", "ECU", "CMR", "KEN", "TZA", "NGA", "SUR", "GAB"]
RAIN_COLOR = "#2f83c5"
DELAY_COLOR = "#d62728"


def main() -> None:
    frame = pd.read_csv(SOURCE, parse_dates=["week_start"])
    plotted = frame[frame["country_code"].isin(COUNTRIES)].copy()
    rain_max = max(190.0, float(plotted["median"].max()) * 1.03)
    delay_max = max(850.0, float(plotted["weekly_burden_h"].max()) * 1.03)

    fig, axes = plt.subplots(3, 4, figsize=(18.58, 10.27), sharex=True)
    for ax, country in zip(axes.ravel(), COUNTRIES):
        subset = plotted[plotted["country_code"].eq(country)].sort_values("week_start")
        ax.plot(subset["week_start"], subset["median"], color=RAIN_COLOR, linewidth=1.8)
        ax.set_ylim(0, rain_max)
        ax.tick_params(axis="y", colors=RAIN_COLOR, labelsize=8)
        ax.grid(True, color="#e6e6e6", linewidth=0.7)
        ax.set_title(country, fontsize=14, fontweight="bold", pad=3)

        delay_ax = ax.twinx()
        delay_ax.plot(subset["week_start"], subset["weekly_burden_h"], color=DELAY_COLOR, linewidth=1.8)
        delay_ax.set_ylim(0, delay_max)
        delay_ax.tick_params(axis="y", colors=DELAY_COLOR, labelsize=8)

        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.tick_params(axis="x", rotation=45, labelsize=8)

    fig.suptitle(
        "Осадки и задержка доступности: топ-12 стран по суммарной задержке",
        fontsize=18,
        fontweight="bold",
        y=0.985,
    )
    fig.text(0.012, 0.5, "медианные недельные осадки, мм", rotation=90, color=RAIN_COLOR, va="center", fontsize=12)
    fig.text(0.988, 0.5, "недельная задержка доступности, ч", rotation=-90, color=DELAY_COLOR, va="center", fontsize=12)
    fig.legend(
        handles=[
            Line2D([0], [0], color=RAIN_COLOR, linewidth=2, label="осадки"),
            Line2D([0], [0], color=DELAY_COLOR, linewidth=2, label="задержка доступности"),
        ],
        loc="lower center",
        ncol=2,
        frameon=False,
        fontsize=12,
    )
    fig.subplots_adjust(left=0.06, right=0.95, top=0.91, bottom=0.10, hspace=0.38, wspace=0.28)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=180, facecolor="white")
    plt.close(fig)
    print(f"wrote={OUTPUT}")


if __name__ == "__main__":
    main()
