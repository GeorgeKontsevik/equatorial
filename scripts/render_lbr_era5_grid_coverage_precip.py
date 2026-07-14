#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = (
    ROOT
    / "supporting_materials"
    / "lbr_era5_grid_coverage"
    / "data"
    / "lbr_era5_grid_week_2024_08_19.gpkg"
)
DEFAULT_OUT = (
    ROOT
    / "outputs"
    / "astar_accessibility_weekly"
    / "paper_lbr_precip_grid"
    / "lbr_era5_grid_coverage_precip_2024_08_19.png"
)


def render(data_path: Path, out_path: Path) -> None:
    boundary = gpd.read_file(data_path, layer="boundary").to_crs("EPSG:4326")
    grid = gpd.read_file(data_path, layer="weekly_grid").to_crs("EPSG:4326")
    bounds = boundary.total_bounds
    levels = [0, 25, 50, 75, 100, 150, 200, 300, 450]
    cmap = plt.get_cmap("YlGnBu", len(levels) - 1)
    norm = BoundaryNorm(levels, cmap.N)

    fig = plt.figure(figsize=(827 / 180, 1683 / 180))
    coverage_ax = fig.add_axes([0.08, 0.57, 0.84, 0.34])
    precip_ax = fig.add_axes([0.08, 0.16, 0.84, 0.34])
    for ax in (coverage_ax, precip_ax):
        boundary.plot(ax=ax, color="white", edgecolor="#5a5a5a", linewidth=0.75, zorder=2)
        boundary.boundary.plot(ax=ax, color="#5a5a5a", linewidth=0.85, zorder=4)
        ax.set_xlim(bounds[0] - 0.25, bounds[2] + 0.25)
        ax.set_ylim(bounds[1] - 0.25, bounds[3] + 0.25)
        ax.set_aspect("equal", adjustable="box")
        ax.set_axis_off()

    coverage_ax.scatter(
        grid.geometry.x,
        grid.geometry.y,
        marker="s",
        s=8,
        color="#9ccdf2",
        linewidths=0,
        zorder=3,
    )
    coverage_ax.set_title("Покрытие ERA5-grid", fontsize=14, fontweight="semibold", pad=10)

    scatter = precip_ax.scatter(
        grid.geometry.x,
        grid.geometry.y,
        c=grid["tp_sum_weekly_mm"],
        marker="s",
        s=8,
        cmap=cmap,
        norm=norm,
        linewidths=0,
        alpha=0.92,
        zorder=3,
    )
    precip_ax.set_title("Осадки, 19.08.2024", fontsize=14, fontweight="semibold", pad=10)
    cax = fig.add_axes([0.08, 0.055, 0.84, 0.022])
    cbar = fig.colorbar(scatter, cax=cax, orientation="horizontal", ticks=levels[:-1])
    cbar.set_label("мм за неделю", fontsize=11, labelpad=4)
    cbar.ax.tick_params(labelsize=10)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=180, transparent=False)
    plt.close(fig)
    print(out_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the preserved LBR ERA5 coverage and weekly precipitation figure.")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    render(args.data, args.out)


if __name__ == "__main__":
    main()
