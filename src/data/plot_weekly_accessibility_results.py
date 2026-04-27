"""Render weekly accessibility and factor-threshold PNGs from weekly scenario outputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.data.run_weekly_accessibility_pandana import _round_output_frame


matplotlib.use("Agg")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot weekly accessibility experiment outputs.")
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--top-factors", type=int, default=16)
    return parser.parse_args()


def _plot_access(
    summary: pd.DataFrame,
    out_path: Path,
    *,
    value_column: str,
    title: str,
    baseline_minutes: float | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(11.0, 5.8))
    for scenario in sorted(summary["scenario"].unique()):
        subset = summary.loc[summary["scenario"] == scenario].sort_values("week_start")
        ax.plot(subset["week_start"], subset[value_column], marker="o", label=scenario)
    if baseline_minutes is not None and np.isfinite(baseline_minutes):
        ax.axhline(float(baseline_minutes), color="#111111", linestyle="--", linewidth=1.4, label="baseline")
    ax.set_title(title)
    ax.set_xlabel("Week start")
    ax.set_ylabel("Minutes")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def _connected_summary(access: pd.DataFrame) -> pd.DataFrame:
    connected = access.loc[access["connected"] == True].copy()  # noqa: E712
    return (
        connected.groupby(["scenario", "week_start"], as_index=False)
        .agg(
            connected_median_access_minutes=("access_minutes", "median"),
            connected_mean_access_minutes=("access_minutes", "mean"),
            connected_n_origins=("origin_id", "count"),
        )
        .sort_values(["scenario", "week_start"])
        .reset_index(drop=True)
    )


def _plot_connectivity(summary: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11.0, 5.8))
    for scenario in sorted(summary["scenario"].unique()):
        subset = summary.loc[summary["scenario"] == scenario].sort_values("week_start")
        ax.plot(subset["week_start"], subset["connected_share"], marker="o", label=scenario)
    ax.set_title("Weekly Connected Origins Share")
    ax.set_xlabel("Week start")
    ax.set_ylabel("Connected share")
    ax.set_ylim(0, 1.05)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def _plot_factor_heatmap(factors: pd.DataFrame, out_path: Path, top_factors: int) -> list[str]:
    base = factors.loc[factors["scenario"] == "unknown_as_unpaved"].copy()
    if base.empty:
        return []
    base["share_triggered_unpaved_roads"] = pd.to_numeric(base["share_triggered_unpaved_roads"], errors="coerce").fillna(0.0)
    pivot_score = (
        base.groupby("factor", as_index=False)["share_triggered_unpaved_roads"]
        .mean()
        .sort_values("share_triggered_unpaved_roads", ascending=False)
    )
    keep = pivot_score["factor"].head(top_factors).tolist()
    heat = base[base["factor"].isin(keep)].copy()
    heat["factor_threshold"] = heat["factor"] + " | " + heat["threshold"]
    mat = (
        heat.pivot_table(
            index="factor_threshold",
            columns="week_start",
            values="share_triggered_unpaved_roads",
            aggfunc="mean",
            fill_value=0.0,
        )
        .sort_index()
        .sort_index(axis=1)
    )
    if mat.empty:
        fig, ax = plt.subplots(figsize=(12.0, 4.8))
        ax.text(0.5, 0.5, "No threshold activations", transform=ax.transAxes, ha="center", va="center")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(out_path, dpi=170)
        plt.close(fig)
        return keep

    fig_h = max(5.5, 0.35 * len(mat.index))
    fig, ax = plt.subplots(figsize=(12.0, fig_h))
    im = ax.imshow(mat.values, aspect="auto", cmap="YlOrRd", vmin=0.0, vmax=max(0.01, float(np.nanmax(mat.values))))
    ax.set_title("Weekly Factor/Threshold Activations (unknown_as_unpaved)")
    ax.set_xlabel("Week start")
    ax.set_ylabel("Factor | threshold")
    ax.set_xticks(np.arange(mat.shape[1]), labels=list(mat.columns), rotation=45, ha="right")
    ax.set_yticks(np.arange(mat.shape[0]), labels=list(mat.index))
    cbar = fig.colorbar(im, ax=ax, pad=0.01)
    cbar.set_label("Share of unpaved roads triggered")
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)
    return keep


def _plot_access_boxplot(access: pd.DataFrame, out_path: Path, *, connected_only: bool) -> None:
    data = access.copy()
    if connected_only:
        data = data.loc[data["connected"] == True].copy()  # noqa: E712
    data = data.sort_values(["scenario", "week_start"])

    scenarios = sorted(data["scenario"].dropna().unique().tolist())
    if not scenarios:
        fig, ax = plt.subplots(figsize=(11.0, 4.8))
        ax.text(0.5, 0.5, "No data for boxplot", transform=ax.transAxes, ha="center", va="center")
        ax.axis("off")
        fig.tight_layout()
        fig.savefig(out_path, dpi=170)
        plt.close(fig)
        return

    fig, axes = plt.subplots(len(scenarios), 1, figsize=(12.0, 4.6 + 3.2 * (len(scenarios) - 1)), sharex=True)
    if len(scenarios) == 1:
        axes = [axes]

    for ax, scenario in zip(axes, scenarios, strict=False):
        subset = data.loc[data["scenario"] == scenario].copy()
        weeks = sorted(subset["week_start"].dropna().unique().tolist())
        series: list[np.ndarray] = []
        labels: list[str] = []
        for week in weeks:
            vals = pd.to_numeric(subset.loc[subset["week_start"] == week, "access_minutes"], errors="coerce").dropna()
            if vals.empty:
                continue
            labels.append(week)
            series.append(vals.to_numpy(dtype="float64"))
        if not series:
            ax.text(0.5, 0.5, f"No values for {scenario}", transform=ax.transAxes, ha="center", va="center")
            ax.set_axis_off()
            continue
        ax.boxplot(series, labels=labels, showfliers=True)
        ax.set_title(scenario)
        ax.set_ylabel("Minutes")
        ax.grid(alpha=0.22)

    axes[-1].set_xlabel("Week start")
    fig.autofmt_xdate(rotation=45)
    suffix = "Connected Origins Only" if connected_only else "All Origins"
    fig.suptitle(f"Weekly Accessibility Distribution ({suffix})", y=0.995)
    fig.tight_layout()
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    summary_csv = args.results_dir / "weekly_summary.csv"
    factors_csv = args.results_dir / "weekly_factor_threshold_counts.csv"
    if not summary_csv.exists():
        raise FileNotFoundError(f"Missing {summary_csv}")
    if not factors_csv.exists():
        raise FileNotFoundError(f"Missing {factors_csv}")

    summary = pd.read_csv(summary_csv)
    factors = pd.read_csv(factors_csv)
    access_csv = args.results_dir / "weekly_accessibility.csv"
    if not access_csv.exists():
        raise FileNotFoundError(f"Missing {access_csv}")
    access = pd.read_csv(access_csv)
    baseline_csv = args.results_dir / "baseline_routes.csv"
    baseline = pd.read_csv(baseline_csv) if baseline_csv.exists() else pd.DataFrame()
    baseline_median = None
    baseline_connected_median = None
    if not baseline.empty:
        baseline_median = float(pd.to_numeric(baseline["access_minutes"], errors="coerce").median())
        connected = baseline.loc[baseline["connected"].astype(bool)].copy()
        if not connected.empty:
            baseline_connected_median = float(pd.to_numeric(connected["access_minutes"], errors="coerce").median())

    connected_summary = _connected_summary(access)
    _round_output_frame(connected_summary).to_csv(args.results_dir / "weekly_connected_summary.csv", index=False)

    _plot_access(
        summary,
        args.results_dir / "weekly_median_access_minutes.png",
        value_column="median_access_minutes",
        title="Weekly Median Accessibility, All Origins (disconnected = isolation minutes)",
        baseline_minutes=baseline_median,
    )
    _plot_access(
        connected_summary,
        args.results_dir / "weekly_connected_median_access_minutes.png",
        value_column="connected_median_access_minutes",
        title="Weekly Median Accessibility, Connected Origins Only",
        baseline_minutes=baseline_connected_median,
    )
    _plot_connectivity(summary, args.results_dir / "weekly_connected_share.png")
    top = _plot_factor_heatmap(factors, args.results_dir / "weekly_factor_threshold_heatmap.png", args.top_factors)
    _plot_access_boxplot(access, args.results_dir / "weekly_access_boxplot_all.png", connected_only=False)
    _plot_access_boxplot(access, args.results_dir / "weekly_access_boxplot_connected_only.png", connected_only=True)

    report = {
        "results_dir": str(args.results_dir),
        "png_outputs": {
            "weekly_median_access_minutes": str(args.results_dir / "weekly_median_access_minutes.png"),
            "weekly_connected_median_access_minutes": str(args.results_dir / "weekly_connected_median_access_minutes.png"),
            "weekly_connected_share": str(args.results_dir / "weekly_connected_share.png"),
            "weekly_factor_threshold_heatmap": str(args.results_dir / "weekly_factor_threshold_heatmap.png"),
            "weekly_access_boxplot_all": str(args.results_dir / "weekly_access_boxplot_all.png"),
            "weekly_access_boxplot_connected_only": str(args.results_dir / "weekly_access_boxplot_connected_only.png"),
        },
        "baseline_median_access_minutes": baseline_median,
        "baseline_connected_median_access_minutes": baseline_connected_median,
        "top_factors_in_heatmap": top,
    }
    (args.results_dir / "plot_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
