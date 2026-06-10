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
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
from scipy.stats import spearmanr


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAIN_MECH_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "paper_experiment_rainfall_mechanism_v1"
DEFAULT_ROUTE_MIX_CSV = ROOT / "outputs" / "astar_accessibility_weekly" / "base_route_surface_mix" / "by_od_surface.csv"
DEFAULT_ROUTE_PENALTY_CSV = ROOT / "outputs" / "astar_accessibility_weekly" / "route_change_diagnostics" / "route_surface_penalty_summary.csv"
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "paper_experiment_country_mechanism_structural_v1"
PREDICTOR_LABELS = {
    "log_threshold_impact": "Threshold-based rain impact",
    "log_remoteness_h": "Network remoteness",
    "actual_unpaved_time_share": "Actual unpaved share",
    "total_burden_h": "Burden",
    "threshold_impact_ratio_actual": "Threshold-based rain impact",
    "weighted_baseline_travel_time_h": "Network remoteness",
    "actual_unpaved_time_share_raw": "Actual unpaved share",
}
PREDICTOR_ORDER = [
    "log_threshold_impact",
    "log_remoteness_h",
    "actual_unpaved_time_share",
]
STAT_PREDICTOR_ORDER = [
    "log_threshold_impact",
    "log_remoteness_h",
    "actual_unpaved_time_share",
]
PREDICTOR_GROUPS = {
    "log_rain_total_mm": "Primary driver",
    "log_remoteness_h": "Network geometry",
    "unpaved_time_share": "Surface composition",
    "unknown_time_share": "Surface composition",
    "synthetic_time_share": "Surface composition",
}


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render country-level structural mechanism scan from existing outputs.")
    parser.add_argument("--rain-mech-dir", default=str(DEFAULT_RAIN_MECH_DIR))
    parser.add_argument("--route-mix-csv", default=str(DEFAULT_ROUTE_MIX_CSV))
    parser.add_argument("--route-penalty-csv", default=str(DEFAULT_ROUTE_PENALTY_CSV))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    return parser.parse_args()


