#!/usr/bin/env python3
"""Render four representative country rainfall/delay time series."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SOURCE = (
    ROOT
    / "outputs/astar_accessibility_weekly/paper_experiment_country_mechanism_structural_v1/data/weekly_country_mechanism.csv"
)
OUTPUT = REPO_ROOT / "itmo-phd-thesis-template-en/images/ch4/temporal_rain_burden_four_countries_square_ru.png"
COUNTRIES = ["COL", "LBR", "CMR", "GAB"]
COUNTRY_LABELS = {
    "COL": "Колумбия",
    "LBR": "Либерия",
    "CMR": "Камерун",
    "GAB": "Габон",
}
MONTH_LABELS = {
    1: "янв",
    3: "мар",
    5: "май",
    7: "июл",
    9: "сен",
    11: "ноя",
}
RAIN_COLOR = "#3498db"
DELAY_COLOR = "#e53935"
RAIN_YLABEL = "осадки, мм/нед."
DELAY_YLABEL = "задержка, ч/нед."
RAIN_LEGEND_LABEL = "осадки"
DELAY_LEGEND_LABEL = "задержка доступности"
TITLE = "Осадки и задержка доступности"


def main() -> None:
    frame = pd.read_csv(SOURCE, parse_dates=["week_start"])
    frame = frame[frame["country_code"].isin(COUNTRIES)].copy()
    counts = frame.groupby("country_code").size().to_dict()
    if counts != {country: 53 for country in COUNTRIES}:
        raise ValueError(f"Unexpected weekly coverage: {counts}")

    rain_max = frame["median"].max() * 1.08
    delay_max = frame["weekly_burden_h"].max() * 1.08
    fig, axes = plt.subplots(2, 2, figsize=(12, 12), sharex=True, sharey=True)

    for index, (ax, country) in enumerate(zip(axes.ravel(), COUNTRIES)):
        subset = frame[frame["country_code"].eq(country)].sort_values("week_start")
        ax.plot(subset["week_start"], subset["median"], color=RAIN_COLOR, linewidth=2.0)
        ax.set_ylim(0, rain_max)
        ax.grid(True, color="#e6e6e6", linewidth=0.8)
        ax.set_title(COUNTRY_LABELS[country], fontsize=17, fontweight="semibold", pad=10)
        ax.tick_params(axis="y", labelcolor=RAIN_COLOR, labelsize=10)
        if index % 2 == 0:
            ax.set_ylabel(RAIN_YLABEL, color=RAIN_COLOR, fontsize=11)

        delay_ax = ax.twinx()
        delay_ax.plot(subset["week_start"], subset["weekly_burden_h"], color=DELAY_COLOR, linewidth=2.0)
        delay_ax.set_ylim(0, delay_max)
        delay_ax.tick_params(axis="y", labelcolor=DELAY_COLOR, labelsize=10)
        if index % 2 == 1:
            delay_ax.set_ylabel(DELAY_YLABEL, color=DELAY_COLOR, fontsize=11)
        else:
            delay_ax.set_yticklabels([])

    for ax in axes[-1]:
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(
            lambda value, _position: MONTH_LABELS.get(mdates.num2date(value).month, "")
        )
        ax.tick_params(axis="x", rotation=0, labelsize=10)
        ax.set_xlabel("2024", fontsize=11)

    legend = [
        Line2D([0], [0], color=RAIN_COLOR, linewidth=2.4, label=RAIN_LEGEND_LABEL),
        Line2D([0], [0], color=DELAY_COLOR, linewidth=2.4, label=DELAY_LEGEND_LABEL),
    ]
    fig.legend(handles=legend, loc="lower center", ncol=2, frameon=False, fontsize=12, bbox_to_anchor=(0.5, 0.025))
    fig.suptitle(TITLE, fontsize=21, fontweight="semibold", y=0.992)
    fig.subplots_adjust(left=0.09, right=0.91, top=0.91, bottom=0.10, hspace=0.28, wspace=0.22)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=200, facecolor="white")
    plt.close(fig)
    print(f"{OUTPUT} | countries={','.join(COUNTRIES)} weeks={counts}")


if __name__ == "__main__":
    main()
