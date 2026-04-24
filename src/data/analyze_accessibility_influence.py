"""Build Q3 accessibility-change charts and influence summary for road scenarios."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


matplotlib.use("Agg")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create accessibility and influence charts from monthly road scenarios.")
    parser.add_argument("--country-code", type=str, default="GAB")
    parser.add_argument("--year", type=int, default=2024)
    parser.add_argument("--months", type=str, default="7,8,9")
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--scenario", type=str, default="unknown_as_unpaved")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _month_stamp(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _load_month(base_dir: Path, scenario: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    baseline = pd.read_csv(base_dir / "baseline_routes.csv")
    scen = pd.read_csv(base_dir / f"{scenario}_routes.csv")
    merged = baseline.merge(scen[["origin_id", "route_length_m", "connected"]], on="origin_id", suffixes=("_baseline", "_scenario"))
    merged["delta_length_m"] = merged["route_length_m_scenario"] - merged["route_length_m_baseline"]
    return baseline, merged


def _safe_median(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.median())


def _safe_mean(series: pd.Series) -> float | None:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.mean())


def _plot_connected(metrics: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    ax.plot(metrics["month"], metrics["baseline_connected_share"], marker="o", label="baseline")
    ax.plot(metrics["month"], metrics["unknown_as_paved_connected_share"], marker="o", label="unknown_as_paved")
    ax.plot(metrics["month"], metrics["unknown_as_unpaved_connected_share"], marker="o", label="unknown_as_unpaved")
    ax.set_title("Connected Origins Share By Month")
    ax.set_xlabel("Month")
    ax.set_ylabel("Connected share")
    ax.set_ylim(0, 1.05)
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def _plot_medians(metrics: pd.DataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    ax.plot(metrics["month"], metrics["baseline_median_length_km"], marker="o", label="baseline")
    ax.plot(metrics["month"], metrics["unknown_as_paved_median_length_km"], marker="o", label="unknown_as_paved")
    ax.plot(metrics["month"], metrics["unknown_as_unpaved_median_length_km"], marker="o", label="unknown_as_unpaved")
    ax.set_title("Median Route Length (Connected Origins)")
    ax.set_xlabel("Month")
    ax.set_ylabel("Distance (km)")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def _plot_delta_boxplot(month_to_delta: dict[str, pd.Series], out_path: Path) -> None:
    labels: list[str] = []
    values: list[np.ndarray] = []
    for month, series in month_to_delta.items():
        clean = pd.to_numeric(series, errors="coerce").dropna().to_numpy()
        if clean.size:
            labels.append(month)
            values.append(clean / 1000.0)

    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    if values:
        ax.boxplot(values, labels=labels, showfliers=True)
    else:
        ax.text(0.5, 0.5, "No connected-pair deltas", transform=ax.transAxes, ha="center", va="center")
    ax.set_title("Accessibility Change Distribution (Scenario - Baseline)")
    ax.set_xlabel("Month")
    ax.set_ylabel("Delta distance (km)")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def _plot_influence(scores: pd.DataFrame, out_path: Path) -> None:
    top = scores.sort_values("abs_score", ascending=False).head(12).copy()
    top = top.sort_values("score", ascending=True)
    fig, ax = plt.subplots(figsize=(10.0, 6.2))
    colors = ["#d73027" if value > 0 else "#4575b4" for value in top["score"]]
    ax.barh(top["feature"], top["score"], color=colors, alpha=0.9)
    ax.axvline(0, color="black", linewidth=1)
    ax.set_title("Top Feature Influence On Road Closure (Unknown As Unpaved)")
    ax.set_xlabel("Standardized mean diff (closed - open)")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    iso3 = args.country_code.upper()
    months = [int(value) for value in args.months.split(",") if value.strip()]

    monthly_rows: list[dict[str, object]] = []
    month_to_delta: dict[str, pd.Series] = {}
    scenario_pairs: dict[str, pd.DataFrame] = {}

    for month in months:
        stamp = _month_stamp(args.year, month)
        base = project_root / "outputs" / "road_scenarios" / iso3 / stamp
        baseline = pd.read_csv(base / "baseline_routes.csv")
        n_origins = int(len(baseline))
        row: dict[str, object] = {"month": stamp, "n_origins": n_origins}

        for scenario_name in ["unknown_as_paved", "unknown_as_unpaved"]:
            merged = _load_month(base, scenario_name)[1]
            scenario_pairs[f"{stamp}:{scenario_name}"] = merged
            connected_b = merged["connected_baseline"].fillna(False)
            connected_s = merged["connected_scenario"].fillna(False)
            both = connected_b & connected_s
            row[f"{scenario_name}_connected_share"] = float(connected_s.mean())
            row[f"{scenario_name}_median_length_km"] = (
                None
                if merged.loc[connected_s, "route_length_m_scenario"].dropna().empty
                else float(merged.loc[connected_s, "route_length_m_scenario"].median() / 1000.0)
            )
            row[f"{scenario_name}_median_delta_km"] = (
                None
                if merged.loc[both, "delta_length_m"].dropna().empty
                else float(merged.loc[both, "delta_length_m"].median() / 1000.0)
            )
            if scenario_name == args.scenario:
                month_to_delta[stamp] = merged.loc[both, "delta_length_m"]

        row["baseline_connected_share"] = float(baseline["connected"].fillna(False).mean())
        row["baseline_median_length_km"] = (
            None
            if baseline.loc[baseline["connected"], "route_length_m"].dropna().empty
            else float(baseline.loc[baseline["connected"], "route_length_m"].median() / 1000.0)
        )
        monthly_rows.append(row)

    metrics = pd.DataFrame(monthly_rows).sort_values("month")

    period_slug = f"{args.year}-Q3_pop{args.city_threshold}"
    out_dir = project_root / "outputs" / "accessibility_analysis" / iso3 / period_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics.to_csv(out_dir / "monthly_accessibility_metrics.csv", index=False)
    _plot_connected(metrics, out_dir / "connected_share_by_month.png")
    _plot_medians(metrics, out_dir / "median_route_length_by_month.png")
    _plot_delta_boxplot(month_to_delta, out_dir / f"{args.scenario}_delta_boxplot.png")

    overlay_path = (
        project_root
        / "outputs"
        / "road_multisource_overlay"
        / iso3
        / "2024-07-01_to_2024-09-30_7d"
        / "roads_with_multisource_overlay.gpkg"
    )
    overlay = gpd.read_file(overlay_path).drop(columns="geometry")
    allowed_prefixes = (
        "chirps_",
        "flood_",
        "landslide_",
        "gem_",
        "liquefaction_",
        "worldcover_",
        "soil_",
        "era5_",
        "cams_",
        "flopros_",
    )
    numeric_features = [
        col
        for col in overlay.columns
        if col != "road_row_id"
        and col.startswith(allowed_prefixes)
        and pd.api.types.is_numeric_dtype(overlay[col])
    ]

    closure_frames: list[pd.DataFrame] = []
    for month in months:
        stamp = _month_stamp(args.year, month)
        roads_path = project_root / "outputs" / "road_scenarios" / iso3 / stamp / f"{args.scenario}_road_status.gpkg"
        roads = gpd.read_file(roads_path)[["road_row_id", "closed"]]
        roads["month"] = stamp
        closure_frames.append(roads)
    closure = pd.concat(closure_frames, ignore_index=True)
    merged = closure.merge(overlay[["road_row_id", *numeric_features]], on="road_row_id", how="left")

    scores: list[dict[str, object]] = []
    closed_mask = merged["closed"].fillna(False)
    for feature in numeric_features:
        series = pd.to_numeric(merged[feature], errors="coerce")
        valid = series[np.isfinite(series)]
        if valid.empty:
            continue
        closed_vals = series[closed_mask & np.isfinite(series)]
        open_vals = series[(~closed_mask) & np.isfinite(series)]
        if closed_vals.empty or open_vals.empty:
            continue
        std = float(valid.std())
        if std == 0:
            continue
        score = float((closed_vals.mean() - open_vals.mean()) / std)
        scores.append(
            {
                "feature": feature,
                "score": score,
                "abs_score": abs(score),
                "closed_mean": float(closed_vals.mean()),
                "open_mean": float(open_vals.mean()),
            }
        )

    score_df = pd.DataFrame(scores).sort_values("abs_score", ascending=False)
    score_df.to_csv(out_dir / "feature_influence_scores.csv", index=False)
    _plot_influence(score_df, out_dir / "feature_influence_top12.png")

    summary = {
        "country_code": iso3,
        "months": [_month_stamp(args.year, month) for month in months],
        "city_population_threshold": args.city_threshold,
        "scenario_for_delta_plot": args.scenario,
        "baseline_connected_share_period_mean": _safe_mean(metrics["baseline_connected_share"]),
        "unknown_as_paved_connected_share_period_mean": _safe_mean(metrics["unknown_as_paved_connected_share"]),
        "unknown_as_unpaved_connected_share_period_mean": _safe_mean(metrics["unknown_as_unpaved_connected_share"]),
        "unknown_as_unpaved_median_delta_km_period_median": _safe_median(metrics["unknown_as_unpaved_median_delta_km"]),
        "top_influence_features": score_df.head(8).to_dict(orient="records"),
        "outputs": {
            "metrics_csv": str((out_dir / "monthly_accessibility_metrics.csv").relative_to(project_root)),
            "connected_png": str((out_dir / "connected_share_by_month.png").relative_to(project_root)),
            "median_png": str((out_dir / "median_route_length_by_month.png").relative_to(project_root)),
            "delta_boxplot_png": str((out_dir / f"{args.scenario}_delta_boxplot.png").relative_to(project_root)),
            "influence_png": str((out_dir / "feature_influence_top12.png").relative_to(project_root)),
            "influence_csv": str((out_dir / "feature_influence_scores.csv").relative_to(project_root)),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
