#!/usr/bin/env python3
"""Render the chapter 4 crop-by-factor Spearman heatmap in Russian."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
SOURCE = (
    ROOT
    / "outputs/astar_accessibility_weekly/paper_experiment_country_mechanism_structural_v1/data/crop_stat_summary.csv"
)
OUTPUT = REPO_ROOT / "itmo-phd-thesis-template-en/images/ch4/crop_spearman_transposed_4x3_ru.png"
CROPS = ["avocado", "banana", "pineapple", "mango", "plantain"]
CROP_LABELS = {
    "avocado": "авокадо",
    "banana": "банан",
    "pineapple": "ананас",
    "mango": "манго",
    "plantain": "плантан",
}
FACTORS = [
    ("rho_log_threshold_impact", "воздействие\nосадков", 1.0),
    ("rho_log_remoteness_h", "удалённость\nпо времени", 1.0),
    ("rho_actual_unpaved_time_share", "доля дорог\nбез покрытия", -1.0),
]
TITLE = "Корреляция Спирмена с задержкой доступности"
COLORBAR_LABEL = "ρ Спирмена"


def display_matrix(frame: pd.DataFrame) -> np.ndarray:
    ordered = frame.set_index("crop_code").loc[CROPS]
    values = np.full((len(CROPS), len(FACTORS)), np.nan)
    for column_index, (column, _label, sign) in enumerate(FACTORS):
        supported = ordered[f"{column}_supported"].astype(bool).to_numpy()
        rho = ordered[column].to_numpy(dtype=float) * sign
        values[supported, column_index] = rho[supported]
    return values


def main() -> None:
    values = display_matrix(pd.read_csv(SOURCE))
    cmap = plt.get_cmap("RdBu_r").copy()
    cmap.set_bad("#dddddd")

    fig, ax = plt.subplots(figsize=(12, 9))
    image = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, vmin=-1, vmax=1, aspect="auto")
    ax.set_yticks(range(len(CROPS)), [CROP_LABELS[crop] for crop in CROPS], fontsize=14)
    ax.set_xticks(range(len(FACTORS)), [label for _column, label, _sign in FACTORS], fontsize=13)
    ax.tick_params(axis="x", top=True, bottom=False, labeltop=True, labelbottom=False, pad=10)
    ax.tick_params(axis="y", pad=8)

    for row, column in zip(*np.where(np.isfinite(values))):
        value = values[row, column]
        color = "white" if abs(value) >= 0.58 else "#222222"
        ax.text(column, row, f"{value:.2f}", ha="center", va="center", fontsize=17, color=color)

    ax.set_title(TITLE, fontsize=19, fontweight="semibold", pad=24)
    ax.set_xticks(np.arange(-0.5, len(FACTORS), 1), minor=True)
    ax.set_yticks(np.arange(-0.5, len(CROPS), 1), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="minor", bottom=False, left=False)

    colorbar = fig.colorbar(image, ax=ax, fraction=0.045, pad=0.035)
    colorbar.set_label(COLORBAR_LABEL, fontsize=14, labelpad=10)
    colorbar.ax.tick_params(labelsize=11)

    fig.subplots_adjust(left=0.15, right=0.89, top=0.80, bottom=0.08)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=200, facecolor="white")
    plt.close(fig)
    print(f"{OUTPUT} | shape={values.shape} annotated={np.isfinite(values).sum()}")


if __name__ == "__main__":
    main()
