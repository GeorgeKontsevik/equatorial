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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "visual_experiments"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "paper_experiment_regimes_v1"

DEST_TYPE_LABELS = {
    "city_5_100k": "small cities",
    "city_100k_plus": "large cities",
    "port": "ports",
    "airport": "airports",
}
DEST_TYPE_COLORS = {
    "city_5_100k": "#f6bd60",
    "city_100k_plus": "#e76f51",
    "port": "#d62828",
    "airport": "#5dade2",
}


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render paper-oriented regime experiment from existing visual experiment CSVs.")
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--top-n", type=int, default=15)
    return parser.parse_args()


def short_regime_label(row: pd.Series) -> str:
    return f"{row['country_code']} {row['crop_code']} {DEST_TYPE_LABELS.get(row['dest_type'], row['dest_type'])}"


def load_inputs(source_dir: Path) -> dict[str, pd.DataFrame | dict]:
    cells = pd.read_csv(source_dir / "visual_experiment_cells.csv")
    crop_points = pd.read_csv(source_dir / "visual_experiment_crop_points_cluster_weighted.csv")
    dest_crop_points = pd.read_csv(source_dir / "visual_experiment_crop_points_cluster_weighted_by_dest.csv")
    manifest_path = source_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return {
        "cells": cells,
        "crop_points": crop_points,
        "dest_crop_points": dest_crop_points,
        "manifest": manifest,
    }


