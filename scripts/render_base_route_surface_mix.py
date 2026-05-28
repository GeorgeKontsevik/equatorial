#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "base_route_surface_mix"
CSV_PATH = BASE_DIR / "by_country_crop_dest_surface.csv"
OUT_DIR = BASE_DIR / "visuals"

SURFACE_ORDER = ["paved", "unpaved", "unpaved_synthetic_line", "unknown"]
SURFACE_LABELS = {
    "paved": "paved",
    "unpaved": "unpaved",
    "unpaved_synthetic_line": "synthetic unpaved link",
    "unknown": "unknown",
}
SURFACE_COLORS = {
    "paved": "#2f9e44",
    "unpaved": "#f08c00",
    "unpaved_synthetic_line": "#d9480f",
    "unknown": "#868e96",
}
DEST_LABELS = {
    "city_5_100k": "10 small cities",
    "city_100k_plus": "3 large cities",
    "port": "3 ports",
    "airport": "3 airports",
}
DEST_ORDER = ["city_5_100k", "city_100k_plus", "port", "airport"]
CROP_ORDER = ["avocado", "banana", "mango", "pineapple", "plantain"]


def weighted_surface_mix(frame: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    numer = (
        frame.groupby(group_cols + ["surface_group"], as_index=False)
        .agg(surface_length_km=("surface_length_km", "sum"), surface_travel_time_h=("surface_travel_time_h", "sum"))
    )
    denom = (
        frame.drop_duplicates(["country_code", "crop_code", "dest_type"])
        .groupby(group_cols, as_index=False)
        .agg(total_length_km=("total_length_km", "sum"), total_travel_time_h=("total_travel_time_h", "sum"))
    )
    out = numer.merge(denom, on=group_cols, how="left")
    out["length_pct"] = out["surface_length_km"] / out["total_length_km"].replace(0, np.nan)
    out["travel_time_pct"] = out["surface_travel_time_h"] / out["total_travel_time_h"].replace(0, np.nan)
    return out


def pivot_surface(frame: pd.DataFrame, index: str | list[str], value: str = "length_pct") -> pd.DataFrame:
    table = frame.pivot_table(index=index, columns="surface_group", values=value, aggfunc="sum", fill_value=0)
    for surface in SURFACE_ORDER:
        if surface not in table.columns:
            table[surface] = 0.0
    return table[SURFACE_ORDER]


def plot_country_stack(frame: pd.DataFrame, out_path: Path) -> dict[str, object]:
    mix = weighted_surface_mix(frame, ["country_code"])
    table = pivot_surface(mix, "country_code")
    table["non_paved_known"] = table["unpaved"] + table["unpaved_synthetic_line"]
    table = table.sort_values(["non_paved_known", "unknown"], ascending=True)

    fig_h = max(7.5, 0.31 * len(table) + 1.8)
    fig, ax = plt.subplots(figsize=(11.5, fig_h), constrained_layout=True)
    left = np.zeros(len(table))
    y = np.arange(len(table))
    for surface in SURFACE_ORDER:
        values = table[surface].to_numpy()
        ax.barh(
            y,
            values,
            left=left,
            color=SURFACE_COLORS[surface],
            edgecolor="white",
            linewidth=0.6,
            label=SURFACE_LABELS[surface],
        )
        left += values
    ax.set_yticks(y)
    ax.set_yticklabels(table.index)
    ax.set_xlim(0, 1)
    ax.set_xlabel("Share of baseline route length")
    ax.set_title("Baseline route surface mix by country")
    ax.xaxis.set_major_formatter(lambda x, _: f"{x:.0%}")
    ax.grid(axis="x", color="#e9ecef", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(title="Surface group", ncols=4, loc="lower center", bbox_to_anchor=(0.5, -0.10), frameon=True)
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return {
        "path": str(out_path),
        "name": "country_surface_mix_stacked",
        "countries": int(len(table)),
        "max_non_paved_known_country": str(table["non_paved_known"].idxmax()),
        "max_non_paved_known_share": float(table["non_paved_known"].max()),
    }


def plot_dest_stack(frame: pd.DataFrame, out_path: Path) -> dict[str, object]:
    mix = weighted_surface_mix(frame, ["dest_type"])
    table = pivot_surface(mix, "dest_type").reindex(DEST_ORDER)
    table.index = [DEST_LABELS.get(item, item) for item in table.index]

    fig, ax = plt.subplots(figsize=(10.5, 4.8), constrained_layout=True)
    x = np.arange(len(table))
    bottom = np.zeros(len(table))
    for surface in SURFACE_ORDER:
        values = table[surface].to_numpy()
        ax.bar(
            x,
            values,
            bottom=bottom,
            color=SURFACE_COLORS[surface],
            edgecolor="white",
            linewidth=0.8,
            label=SURFACE_LABELS[surface],
        )
        bottom += values
    ax.set_xticks(x)
    ax.set_xticklabels(table.index)
    ax.set_ylim(0, 1)
    ax.set_ylabel("Share of baseline route length")
    ax.set_title("Baseline route surface mix by destination group")
    ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    ax.grid(axis="y", color="#e9ecef", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(title="Surface group", ncols=4, loc="upper center", bbox_to_anchor=(0.5, -0.14), frameon=True)
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return {
        "path": str(out_path),
        "name": "destination_surface_mix_stacked",
        "dest_groups": int(len(table)),
    }


def plot_crop_country_heatmap(frame: pd.DataFrame, out_path: Path) -> dict[str, object]:
    mix = weighted_surface_mix(frame, ["country_code", "crop_code"])
    table = pivot_surface(mix, ["country_code", "crop_code"])
    table["non_paved_or_unknown"] = 1.0 - table["paved"]
    heat = table["non_paved_or_unknown"].unstack("crop_code").reindex(columns=CROP_ORDER)
    heat["row_max"] = heat.max(axis=1)
    heat = heat.sort_values("row_max", ascending=False).drop(columns="row_max")

    fig_h = max(8.0, 0.32 * len(heat) + 1.8)
    fig, ax = plt.subplots(figsize=(8.8, fig_h), constrained_layout=True)
    data = heat.to_numpy()
    im = ax.imshow(data, aspect="auto", cmap="YlOrRd", vmin=0, vmax=1)
    ax.set_xticks(np.arange(len(heat.columns)))
    ax.set_xticklabels(heat.columns, rotation=35, ha="right")
    ax.set_yticks(np.arange(len(heat.index)))
    ax.set_yticklabels(heat.index)
    ax.set_title("Non-paved or unknown share by country and crop")
    ax.set_xlabel("Crop")
    ax.set_ylabel("Country")
    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            value = data[i, j]
            if np.isfinite(value) and value >= 0.35:
                ax.text(j, i, f"{value:.0%}", ha="center", va="center", fontsize=7.5, color="#212529")
    cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("1 - paved share of baseline route length")
    cbar.ax.yaxis.set_major_formatter(lambda y, _: f"{y:.0%}")
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return {
        "path": str(out_path),
        "name": "country_crop_non_paved_unknown_heatmap",
        "countries": int(len(heat)),
        "max_share": float(np.nanmax(data)),
    }


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"Missing input CSV: {CSV_PATH}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    frame = pd.read_csv(CSV_PATH)
    required = {
        "country_code",
        "crop_code",
        "dest_type",
        "surface_group",
        "surface_length_km",
        "total_length_km",
        "surface_travel_time_h",
        "total_travel_time_h",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise SystemExit(f"Missing required columns: {', '.join(missing)}")

    plots = [
        plot_country_stack(frame, OUT_DIR / "01_country_surface_mix_stacked.png"),
        plot_dest_stack(frame, OUT_DIR / "02_destination_surface_mix_stacked.png"),
        plot_crop_country_heatmap(frame, OUT_DIR / "03_country_crop_non_paved_unknown_heatmap.png"),
    ]
    manifest = {
        "input_csv": str(CSV_PATH),
        "rows": int(len(frame)),
        "countries": sorted(frame["country_code"].dropna().unique().tolist()),
        "crops": sorted(frame["crop_code"].dropna().unique().tolist()),
        "dest_types": sorted(frame["dest_type"].dropna().unique().tolist()),
        "surface_groups": sorted(frame["surface_group"].dropna().unique().tolist()),
        "plots": plots,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    for plot in plots:
        print(f"[plot] {plot['path']}")
    print(f"[manifest] {OUT_DIR / 'manifest.json'}")


if __name__ == "__main__":
    main()