def copy_inputs(rain_mech_dir: Path, route_mix_csv: Path, route_penalty_csv: Path, data_dir: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    for rel in [
        "experiment_manifest.json",
        "data/country_mechanism_summary.csv",
        "data/country_rainfall_summary.csv",
        "data/country_burden_summary.csv",
        "data/weekly_country_mechanism.csv",
        "data/source_visual_experiment_crop_points_cluster_weighted_by_dest.csv",
    ]:
        src = rain_mech_dir / rel
        dst = data_dir / f"source_{src.name}"
        shutil.copy2(src, dst)
        copied[rel] = str(dst)
    route_dst = data_dir / "source_by_od_surface.csv"
    shutil.copy2(route_mix_csv, route_dst)
    copied["base_route_surface_mix/by_od_surface.csv"] = str(route_dst)
    penalty_dst = data_dir / "source_route_surface_penalty_summary.csv"
    shutil.copy2(route_penalty_csv, penalty_dst)
    copied["route_change_diagnostics/route_surface_penalty_summary.csv"] = str(penalty_dst)
    return copied


def load_rain_mechanism_inputs(rain_mech_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    mech = pd.read_csv(rain_mech_dir / "data" / "country_mechanism_summary.csv")
    rain = pd.read_csv(rain_mech_dir / "data" / "country_rainfall_summary.csv")
    burden = pd.read_csv(rain_mech_dir / "data" / "country_burden_summary.csv")
    weekly_country = pd.read_csv(rain_mech_dir / "data" / "weekly_country_mechanism.csv")
    weekly_country["week_start"] = pd.to_datetime(weekly_country["week_start"])
    return mech, rain, burden, weekly_country


def load_penalty_rules(rain_mech_dir: Path) -> pd.DataFrame:
    path = rain_mech_dir / "data" / "source_weekly_rain_speed_penalty_rules.csv"
    if path.exists():
        return pd.read_csv(path)
    return pd.DataFrame(columns=["road_type", "min_weekly_mm", "max_weekly_mm", "speed_multiplier", "effect_label"])


def select_high_rain_contrast_weeks(subset: pd.DataFrame) -> dict[str, pd.Series]:
    if subset.empty:
        return {}
    rain_threshold = float(subset["median"].quantile(0.8))
    high_rain = subset[subset["median"] >= rain_threshold].copy()
    if high_rain.empty:
        high_rain = subset.copy()
    low_burden = high_rain.sort_values(["weekly_burden_h", "median", "week_start"], ascending=[True, False, True]).iloc[0]
    result = {
        "rain_threshold": pd.Series({"value": rain_threshold}),
        "high_rain_low_burden": low_burden,
    }
    high_burden = high_rain.sort_values(["weekly_burden_h", "median", "week_start"], ascending=[False, False, True]).iloc[0]
    same_week = pd.Timestamp(low_burden["week_start"]) == pd.Timestamp(high_burden["week_start"])
    same_values = float(low_burden["weekly_burden_h"]) == float(high_burden["weekly_burden_h"]) and float(low_burden["median"]) == float(high_burden["median"])
    if float(high_burden["weekly_burden_h"]) > 0.0 and not (same_week and same_values):
        result["high_rain_high_burden"] = high_burden
    return result


def aggregate_route_mix(frame: pd.DataFrame) -> pd.DataFrame:
    return aggregate_route_mix_by(frame, ["country_code"])


def aggregate_route_mix_by(frame: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    routes = frame.copy()
    route_keys = ["country_code", "crop_code", "candidate_rank", "dest_type", "dest_rank", "dest_id"]
    routes["route_key"] = routes[route_keys].astype(str).agg("|".join, axis=1)
    route_totals = (
        routes.groupby(key_cols + ["route_key"], dropna=False)
        .agg(
            route_weight=("cluster_cell_count", "first"),
            total_travel_time_h=("total_travel_time_h", "first"),
        )
        .reset_index()
    )
    surface = (
        routes.assign(
            unpaved_time_share=np.where(routes["surface_group"].eq("unpaved"), routes["surface_travel_time_pct"], 0.0),
            unknown_time_share=np.where(routes["surface_group"].eq("unknown"), routes["surface_travel_time_pct"], 0.0),
            synthetic_time_share=np.where(routes["surface_group"].astype(str).str.contains("synthetic", na=False), routes["surface_travel_time_pct"], 0.0),
            paved_time_share=np.where(routes["surface_group"].eq("paved"), routes["surface_travel_time_pct"], 0.0),
        )
        .groupby(key_cols + ["route_key"], dropna=False)
        .agg(
            unpaved_time_share=("unpaved_time_share", "sum"),
            unknown_time_share=("unknown_time_share", "sum"),
            synthetic_time_share=("synthetic_time_share", "sum"),
            paved_time_share=("paved_time_share", "sum"),
        )
        .reset_index()
    )
    route_summary = route_totals.merge(surface, on=key_cols + ["route_key"], how="left")

    def _weighted_mean(values: pd.Series, weights: pd.Series) -> float:
        clean = pd.DataFrame({"value": values, "weight": weights}).dropna()
        if clean.empty:
            return 0.0
        return float(np.average(clean["value"], weights=clean["weight"]))

    summary = (
        route_summary.groupby(key_cols, dropna=False)
        .apply(
            lambda g: pd.Series(
                {
                    "route_count": int(len(g)),
                    "weighted_baseline_travel_time_h": _weighted_mean(g["total_travel_time_h"], g["route_weight"]),
                    "unpaved_time_share": _weighted_mean(g["unpaved_time_share"], g["route_weight"]),
                    "unknown_time_share": _weighted_mean(g["unknown_time_share"], g["route_weight"]),
                    "synthetic_time_share": _weighted_mean(g["synthetic_time_share"], g["route_weight"]),
                    "paved_time_share": _weighted_mean(g["paved_time_share"], g["route_weight"]),
                }
            ),
            include_groups=False,
        )
        .reset_index()
    )
    return summary


def aggregate_actual_threshold_impact(frame: pd.DataFrame) -> pd.DataFrame:
    return aggregate_actual_threshold_impact_by(frame, ["country_code"])


def aggregate_actual_threshold_impact_by(frame: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    penalty = frame.copy()
    penalty = penalty[penalty["surface_group"].isin(["paved", "unpaved"])].copy()
    group_cols = key_cols + ["week_start", "dest_type", "surface_group"]
    per_surface = (
        penalty.groupby(group_cols, dropna=False)
        .agg(
            wet_time_h=("wet_weighted_time_h", "sum"),
            base_time_h=("base_weighted_time_h", "first"),
            affected_length_km=("affected_weighted_length_km", "sum"),
            base_length_km=("base_weighted_length_km", "first"),
        )
        .reset_index()
    )
    per_surface["time_increase_h"] = (per_surface["wet_time_h"] - per_surface["base_time_h"]).clip(lower=0.0)
    summary = (
        per_surface.groupby(key_cols, dropna=False)
        .agg(
            actual_threshold_time_increase_h=("time_increase_h", "sum"),
            actual_base_time_h=("base_time_h", "sum"),
            actual_penalized_length_km=("affected_length_km", "sum"),
            actual_base_length_km=("base_length_km", "sum"),
        )
        .reset_index()
    )
    summary["threshold_impact_ratio_actual"] = np.where(
        summary["actual_base_time_h"] > 0,
        summary["actual_threshold_time_increase_h"] / summary["actual_base_time_h"],
        0.0,
    )
    summary["penalized_length_share_actual"] = np.where(
        summary["actual_base_length_km"] > 0,
        summary["actual_penalized_length_km"] / summary["actual_base_length_km"],
        0.0,
    )
    return summary


def build_country_crop_burden_summary(dest_crop_points: pd.DataFrame) -> pd.DataFrame:
    burden = dest_crop_points.copy()
    return (
        burden.groupby(["country_code", "crop_code"], dropna=False)
        .agg(
            crop_burden_h=("annual_severe_burden_h", "sum"),
            crop_peak_delay_h=("peak_delay_h", "max"),
            crop_dest_groups=("dest_type", "nunique"),
            crop_total_cluster_weight=("total_cluster_weight", "sum"),
            crop_affected_cluster_weight=("affected_cluster_weight", "sum"),
            crop_max_affected_weeks=("affected_weeks", "max"),
        )
        .reset_index()
    )


def build_country_crop_mechanism_frame(
    dest_crop_points: pd.DataFrame,
    route_mix: pd.DataFrame,
    route_penalty: pd.DataFrame,
) -> pd.DataFrame:
    burden = build_country_crop_burden_summary(dest_crop_points)
    route_mix_summary = aggregate_route_mix_by(route_mix, ["country_code", "crop_code"])
    threshold_summary = aggregate_actual_threshold_impact_by(route_penalty, ["country_code", "crop_code"])
    frame = burden.merge(route_mix_summary, on=["country_code", "crop_code"], how="left").merge(
        threshold_summary,
        on=["country_code", "crop_code"],
        how="left",
    )
    frame["threshold_impact_ratio_actual"] = frame["threshold_impact_ratio_actual"].fillna(0.0)
    frame["actual_unpaved_time_share"] = frame["unpaved_time_share"].fillna(0.0)
    frame["log_crop_burden_h"] = np.log1p(frame["crop_burden_h"])
    frame["log_threshold_impact"] = np.log1p(frame["threshold_impact_ratio_actual"])
    frame["log_remoteness_h"] = np.log1p(frame["weighted_baseline_travel_time_h"])
    return frame


def crop_stat_summary(frame: pd.DataFrame, *, n_boot: int = 1000) -> pd.DataFrame:
    rows = []
    predictors = ["log_threshold_impact", "log_remoteness_h", "actual_unpaved_time_share"]
    for crop_idx, (crop, sub) in enumerate(frame.groupby("crop_code", dropna=False)):
        valid = sub.dropna(subset=["log_crop_burden_h"]).copy()
        row: dict[str, object] = {
            "crop_code": crop,
            "n_country_crop": int(len(valid)),
            "total_crop_burden_h": float(valid["crop_burden_h"].sum()),
            "median_crop_burden_h": float(valid["crop_burden_h"].median()),
        }
        for pred_idx, predictor in enumerate(predictors):
            data = valid[[predictor, "log_crop_burden_h"]].dropna()
            if len(data) >= 6:
                stat = bootstrap_spearman_summary(
                    data.rename(columns={"log_crop_burden_h": "target"}),
                    predictor,
                    "target",
                    n_boot=n_boot,
                    seed=500 + crop_idx * 10 + pred_idx,
                )
                row[f"rho_{predictor}"] = float(stat["rho"])
                row[f"rho_{predictor}_ci_low"] = float(stat["ci_low"])
                row[f"rho_{predictor}_ci_high"] = float(stat["ci_high"])
                row[f"rho_{predictor}_supported"] = bool(stat["supported"])
            else:
                row[f"rho_{predictor}"] = np.nan
                row[f"rho_{predictor}_ci_low"] = np.nan
                row[f"rho_{predictor}_ci_high"] = np.nan
                row[f"rho_{predictor}_supported"] = False
        model_data = valid.dropna(subset=predictors + ["log_crop_burden_h"])
        if len(model_data) >= 6:
            model = run_standardized_regression(model_data, "log_crop_burden_h", predictors)
            row["model_r_squared"] = float(model["r_squared"])
            for item in model["coefficients"]:
                row[f"beta_{item['predictor']}"] = float(item["beta"])
        rows.append(row)
    return pd.DataFrame(rows).sort_values("total_crop_burden_h", ascending=False)


def run_standardized_regression(frame: pd.DataFrame, target: str, predictors: list[str]) -> dict[str, object]:
    keep_cols = ["country_code"] + [target] + predictors if "country_code" in frame.columns else [target] + predictors
    data = frame[keep_cols].dropna().copy()
    numeric = data[[target] + predictors].copy()
    z = (numeric - numeric.mean()) / numeric.std(ddof=0)
    y = z[target].to_numpy(dtype=float)
    x = z[predictors].to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    fitted = x @ coef
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    n = len(data)
    p = len(predictors)
    adjusted_r_squared = 1.0 - (1.0 - r_squared) * (n - 1) / (n - p - 1) if n > p + 1 else np.nan
    result = data.copy()
    result["fitted_z"] = fitted
    result["residual_z"] = y - fitted
    coefficients = [{"predictor": predictor, "beta": float(beta)} for predictor, beta in zip(predictors, coef[1:])]
    return {
        "predictors": predictors,
        "coefficients": coefficients,
        "intercept": float(coef[0]),
        "r_squared": r_squared,
        "adjusted_r_squared": float(adjusted_r_squared) if np.isfinite(adjusted_r_squared) else None,
        "n_obs": n,
        "fitted_frame": result,
    }


def bootstrap_spearman_summary(
    frame: pd.DataFrame,
    x_col: str,
    y_col: str,
    *,
    n_boot: int = 2000,
    seed: int = 42,
) -> dict[str, float | str]:
    data = frame[[x_col, y_col]].dropna().copy()
    x = data[x_col].to_numpy(dtype=float)
    y = data[y_col].to_numpy(dtype=float)
    rho = float(spearmanr(x, y).statistic)
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(data), len(data))
        stat = spearmanr(x[idx], y[idx]).statistic
        draws.append(float(stat if np.isfinite(stat) else 0.0))
    ci_low, ci_high = np.percentile(draws, [2.5, 97.5])
    return {
        "predictor": x_col,
        "rho": rho,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "supported": bool(ci_low > 0 or ci_high < 0),
    }


def bootstrap_standardized_coefficients(
    frame: pd.DataFrame,
    target: str,
    predictors: list[str],
    *,
    n_boot: int = 2000,
    seed: int = 42,
) -> pd.DataFrame:
    data = frame[["country_code", target] + predictors].dropna().copy()
    rng = np.random.default_rng(seed)
    draws: list[dict[str, float]] = []
    for _ in range(n_boot):
        sample = data.iloc[rng.integers(0, len(data), len(data))].reset_index(drop=True)
        result = run_standardized_regression(sample, target, predictors)
        row = {item["predictor"]: float(item["beta"]) for item in result["coefficients"]}
        draws.append(row)
    return pd.DataFrame(draws)


def summarize_bootstrap_coefficients(model: dict[str, object], boot: pd.DataFrame) -> list[dict[str, object]]:
    coef_map = {row["predictor"]: float(row["beta"]) for row in model["coefficients"]}
    rows = []
    for predictor, beta in coef_map.items():
        values = boot[predictor].dropna().to_numpy(dtype=float)
        ci_low, ci_high = np.percentile(values, [2.5, 97.5])
        sign_prob = float(max(np.mean(values > 0), np.mean(values < 0)))
        rows.append(
            {
                "predictor": predictor,
                "beta": beta,
                "ci_low": float(ci_low),
                "ci_high": float(ci_high),
                "supported": bool(ci_low > 0 or ci_high < 0),
                "sign_prob": sign_prob,
            }
        )
    return rows


def build_full_country_frame(
    country_mechanism: pd.DataFrame,
    route_mix_summary: pd.DataFrame,
    threshold_impact_summary: pd.DataFrame,
) -> pd.DataFrame:
    frame = country_mechanism.merge(route_mix_summary, on="country_code", how="left").merge(
        threshold_impact_summary,
        on="country_code",
        how="left",
    )
    frame["log_burden_h"] = np.log1p(frame["total_burden_h"])
    frame["threshold_impact_ratio_actual"] = frame["threshold_impact_ratio_actual"].fillna(0.0)
    frame["penalized_length_share_actual"] = frame["penalized_length_share_actual"].fillna(0.0)
    frame["actual_unpaved_time_share"] = frame["unpaved_time_share"].fillna(0.0)
    frame["log_threshold_impact"] = np.log1p(frame["threshold_impact_ratio_actual"])
    frame["log_remoteness_h"] = np.log1p(frame["weighted_baseline_travel_time_h"])
    return frame


def correlation_table(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return frame[columns].corr(method="spearman")


def plot_driver_scatter_summary(
    full: pd.DataFrame,
    out_path: Path,
    *,
    n_obs: int,
) -> dict[str, object]:
    plotted = full.copy()
    plotted["unpaved_share_pct"] = plotted["actual_unpaved_time_share"] * 100.0
    plotted["threshold_impact_pct"] = plotted["threshold_impact_ratio_actual"] * 100.0
    plotted["log_burden_h"] = np.log1p(plotted["total_burden_h"])
    panels = [
        ("threshold_impact_pct", "Threshold-based rain impact", "Route-weighted travel-time increase from precipitation thresholds, %", "#3182bd"),
        ("weighted_baseline_travel_time_h", "Network remoteness", "Weighted baseline travel time, hours", "#e6550d"),
        ("unpaved_share_pct", "Surface composition", "Actual unpaved route share, %", "#31a354"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(17.0, 6.2), sharey=True)
    label_rows = plotted.nlargest(6, "total_burden_h")
    y_max = float(plotted["log_burden_h"].max() * 1.08) if not plotted.empty else 1.0
    for ax, (x_col, title, xlabel, color) in zip(axes, panels):
        x = plotted[x_col].to_numpy(dtype=float)
        y = plotted["log_burden_h"].to_numpy(dtype=float)
        rho = float(pd.Series(x).corr(pd.Series(y), method="spearman"))
        ax.scatter(x, y, s=58, color=color, alpha=0.78, edgecolor="#111111", linewidth=0.45)
        if len(plotted) >= 2 and np.isfinite(x).all() and np.isfinite(y).all() and np.unique(x).size >= 2:
            coeffs = np.polyfit(x, y, deg=1)
            x_line = np.linspace(float(np.min(x)), float(np.max(x)), 100)
            y_line = coeffs[0] * x_line + coeffs[1]
            ax.plot(x_line, y_line, color="#444444", linewidth=1.3, linestyle="--")
        for row in label_rows.itertuples(index=False):
            ax.annotate(row.country_code, (getattr(row, x_col), row.log_burden_h), xytext=(4, 4), textcoords="offset points", fontsize=8)
        ax.set_title(f"{title}\nSpearman rho={rho:.2f}", fontsize=11)
        ax.set_xlabel(xlabel, fontsize=9)
        ax.set_ylim(0, y_max)
        ax.grid(True, color="#e6e6e6", linewidth=0.8)
    axes[0].set_ylabel("Accessibility burden, log1p(hours)", fontsize=10)
    fig.suptitle("All-country view: burden against the main physical and network factors", y=0.97, fontsize=13)
    fig.text(
        0.06,
        0.04,
        fill(
            f"Each point is one country. The left panel shows the route-level impact implied by the project precipitation thresholds on actual paved/unpaved segments; the middle and right panels show two OD-network factors. "
            f"Y uses log1p(total severe burden hours) so low-burden and high-burden countries remain visible on the same plot. "
            f"Labels mark the six highest-burden countries. n={n_obs}.",
            width=175,
        ),
        ha="left",
        fontsize=9,
    )
    fig.subplots_adjust(left=0.07, right=0.98, top=0.84, bottom=0.17, wspace=0.20)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "points": int(len(plotted))}


def plot_model_residuals(fitted_frame: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = fitted_frame.copy().sort_values("residual_z", ascending=False)
    fig, ax = plt.subplots(figsize=(12.5, 8.5))
    ax.scatter(plotted["fitted_z"], plotted["residual_z"], s=90, c="#666666", edgecolor="#111111", linewidth=0.45, alpha=0.82)
    for row in pd.concat([plotted.head(5), plotted.tail(5)]).drop_duplicates("country_code").itertuples(index=False):
        ax.annotate(row.country_code, (row.fitted_z, row.residual_z), xytext=(4, 4), textcoords="offset points", fontsize=8)
    ax.axhline(0, color="#111111", linewidth=1.0)
    ax.set_xlabel("Model fitted value (z-score)")
    ax.set_ylabel("Residual (z-score)")
    ax.set_title("Model residuals: which countries are more or less disrupted than predicted")
    ax.grid(True, color="#e6e6e6", linewidth=0.8)
    fig.text(0.08, 0.04, "Positive residuals = higher burden than predicted by the selected predictors. Negative residuals = lower than predicted.", ha="left", fontsize=9)
    fig.subplots_adjust(left=0.08, right=0.97, top=0.90, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path)}


def plot_rain_delay_quadrants(full: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = full.copy()
    plotted["threshold_impact_pct"] = plotted["threshold_impact_ratio_actual"] * 100.0
    plotted["threshold_impact_log_pct"] = np.log1p(plotted["threshold_impact_pct"])
    plotted["burden_log"] = np.log1p(plotted["total_burden_h"])
    plotted["remoteness_size"] = 50.0 + 12.0 * plotted["weighted_baseline_travel_time_h"].clip(lower=0)
    x_cut = float(plotted["threshold_impact_log_pct"].median())
    y_cut = float(plotted["burden_log"].median())

    fig, ax = plt.subplots(figsize=(11.5, 8.0))
    scatter = ax.scatter(
        plotted["threshold_impact_log_pct"],
        plotted["burden_log"],
        s=plotted["remoteness_size"],
        c=plotted["weighted_baseline_travel_time_h"],
        cmap="YlOrRd",
        alpha=0.82,
        edgecolor="#111111",
        linewidth=0.55,
    )
    ax.axvline(x_cut, color="#333333", linewidth=1.2, linestyle="--")
    ax.axhline(y_cut, color="#333333", linewidth=1.2, linestyle="--")

    box = {"facecolor": "white", "alpha": 0.78, "edgecolor": "none", "boxstyle": "round,pad=0.20"}
    ax.text(0.98, 0.75, "high rain penalty\nhigh severe delay", transform=ax.transAxes, ha="right", va="top", fontsize=10, weight="bold", bbox=box)
    ax.text(0.98, 0.14, "high rain penalty\nlow severe delay", transform=ax.transAxes, ha="right", va="bottom", fontsize=10, bbox=box)
    ax.text(0.03, 0.98, "low rain penalty\nhigh severe delay", transform=ax.transAxes, ha="left", va="top", fontsize=10, bbox=box)
    ax.text(0.03, 0.08, "low rain penalty\nlow severe delay", transform=ax.transAxes, ha="left", va="bottom", fontsize=10, bbox=box)

    label_codes = set(plotted.nlargest(7, "total_burden_h")["country_code"])
    label_codes.update(plotted.nlargest(3, "threshold_impact_pct")["country_code"])
    label_codes.update(plotted.nsmallest(2, "total_burden_h")["country_code"])
    label_offsets = {
        "PNG": (6, 6),
        "MYS": (6, 8),
        "LBR": (6, -10),
        "COL": (6, 6),
        "SUR": (6, 6),
        "VEN": (6, 6),
        "BRN": (6, 6),
    }
    for row in plotted[plotted["country_code"].isin(label_codes)].itertuples(index=False):
        ax.annotate(
            row.country_code,
            (row.threshold_impact_log_pct, row.burden_log),
            xytext=label_offsets.get(row.country_code, (5, 5)),
            textcoords="offset points",
            fontsize=8,
        )

    cbar = fig.colorbar(scatter, ax=ax, pad=0.015)
    cbar.set_label("Baseline route time, hours")
    size_values = [5, 15, 25]
    size_handles = [
        ax.scatter([], [], s=50.0 + 12.0 * value, color="#bbbbbb", edgecolor="#111111", linewidth=0.55, alpha=0.82)
        for value in size_values
    ]
    ax.legend(
        size_handles,
        [f"{value}h baseline route time" for value in size_values],
        title="Point size",
        loc="lower right",
        frameon=True,
        fontsize=8,
        title_fontsize=9,
    )
    tick_pct = np.array([0, 10, 25, 50, 100, 200, 400], dtype=float)
    ax.set_xticks(np.log1p(tick_pct))
    ax.set_xticklabels([f"{v:.0f}" for v in tick_pct])
    ax.set_xlabel("Rain-threshold travel-time penalty, % of baseline route time")
    ax.set_ylabel("Annual severe accessibility delay, log1p(hours)")
    ax.set_title("Rain penalties translate into severe delay only in some network settings")
    ax.grid(True, color="#e8e8e8", linewidth=0.8)
    caption = (
        "Each point is one country. X = total extra route travel time caused by project rainfall-threshold speed "
        "penalties divided by total baseline route time; only actual paved and unpaved road segments are included. "
        "Y = annual severe accessibility delay, accumulated hours above the 3-hour delay threshold. Dashed lines are "
        "cross-country medians; the x-axis is log-scaled only to keep outliers visible."
    )
    fig.text(0.08, 0.04, fill(caption, width=145), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.09, right=0.92, top=0.90, bottom=0.15)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "x_median_log_pct": x_cut, "y_median": y_cut}


def plot_mechanism_ladder(full: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = full.sort_values("total_burden_h", ascending=False).copy()
    columns = [
        ("threshold_impact_ratio_actual", "Rain threshold\nimpact"),
        ("weighted_baseline_travel_time_h", "Network\nremoteness"),
        ("actual_unpaved_time_share", "Unpaved route\nshare"),
        ("total_burden_h", "Accessibility\nburden"),
    ]
    matrix = []
    text_values = []
    for col, _ in columns:
        values = plotted[col].to_numpy(dtype=float)
        lo = float(np.nanmin(values))
        hi = float(np.nanmax(values))
        scaled = (values - lo) / (hi - lo) if hi > lo else np.zeros_like(values)
        matrix.append(scaled)
        if col == "threshold_impact_ratio_actual":
            text_values.append([f"{v * 100:.0f}%" for v in values])
        elif col == "weighted_baseline_travel_time_h":
            text_values.append([f"{v:.0f}h" for v in values])
        elif col == "actual_unpaved_time_share":
            text_values.append([f"{v * 100:.0f}%" for v in values])
        else:
            text_values.append([f"{v / 1000:.1f}k" if v >= 1000 else f"{v:.0f}" for v in values])
    heat = np.column_stack(matrix)

    fig_height = max(8.0, 0.33 * len(plotted) + 1.8)
    fig, ax = plt.subplots(figsize=(9.8, fig_height))
    image = ax.imshow(heat, aspect="auto", cmap="YlOrRd", vmin=0, vmax=1)
    ax.set_yticks(np.arange(len(plotted)))
    ax.set_yticklabels(plotted["country_code"], fontsize=8)
    ax.set_xticks(np.arange(len(columns)))
    ax.set_xticklabels([label for _, label in columns], fontsize=9)
    ax.tick_params(top=True, bottom=False, labeltop=True, labelbottom=False)
    for y in range(heat.shape[0]):
        for x in range(heat.shape[1]):
            color = "white" if heat[y, x] > 0.58 else "#111111"
            ax.text(x, y, text_values[x][y], ha="center", va="center", fontsize=7, color=color)
    ax.set_title("Burden emerges where rain-threshold impact aligns with OD-network structure", pad=34, fontsize=13)
    ax.set_xlabel("")
    ax.set_ylabel("Countries sorted by total severe burden")
    cbar = fig.colorbar(image, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("Within-column normalized value")
    caption = (
        "Rows are countries. Values are shown in original units, while color is normalized within each column. "
        "The pattern to look for is co-occurrence: high burden concentrates where threshold impact and remoteness are both high."
    )
    fig.text(0.07, 0.03, fill(caption, width=120), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.12, right=0.91, top=0.86, bottom=0.10)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "rows": int(len(plotted))}


def plot_residual_case_bars(fitted_frame: pd.DataFrame, model: dict[str, object], out_path: Path) -> dict[str, object]:
    plotted = fitted_frame.sort_values("residual_z", ascending=True).copy()
    colors = np.where(plotted["residual_z"] >= 0, "#d7301f", "#3182bd")
    fig, ax = plt.subplots(figsize=(11.0, 9.0))
    ax.barh(plotted["country_code"], plotted["residual_z"], color=colors, alpha=0.86)
    ax.axvline(0, color="#111111", linewidth=1.0)
    for row in plotted.itertuples(index=False):
        if abs(float(row.residual_z)) >= 0.65:
            ha = "left" if row.residual_z >= 0 else "right"
            dx = 0.035 if row.residual_z >= 0 else -0.035
            ax.text(row.residual_z + dx, row.country_code, f"{row.residual_z:+.2f}", va="center", ha=ha, fontsize=8)
    r2 = float(model["r_squared"])
    adj = model["adjusted_r_squared"]
    adj_text = f"{float(adj):.2f}" if adj is not None else "NA"
    ax.set_title(f"Model residual cases after threshold impact and network controls  R²={r2:.2f} adj={adj_text}")
    ax.set_xlabel("Residual burden, z-score")
    ax.set_ylabel("Country")
    ax.grid(True, axis="x", color="#e6e6e6", linewidth=0.8)
    caption = (
        "Red bars are countries with more burden than predicted by the three-factor model; blue bars have less. "
        "These cases are useful for interpretation and for deciding where additional OD/corridor-level mechanisms are needed."
    )
    fig.text(0.08, 0.035, fill(caption, width=135), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.11, right=0.97, top=0.91, bottom=0.12)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path)}


def plot_crop_burden_composition(country_crop: pd.DataFrame, out_path: Path, *, top_country_count: int = 12) -> dict[str, object]:
    top_countries = (
        country_crop.groupby("country_code", dropna=False)["crop_burden_h"]
        .sum()
        .sort_values(ascending=False)
        .head(top_country_count)
        .index.tolist()
    )
    pivot = (
        country_crop[country_crop["country_code"].isin(top_countries)]
        .pivot_table(index="country_code", columns="crop_code", values="crop_burden_h", aggfunc="sum", fill_value=0.0)
        .loc[top_countries]
    )
    crop_order = [crop for crop in ["banana", "plantain", "pineapple", "mango", "avocado"] if crop in pivot.columns]
    pivot = pivot[crop_order]
    totals = pivot.sum(axis=1)
    shares = pivot.div(totals.replace(0, np.nan), axis=0).fillna(0.0) * 100.0
    colors = {
        "avocado": "#41b6c4",
        "banana": "#ffd92f",
        "mango": "#f768a1",
        "pineapple": "#7adf00",
        "plantain": "#9b51e0",
    }
    fig, ax = plt.subplots(figsize=(12.5, 7.0))
    left = np.zeros(len(shares))
    y = np.arange(len(shares))
    for crop in shares.columns:
        values = shares[crop].to_numpy(dtype=float)
        ax.barh(y, values, left=left, color=colors.get(crop, "#999999"), edgecolor="white", linewidth=0.5, label=crop)
        left += values
    for idx, total in enumerate(totals.to_numpy(dtype=float)):
        label = f"{total / 1000:.1f}k h" if total >= 1000 else f"{total:.0f} h"
        ax.text(101.5, idx, label, va="center", ha="left", fontsize=8)
    ax.set_yticks(y)
    ax.set_yticklabels(shares.index)
    ax.invert_yaxis()
    ax.set_xlim(0, 116)
    ax.set_xlabel("Share of country annual severe delay, %")
    ax.set_title("Crop composition of severe accessibility delay differs by country")
    ax.legend(title="Crop", ncols=len(pivot.columns), loc="lower center", bbox_to_anchor=(0.5, -0.20))
    ax.grid(True, axis="x", color="#e6e6e6", linewidth=0.8)
    caption = "Each country sums to 100%. Labels on the right show total annual severe delay hours, preserving scale without making it the x-axis."
    fig.text(0.08, 0.04, fill(caption, width=130), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.10, right=0.98, top=0.90, bottom=0.22)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "countries": top_countries}


def plot_crop_threshold_scatter(country_crop: pd.DataFrame, out_path: Path) -> dict[str, object]:
    crops = [crop for crop in ["banana", "plantain", "pineapple", "mango", "avocado"] if crop in set(country_crop["crop_code"])]
    ncols = len(crops)
    fig, axes = plt.subplots(1, ncols, figsize=(4.1 * ncols, 6.2), sharey=True)
    axes_flat = np.atleast_1d(axes).ravel()
    plotted = country_crop.copy()
    plotted["threshold_impact_pct"] = plotted["threshold_impact_ratio_actual"] * 100.0
    plotted["threshold_impact_log_pct"] = np.log1p(plotted["threshold_impact_pct"])
    y_max = float(plotted["log_crop_burden_h"].max() * 1.07) if not plotted.empty else 1.0
    tick_pct = np.array([0, 10, 25, 50, 100, 200, 400], dtype=float)
    for ax, crop in zip(axes_flat, crops):
        sub = plotted[plotted["crop_code"].eq(crop)].copy()
        scatter = ax.scatter(
            sub["threshold_impact_log_pct"],
            sub["log_crop_burden_h"],
            s=45 + 8 * sub["weighted_baseline_travel_time_h"].fillna(0),
            c=sub["weighted_baseline_travel_time_h"],
            cmap="YlOrRd",
            alpha=0.80,
            edgecolor="#111111",
            linewidth=0.45,
        )
        rho = sub["log_threshold_impact"].corr(sub["log_crop_burden_h"], method="spearman")
        for row in sub.nlargest(3, "crop_burden_h").itertuples(index=False):
            ax.annotate(row.country_code, (row.threshold_impact_log_pct, row.log_crop_burden_h), xytext=(4, 4), textcoords="offset points", fontsize=7)
        ax.set_title(f"{crop}\nrho={rho:.2f}", fontsize=10)
        ax.set_xticks(np.log1p(tick_pct))
        ax.set_xticklabels([f"{v:.0f}" for v in tick_pct], rotation=45, fontsize=7)
        ax.set_ylim(0, y_max)
        ax.grid(True, color="#e9e9e9", linewidth=0.8)
        ax.set_xlabel("threshold impact, %")
    axes_flat[0].set_ylabel("Crop burden, log1p(hours)")
    cbar_ax = fig.add_axes([0.94, 0.23, 0.012, 0.57])
    cbar = fig.colorbar(scatter, cax=cbar_ax)
    cbar.set_label("Weighted baseline travel time, hours")
    fig.suptitle("Crop-specific rain-to-burden relationship", y=0.98, fontsize=13)
    caption = "Each point is one country-crop pair. X uses the same project precipitation-threshold impact as the country analysis, recomputed within crop routes."
    fig.text(0.06, 0.04, fill(caption, width=160), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.06, right=0.91, top=0.84, bottom=0.18, wspace=0.28)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "crops": crops}


def plot_crop_stat_heatmap(stats: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = stats.sort_values("total_crop_burden_h", ascending=False).copy()
    columns = [
        ("rho_log_threshold_impact", "rho rain\nimpact"),
        ("rho_log_remoteness_h", "rho\nremote"),
        ("rho_actual_unpaved_time_share", "rho\nunpaved"),
    ]
    heat_cols = []
    text_cols = []
    for col, _ in columns:
        values = plotted[col].to_numpy(dtype=float)
        if col.startswith("rho_"):
            scaled = (values + 1.0) / 2.0
            support_col = f"{col}_supported"
            supported = plotted[support_col].tolist() if support_col in plotted.columns else [False] * len(plotted)
            text = [f"{v:.2f}{'*' if ok else ''}" for v, ok in zip(values, supported)]
        heat_cols.append(scaled)
        text_cols.append(text)
    heat = np.column_stack(heat_cols)
    fig, ax = plt.subplots(figsize=(8.8, 4.8))
    image = ax.imshow(heat, aspect="auto", cmap="RdYlBu_r", vmin=0, vmax=1)
    ax.set_yticks(np.arange(len(plotted)))
    ax.set_yticklabels(plotted["crop_code"])
    ax.set_xticks(np.arange(len(columns)))
    ax.set_xticklabels([label for _, label in columns])
    ax.tick_params(top=True, bottom=False, labeltop=True, labelbottom=False)
    for y in range(heat.shape[0]):
        for x in range(heat.shape[1]):
            col = columns[x][0]
            support_col = f"{col}_supported"
            if col.startswith("rho_") and support_col in plotted.columns and not bool(plotted.iloc[y][support_col]):
                ax.add_patch(Rectangle((x - 0.5, y - 0.5), 1.0, 1.0, facecolor="#d9d9d9", edgecolor="#ffffff", linewidth=0.6, zorder=2))
            color = "white" if heat[y, x] > 0.72 or heat[y, x] < 0.18 else "#111111"
            if col.startswith("rho_") and support_col in plotted.columns and not bool(plotted.iloc[y][support_col]):
                color = "#555555"
            ax.text(x, y, text_cols[x][y], ha="center", va="center", fontsize=9, color=color, zorder=3)
    ax.set_title("Crop-level correlations with severe accessibility delay")
    cbar = fig.colorbar(image, ax=ax, fraction=0.04, pad=0.03)
    cbar.set_label("Normalized display scale")
    caption = (
        "Rows are crops; columns are explanatory factors. Each cell is Spearman rho: the rank correlation between "
        "that factor and annual severe accessibility delay across country-crop pairs for the crop. Positive rho means "
        "higher factor values tend to coincide with higher severe delay. Gray cells are not statistically supported; "
        "asterisk means bootstrap 95% CI excludes zero."
    )
    fig.text(0.08, 0.05, fill(caption, width=105), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.12, right=0.90, top=0.78, bottom=0.20)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "crops": plotted["crop_code"].tolist()}


def plot_crop_model_fit(stats: pd.DataFrame, out_path: Path) -> dict[str, object]:
    plotted = stats.sort_values("model_r_squared", ascending=True).copy()
    fig, ax = plt.subplots(figsize=(8.0, 4.6))
    colors = np.where(plotted["model_r_squared"] >= 0.70, "#d7301f", "#fdae61")
    ax.barh(plotted["crop_code"], plotted["model_r_squared"], color=colors, alpha=0.88)
    ax.set_xlim(0, 1.0)
    ax.set_xlabel("Model R²")
    ax.set_ylabel("Crop")
    ax.set_title("Crop-specific model fit")
    ax.grid(True, axis="x", color="#e6e6e6", linewidth=0.8)
    for row in plotted.itertuples(index=False):
        ax.text(row.model_r_squared + 0.02, row.crop_code, f"{row.model_r_squared:.2f}", va="center", ha="left", fontsize=9)
    caption = "Each crop model uses the same three predictors: rain-threshold impact, remoteness, and actual unpaved share."
    fig.text(0.10, 0.05, fill(caption, width=95), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.14, right=0.94, top=0.84, bottom=0.20)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "crops": plotted["crop_code"].tolist()}