def copy_source_artifacts(source_dir: Path, data_dir: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    for name in [
        "manifest.json",
        "visual_experiment_cells.csv",
        "visual_experiment_crop_points_cluster_weighted.csv",
        "visual_experiment_crop_points_cluster_weighted_by_dest.csv",
    ]:
        src = source_dir / name
        if not src.exists():
            continue
        dst = data_dir / f"source_{name}"
        shutil.copy2(src, dst)
        copied[name] = str(dst)
    return copied


def build_top_regimes(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    regimes = dest_crop_points.copy()
    regimes = regimes.sort_values(
        ["annual_severe_burden_h", "affected_cluster_weight", "mean_affected_delay_h", "affected_weeks"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    regimes["regime_label"] = regimes.apply(short_regime_label, axis=1)
    total_burden = float(regimes["annual_severe_burden_h"].sum() or 0.0)
    total_exposure = float(regimes["affected_cluster_weight"].sum() or 0.0)
    regimes["burden_share"] = regimes["annual_severe_burden_h"] / total_burden if total_burden > 0 else 0.0
    regimes["exposure_share"] = regimes["affected_cluster_weight"] / total_exposure if total_exposure > 0 else 0.0
    regimes["cumulative_burden_share"] = regimes["burden_share"].cumsum()
    regimes["rank"] = np.arange(1, len(regimes) + 1)
    return regimes


def build_destination_summary(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    summary = (
        dest_crop_points.groupby("dest_type", dropna=False)
        .agg(
            regime_count=("country_code", "size"),
            total_burden_h=("annual_severe_burden_h", "sum"),
            total_affected_exposure=("affected_cluster_weight", "sum"),
            median_affected_weeks=("affected_weeks", "median"),
            median_delay_h=("mean_affected_delay_h", "median"),
            p90_delay_h=("mean_affected_delay_h", lambda s: float(np.nanpercentile(s, 90))),
            max_delay_h=("mean_affected_delay_h", "max"),
        )
        .reset_index()
    )
    summary["dest_label"] = summary["dest_type"].map(DEST_TYPE_LABELS).fillna(summary["dest_type"])
    return summary.sort_values("total_burden_h", ascending=False).reset_index(drop=True)


def build_country_summary(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    summary = (
        dest_crop_points.groupby("country_code", dropna=False)
        .agg(
            regime_count=("dest_type", "size"),
            total_burden_h=("annual_severe_burden_h", "sum"),
            total_affected_exposure=("affected_cluster_weight", "sum"),
            max_delay_h=("mean_affected_delay_h", "max"),
            median_affected_weeks=("affected_weeks", "median"),
        )
        .reset_index()
        .sort_values(["total_burden_h", "total_affected_exposure"], ascending=False)
        .reset_index(drop=True)
    )
    return summary


def plot_risk_concentration(top_regimes: pd.DataFrame, out_path: Path, top_n: int) -> dict[str, object]:
    plotted = top_regimes.head(top_n).copy()
    labels = plotted["regime_label"].tolist()
    colors = [DEST_TYPE_COLORS.get(value, "#999999") for value in plotted["dest_type"]]

    fig, ax = plt.subplots(figsize=(15.5, 8.5))
    ax.bar(np.arange(len(plotted)), plotted["annual_severe_burden_h"], color=colors, edgecolor="#222222", linewidth=0.5)
    ax.set_xticks(np.arange(len(plotted)))
    ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=8)
    ax.set_ylabel("Annual severe burden, hours over 3h threshold")
    ax.set_xlabel("Country-crop-destination regime")
    ax.set_title("Risk concentration: top regimes dominate the annual burden")
    ax.grid(axis="y", color="#e6e6e6", linewidth=0.8)

    ax2 = ax.twinx()
    ax2.plot(np.arange(len(plotted)), plotted["cumulative_burden_share"] * 100.0, color="#111111", marker="o", linewidth=1.6)
    ax2.set_ylabel("Cumulative share of total burden, %")
    ax2.set_ylim(0, min(100.0, max(35.0, float(plotted["cumulative_burden_share"].max() * 112.0))))

    legend_handles = [
        Line2D([0], [0], marker="s", color="none", markerfacecolor=color, markeredgecolor="#222222", markersize=8, label=DEST_TYPE_LABELS[key])
        for key, color in DEST_TYPE_COLORS.items()
    ]
    ax.legend(handles=legend_handles, title="Destination group", loc="upper left", frameon=True, ncols=2)
    top_share = float(plotted["annual_severe_burden_h"].sum() / top_regimes["annual_severe_burden_h"].sum() * 100.0)
    fig.text(
        0.07,
        0.04,
        fill(
            f"Bars show annual severe burden for the top {top_n} country-crop-destination regimes. "
            f"The black line shows cumulative burden share. These top {top_n} regimes account for {top_share:.1f}% of the total burden in the current experiment.",
            width=150,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.07, right=0.92, top=0.90, bottom=0.29)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "top_n": top_n,
        "top_share_percent": top_share,
        "max_burden_h": float(plotted["annual_severe_burden_h"].max() or 0.0),
    }


def plot_destination_structure(dest_summary: pd.DataFrame, out_path: Path) -> dict[str, object]:
    ordered = dest_summary.copy()
    ordered["color"] = ordered["dest_type"].map(DEST_TYPE_COLORS).fillna("#999999")
    labels = ordered["dest_label"].tolist()

    fig, axes = plt.subplots(2, 2, figsize=(13.5, 9.5))
    metrics = [
        ("regime_count", "Regimes with >=3h impact"),
        ("median_affected_weeks", "Median affected weeks"),
        ("median_delay_h", "Median affected delay, hours"),
        ("total_burden_h", "Total severe burden, hours"),
    ]
    for ax, (column, title) in zip(axes.flat, metrics):
        values = ordered[column]
        ax.bar(labels, values, color=ordered["color"], edgecolor="#222222", linewidth=0.5)
        ax.set_title(title, fontsize=11)
        ax.grid(axis="y", color="#e6e6e6", linewidth=0.8)
        ax.tick_params(axis="x", rotation=20)
        if column == "total_burden_h":
            ax.ticklabel_format(axis="y", style="plain")

    fig.suptitle("Destination group matters: regimes differ by persistence, intensity, and burden", y=0.97, fontsize=13)
    fig.text(
        0.07,
        0.04,
        fill(
            "Each bar aggregates the current country-crop-destination regimes with at least one affected week. "
            "Together these panels show that destination-group differences are structural, not just crop-specific noise.",
            width=145,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.07, right=0.97, top=0.90, bottom=0.12, hspace=0.28, wspace=0.18)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "dest_types": ordered["dest_type"].tolist(),
        "max_total_burden_h": float(ordered["total_burden_h"].max() or 0.0),
    }


def plot_regime_scatter(top_regimes: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = top_regimes.copy()
    max_exposure = float(plotted["affected_cluster_weight"].max() or 1.0)
    plotted["size"] = 24.0 + 900.0 * np.sqrt(plotted["affected_cluster_weight"].clip(lower=0.0) / max_exposure)

    fig, ax = plt.subplots(figsize=(14.0, 10.0))
    for dest_type, subset in plotted.groupby("dest_type", dropna=False):
        ax.scatter(
            subset["affected_weeks"],
            subset["mean_affected_delay_h"],
            s=subset["size"],
            c=DEST_TYPE_COLORS.get(dest_type, "#999999"),
            alpha=0.82,
            edgecolor="#111111",
            linewidth=0.5,
            label=DEST_TYPE_LABELS.get(dest_type, dest_type),
        )

    label_rows = plotted.head(12)
    for row in label_rows.itertuples(index=False):
        dx = 0.35 if row.affected_weeks < 35 else -0.35
        ax.annotate(
            f"{row.country_code} {row.crop_code}",
            xy=(row.affected_weeks, row.mean_affected_delay_h),
            xytext=(row.affected_weeks + dx, row.mean_affected_delay_h + 0.3),
            fontsize=8,
            ha="left" if dx > 0 else "right",
            va="bottom",
        )

    ax.set_xlim(-0.5, 54)
    ax.set_ylim(0, max(10.0, float(plotted["mean_affected_delay_h"].max() or 0.0) * 1.12))
    ax.set_xticks([0, 10, 20, 30, 40, 50])
    ax.set_xlabel("Affected weeks in 2024, count")
    ax.set_ylabel("Mean delay during affected weeks, hours")
    ax.set_title("Regime map: vulnerability depends on persistence, intensity, and exposed crop mass")
    ax.grid(True, color="#e6e6e6", linewidth=0.8)
    color_legend = ax.legend(title="Destination group", loc="upper left", frameon=True)
    ax.add_artist(color_legend)

    legend_values = [float(plotted["affected_cluster_weight"].quantile(q)) for q in [0.50, 0.80, 0.95]]
    legend_values.append(max_exposure)
    legend_values = sorted({max(1.0, round(v, 0)) for v in legend_values})
    size_handles = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="#bbbbbb",
            markeredgecolor="#111111",
            markersize=np.sqrt(24.0 + 900.0 * np.sqrt(value / max_exposure)),
            label=f"{value:,.0f}",
        )
        for value in legend_values
    ]
    ax.legend(handles=size_handles, title="Affected crop-cluster exposure", loc="lower right", frameon=True)

    fig.text(
        0.075,
        0.04,
        fill(
            "Each point is one country-crop-destination regime. "
            "X captures persistence, Y captures intensity, and bubble size captures affected crop-cluster exposure. "
            "Labeled points are the highest-burden regimes in the current experiment.",
            width=150,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.075, right=0.965, top=0.92, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {
        "path": str(out_path),
        "points": int(len(plotted)),
        "max_delay_h": float(plotted["mean_affected_delay_h"].max() or 0.0),
        "max_exposure": max_exposure,
    }


def write_table(frame: pd.DataFrame, path: Path) -> str:
    frame.to_csv(path, index=False)
    return str(path)


def main() -> None:
    args = parse_args()
    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)
    data_dir = out_dir / "data"
    figures_dir = out_dir / "figures"
    data_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    loaded = load_inputs(source_dir)
    copied = copy_source_artifacts(source_dir, data_dir)
    dest_crop_points = loaded["dest_crop_points"]

    top_regimes = build_top_regimes(dest_crop_points)
    destination_summary = build_destination_summary(dest_crop_points)
    country_summary = build_country_summary(dest_crop_points)

    top_regimes_csv = write_table(top_regimes, data_dir / "top_regimes.csv")
    top_n_regimes_csv = write_table(top_regimes.head(args.top_n), data_dir / f"top_{args.top_n}_regimes.csv")
    destination_summary_csv = write_table(destination_summary, data_dir / "destination_type_summary.csv")
    country_summary_csv = write_table(country_summary, data_dir / "country_summary.csv")

    plots = [
        plot_risk_concentration(top_regimes, figures_dir / "01_risk_concentration_top_regimes.png", args.top_n),
        plot_destination_structure(destination_summary, figures_dir / "02_destination_group_structure.png"),
        plot_regime_scatter(top_regimes, figures_dir / "03_regime_map_scatter.png"),
    ]

    manifest = {
        "source_dir": str(source_dir),
        "out_dir": str(out_dir),
        "top_n": args.top_n,
        "copied_source_artifacts": copied,
        "derived_tables": {
            "top_regimes_csv": top_regimes_csv,
            f"top_{args.top_n}_regimes_csv": top_n_regimes_csv,
            "destination_summary_csv": destination_summary_csv,
            "country_summary_csv": country_summary_csv,
        },
        "plots": plots,
        "headline_numbers": {
            "regime_count": int(len(top_regimes)),
            "country_count": int(top_regimes["country_code"].nunique()),
            "top_n_burden_share_percent": float(top_regimes.head(args.top_n)["annual_severe_burden_h"].sum() / top_regimes["annual_severe_burden_h"].sum() * 100.0),
            "top_country_by_burden": str(country_summary.iloc[0]["country_code"]) if not country_summary.empty else None,
            "top_destination_by_burden": str(destination_summary.iloc[0]["dest_type"]) if not destination_summary.empty else None,
        },
    }
    (out_dir / "experiment_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log(f"[done] source_dir={source_dir}")
    log(f"[done] out_dir={out_dir}")
    for plot in plots:
        log(f"[plot] {plot['path']}")


if __name__ == "__main__":
    main()