def plot_temporal_country_small_multiples(
    weekly_country: pd.DataFrame,
    full: pd.DataFrame,
    penalty_rules: pd.DataFrame,
    out_path: Path,
    top_country_count: int | None = None,
) -> dict[str, object]:
    count = len(full) if top_country_count is None else min(int(top_country_count), len(full))
    countries = full.nlargest(count, "total_burden_h")["country_code"].tolist()
    plotted = weekly_country[weekly_country["country_code"].isin(countries)].copy()
    threshold_values = sorted({float(v) for v in penalty_rules["min_weekly_mm"].dropna().tolist() if float(v) > 0})
    global_rain_max = max(
        float(plotted["median"].max() * 1.05) if not plotted.empty else 0.0,
        threshold_values[-1] if threshold_values else 0.0,
        320.0,
    )
    global_burden_max = max(float(plotted["weekly_burden_h"].max() * 1.05) if not plotted.empty else 0.0, 1.0)
    ncols = 4 if len(countries) > 12 else 2
    nrows = int(np.ceil(len(countries) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(18.0 if ncols == 4 else 14.0, 2.45 * nrows + 1.2), sharex=True)
    axes_flat = np.atleast_1d(axes).ravel()
    meta = full.set_index("country_code")
    show_annotation_box = len(countries) <= 12
    for ax, iso in zip(axes_flat, countries):
        subset = plotted[plotted["country_code"].eq(iso)].sort_values("week_start")
        bands = [0.0] + threshold_values + [global_rain_max]
        band_colors = ["#f7fbff", "#deebf7", "#c6dbef", "#9ecae1", "#6baed6", "#3182bd", "#08519c"]
        for i in range(len(bands) - 1):
            ax.axhspan(bands[i], bands[i + 1], color=band_colors[min(i, len(band_colors) - 1)], alpha=0.35, zorder=0)
        for y_val in threshold_values:
            ax.axhline(y_val, color="#4d4d4d", linewidth=0.7, linestyle="--", alpha=0.65, zorder=1)
        contrast = select_high_rain_contrast_weeks(subset)
        ax.plot(subset["week_start"], subset["median"], color="#5dade2", linewidth=1.4)
        ax.set_ylim(0, global_rain_max)
        ax2 = ax.twinx()
        ax2.plot(subset["week_start"], subset["weekly_burden_h"], color="#d62828", linewidth=1.4)
        ax2.set_ylim(0, global_burden_max)
        marker_specs = [
            ("high_rain_low_burden", "#2ca25f", "rain high, delay low"),
            ("high_rain_high_burden", "#f16913", "critical week"),
        ]
        annotation_lines = []
        for key, color, label in marker_specs:
            row = contrast.get(key)
            if row is None:
                continue
            ax.axvline(row["week_start"], color=color, linewidth=0.9, linestyle=":", alpha=0.8, zorder=1)
            annotation_lines.append(f"{label}: {pd.to_datetime(row['week_start']).strftime('%b %d')}")
        remoteness = float(meta.loc[iso, "weighted_baseline_travel_time_h"])
        unpaved = float(meta.loc[iso, "actual_unpaved_time_share"])
        title = f"{iso}  baseline route time={remoteness:.1f}h  unpaved={unpaved:.0%}" if ncols == 2 else f"{iso} base={remoteness:.0f}h unpaved={unpaved:.0%}"
        ax.set_title(title, fontsize=9 if ncols == 4 else 10)
        if annotation_lines and show_annotation_box:
            ax.text(
                0.01,
                0.98,
                "\n".join(annotation_lines),
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=7,
                bbox={"facecolor": "white", "alpha": 0.72, "edgecolor": "#d9d9d9", "boxstyle": "round,pad=0.25"},
            )
        ax.grid(True, color="#eeeeee", linewidth=0.7)
        ax.tick_params(axis="x", rotation=45, labelsize=6 if ncols == 4 else 7)
        ax.tick_params(axis="y", labelsize=6 if ncols == 4 else 7)
        ax2.tick_params(axis="y", labelsize=6 if ncols == 4 else 7)
        if ax.get_subplotspec().is_last_row():
            ax.set_xlabel("Week in 2024", fontsize=7 if ncols == 4 else 9)
    for ax in axes_flat[len(countries):]:
        ax.set_axis_off()
    fig.suptitle("Weekly rainfall and severe accessibility delay do not move one-to-one across countries", y=0.985, fontsize=12)
    caption = (
        "X-axis is calendar week. Blue line and left y-axis show country median weekly precipitation. Red line and "
        "right y-axis show weekly severe accessibility delay: accumulated hours above the 3-hour delay threshold. "
        "Background shading follows the project precipitation thresholds. Panel titles report route-weighted baseline "
        "travel time and actual unpaved-route share."
    )
    fig.text(0.015, 0.53, "Rainfall, mm/week (blue, left axis)", rotation=90, va="center", ha="center", fontsize=9, color="#2b7bba")
    fig.text(0.985, 0.53, "Severe delay, hours/week (red, right axis)", rotation=270, va="center", ha="center", fontsize=9, color="#b71c1c")
    fig.text(0.07, 0.035, fill(caption, width=180), ha="left", fontsize=9)
    fig.subplots_adjust(left=0.055, right=0.945, top=0.93, bottom=0.09, hspace=0.52 if ncols == 4 else 0.42, wspace=0.28)
    fig.savefig(out_path, dpi=180)
    plt.close(fig)
    return {"path": str(out_path), "countries": countries}


def main() -> None:
    args = parse_args()
    rain_mech_dir = Path(args.rain_mech_dir)
    route_mix_csv = Path(args.route_mix_csv)
    route_penalty_csv = Path(args.route_penalty_csv)
    out_dir = Path(args.out_dir)
    data_dir = out_dir / "data"
    figures_dir = out_dir / "figures"
    data_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)

    copied = copy_inputs(rain_mech_dir, route_mix_csv, route_penalty_csv, data_dir)
    country_mechanism, _, _, weekly_country = load_rain_mechanism_inputs(rain_mech_dir)
    penalty_rules = load_penalty_rules(rain_mech_dir)
    route_mix = pd.read_csv(route_mix_csv)
    route_penalty = pd.read_csv(route_penalty_csv)
    dest_crop_points = pd.read_csv(rain_mech_dir / "data" / "source_visual_experiment_crop_points_cluster_weighted_by_dest.csv")
    route_mix_summary = aggregate_route_mix(route_mix)
    threshold_impact_summary = aggregate_actual_threshold_impact(route_penalty)
    full = build_full_country_frame(country_mechanism, route_mix_summary, threshold_impact_summary)
    country_crop = build_country_crop_mechanism_frame(dest_crop_points, route_mix, route_penalty)
    crop_stats = crop_stat_summary(country_crop)

    structural_model = run_standardized_regression(
        full,
        "log_burden_h",
        PREDICTOR_ORDER,
    )
    correlation_rows = [
        bootstrap_spearman_summary(full, predictor, "log_burden_h", n_boot=2000, seed=42 + idx)
        for idx, predictor in enumerate(PREDICTOR_ORDER)
    ]
    boot_coef = bootstrap_standardized_coefficients(full, "log_burden_h", PREDICTOR_ORDER, n_boot=2000, seed=123)
    coefficient_rows = summarize_bootstrap_coefficients(structural_model, boot_coef)
    corr = correlation_table(
        full,
        ["total_burden_h", "threshold_impact_ratio_actual", "weighted_baseline_travel_time_h", "actual_unpaved_time_share"],
    )

    route_mix_summary_csv = data_dir / "country_route_mix_summary.csv"
    threshold_impact_csv = data_dir / "country_threshold_impact_summary.csv"
    full_csv = data_dir / "country_mechanism_full_summary.csv"
    corr_csv = data_dir / "country_correlation_matrix.csv"
    structural_fitted_csv = data_dir / "model_structural_fitted.csv"
    inferential_corr_csv = data_dir / "bootstrap_spearman_summary.csv"
    inferential_coef_csv = data_dir / "bootstrap_coefficient_summary.csv"
    weekly_country_csv = data_dir / "weekly_country_mechanism.csv"
    country_crop_csv = data_dir / "country_crop_mechanism_summary.csv"
    crop_stats_csv = data_dir / "crop_stat_summary.csv"
    route_mix_summary.to_csv(route_mix_summary_csv, index=False)
    threshold_impact_summary.to_csv(threshold_impact_csv, index=False)
    full.to_csv(full_csv, index=False)
    corr.to_csv(corr_csv)
    structural_model["fitted_frame"].to_csv(structural_fitted_csv, index=False)
    pd.DataFrame(correlation_rows).to_csv(inferential_corr_csv, index=False)
    pd.DataFrame(coefficient_rows).to_csv(inferential_coef_csv, index=False)
    weekly_country.to_csv(weekly_country_csv, index=False)
    country_crop.to_csv(country_crop_csv, index=False)
    crop_stats.to_csv(crop_stats_csv, index=False)

    plots = [
        plot_temporal_country_small_multiples(
            weekly_country,
            full,
            penalty_rules,
            figures_dir / "03_temporal_rain_burden_top_countries.png",
            None,
        ),
        plot_rain_delay_quadrants(
            full,
            figures_dir / "04_rain_to_delay_quadrants.png",
        ),
        plot_crop_stat_heatmap(
            crop_stats,
            figures_dir / "09_crop_stat_summary.png",
        ),
    ]

    manifest = {
        "rain_mech_dir": str(rain_mech_dir),
        "route_mix_csv": str(route_mix_csv),
        "out_dir": str(out_dir),
        "copied_inputs": copied,
        "derived_tables": {
            "route_mix_summary_csv": str(route_mix_summary_csv),
            "threshold_impact_summary_csv": str(threshold_impact_csv),
            "full_summary_csv": str(full_csv),
            "correlation_csv": str(corr_csv),
            "structural_fitted_csv": str(structural_fitted_csv),
            "bootstrap_spearman_summary_csv": str(inferential_corr_csv),
            "bootstrap_coefficient_summary_csv": str(inferential_coef_csv),
            "weekly_country_csv": str(weekly_country_csv),
            "country_crop_summary_csv": str(country_crop_csv),
            "crop_stat_summary_csv": str(crop_stats_csv),
        },
        "plots": plots,
        "models": {
            "structural": {
                "predictors": structural_model["predictors"],
                "coefficients": structural_model["coefficients"],
                "r_squared": structural_model["r_squared"],
                "adjusted_r_squared": structural_model["adjusted_r_squared"],
                "n_obs": structural_model["n_obs"],
            },
        },
        "headline_numbers": {
            "country_count": int(len(full)),
            "corr_threshold_impact_burden": float(next(row["rho"] for row in correlation_rows if row["predictor"] == "log_threshold_impact")),
            "corr_remoteness_burden": float(next(row["rho"] for row in correlation_rows if row["predictor"] == "log_remoteness_h")),
            "corr_unpaved_burden": float(next(row["rho"] for row in correlation_rows if row["predictor"] == "actual_unpaved_time_share")),
            "top_positive_residual_country": str(structural_model["fitted_frame"].sort_values("residual_z", ascending=False).iloc[0]["country_code"]),
            "top_negative_residual_country": str(structural_model["fitted_frame"].sort_values("residual_z", ascending=True).iloc[0]["country_code"]),
            "top_crop_by_burden": str(crop_stats.iloc[0]["crop_code"]) if not crop_stats.empty else None,
        },
    }
    (out_dir / "experiment_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log(f"[done] out_dir={out_dir}")
    for plot in plots:
        log(f"[plot] {plot['path']}")


if __name__ == "__main__":
    main()
