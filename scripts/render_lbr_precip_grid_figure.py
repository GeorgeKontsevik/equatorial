#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
from matplotlib.colors import ListedColormap
from matplotlib.cm import ScalarMappable
from matplotlib.colors import BoundaryNorm
from psycopg.rows import dict_row
from shapely import contains_xy
from shapely.geometry.base import BaseGeometry


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_COUNTRY = "LBR"
DEFAULT_MONTHS = [7, 8]
DEFAULT_OUT_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / "paper_lbr_precip_grid"
DEFAULT_THESIS_IMAGE = REPO_ROOT / "itmo-phd-thesis-template-en" / "images" / "ch4" / "lbr_precip_grid_week_2024_08_19.png"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the masked Liberia ERA5 weekly precipitation panel for chapter 4.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--country-code", default=DEFAULT_COUNTRY)
    parser.add_argument("--months", default=",".join(str(month) for month in DEFAULT_MONTHS), help="Comma-separated month numbers used to select the wettest week.")
    parser.add_argument("--week-start", default="", help="Optional explicit week_start YYYY-MM-DD. Overrides --months selection.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--thesis-image", default=str(DEFAULT_THESIS_IMAGE))
    return parser.parse_args()


def parse_months(value: str) -> list[int]:
    months = [int(part.strip()) for part in value.split(",") if part.strip()]
    if not months:
        raise ValueError("At least one month must be provided.")
    invalid = [month for month in months if month < 1 or month > 12]
    if invalid:
        raise ValueError(f"Invalid month numbers: {invalid}")
    return months


def load_country_boundary(country_code: str) -> gpd.GeoDataFrame:
    path = ROOT / "data" / "raw" / "gadm" / country_code / f"gadm41_{country_code}.gpkg"
    if not path.exists():
        raise FileNotFoundError(f"Missing GADM boundary: {path}")
    boundary = gpd.read_file(path, layer="ADM_ADM_0").to_crs("EPSG:4326")
    return boundary[["geometry"]]


def fetch_weekly_medians(conn: psycopg.Connection, country_code: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT week_start,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY tp_sum_weekly_mm) AS median_mm
            FROM eq.era5_precip_weekly_grid
            WHERE country_code = %(country_code)s
            GROUP BY week_start
            ORDER BY week_start
            """,
            {"country_code": country_code},
        )
        return pd.DataFrame(cur.fetchall(), columns=["week_start", "median_mm"])


def fetch_week_grid(conn: psycopg.Connection, country_code: str, week_start: date) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cell_lon, cell_lat, tp_sum_weekly_mm
            FROM eq.era5_precip_weekly_grid
            WHERE country_code = %(country_code)s
              AND week_start = %(week_start)s
              AND tp_sum_weekly_mm IS NOT NULL
            ORDER BY cell_lat, cell_lon
            """,
            {"country_code": country_code, "week_start": week_start},
        )
        return pd.DataFrame(cur.fetchall(), columns=["cell_lon", "cell_lat", "tp_sum_weekly_mm"])


def fetch_weekly_road_damage(conn: psycopg.Connection, country_code: str, week_start: date) -> gpd.GeoDataFrame:
    suffix = country_code.lower()
    sql = f"""
        WITH roads AS (
            SELECT
                id AS road_row_id,
                CASE
                    WHEN lower(surface::text) IN ('paved', 'unpaved') THEN lower(surface::text)
                    WHEN lower(pred_label::text) IN ('paved', 'unpaved') THEN lower(pred_label::text)
                    WHEN lower(osm_surface_class::text) IN ('paved', 'unpaved') THEN lower(osm_surface_class::text)
                    WHEN lower(combined_surface_osm_priority::text) IN ('paved', 'unpaved') THEN lower(combined_surface_osm_priority::text)
                    WHEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority')) IN ('paved', 'unpaved')
                        THEN lower(coalesce(to_jsonb(r)->>'combined_surface_DL_priority', to_jsonb(r)->>'combined_surface_dl_priority'))
                    ELSE 'unknown'
                END AS surface_group,
                geometry
            FROM public.road_surface_{suffix} r
        ),
        overlay AS (
            SELECT
                m.road_row_id,
                o.tp_sum_weekly_mm
            FROM eq.road_era5_cell_map m
            JOIN eq.era5_precip_cell_overlay o
              ON o.country_code = m.country_code
             AND o.cell_id = m.cell_id
             AND o.week_start = %(week_start)s
             AND o.scenario = 'unknown_as_unpaved'
             AND o.surface_scope = 'all'
            WHERE m.country_code = %(country_code)s
        )
        SELECT
            r.road_row_id,
            r.surface_group,
            o.tp_sum_weekly_mm,
            CASE
                WHEN CASE WHEN r.surface_group = 'paved' THEN 'paved' ELSE 'unpaved' END = 'paved' THEN
                    CASE
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 300 THEN 0.05
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 200 THEN 0.40
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 100 THEN 0.75
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 50 THEN 0.90
                        ELSE 1.00
                    END
                ELSE
                    CASE
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 250 THEN 0.05
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 150 THEN 0.20
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 100 THEN 0.45
                        WHEN coalesce(o.tp_sum_weekly_mm, 0) >= 50 THEN 0.70
                        ELSE 1.00
                    END
            END AS speed_multiplier,
            r.geometry
        FROM roads r
        JOIN overlay o USING (road_row_id)
    """
    gdf = gpd.read_postgis(
        sql,
        conn,
        geom_col="geometry",
        params={"country_code": country_code, "week_start": week_start},
    )
    gdf["is_damaged"] = gdf["speed_multiplier"] < 1.0
    return gdf


def select_wettest_week(weekly: pd.DataFrame, months: list[int]) -> date:
    data = weekly.copy()
    data["week_start"] = pd.to_datetime(data["week_start"])
    subset = data[data["week_start"].dt.month.isin(months)].copy()
    if subset.empty:
        raise ValueError(f"No weekly ERA5 rows found for months {months}.")
    selected = subset.sort_values(["median_mm", "week_start"], ascending=[False, True]).iloc[0]["week_start"]
    return pd.Timestamp(selected).date()


def mask_points_inside_country(grid: pd.DataFrame, country_geometry: BaseGeometry) -> pd.DataFrame:
    if grid.empty:
        return grid.copy()
    inside = contains_xy(
        country_geometry,
        grid["cell_lon"].to_numpy(dtype=float),
        grid["cell_lat"].to_numpy(dtype=float),
    )
    return grid.loc[inside].copy().reset_index(drop=True)


def render_precip_grid(
    grid_inside: pd.DataFrame,
    road_damage: gpd.GeoDataFrame,
    boundary: gpd.GeoDataFrame,
    country_code: str,
    week_start: date,
    out_path: Path,
) -> dict[str, object]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bounds = boundary.total_bounds
    levels = [0, 25, 50, 75, 100, 150, 200, 300, 450]
    cmap = plt.get_cmap("YlGnBu", len(levels) - 1)
    norm = BoundaryNorm(levels, cmap.N)

    fig = plt.figure(figsize=(9, 12))
    precip_ax = fig.add_axes([0.02, 0.585, 0.96, 0.38])
    road_ax = fig.add_axes([0.02, 0.035, 0.96, 0.38])
    for ax in (precip_ax, road_ax):
        boundary.plot(ax=ax, color="white", edgecolor="#5a5a5a", linewidth=0.75, zorder=2)
        boundary.boundary.plot(ax=ax, color="#5a5a5a", linewidth=0.85, zorder=4)
        ax.set_xlim(bounds[0] - 0.05, bounds[2] + 0.05)
        ax.set_ylim(bounds[1] - 0.05, bounds[3] + 0.05)
        ax.set_aspect("equal", adjustable="box")
        ax.set_axis_off()

    if not road_damage.empty:
        all_roads = road_damage[["geometry"]]
        damaged = road_damage[road_damage["is_damaged"]].copy()
        all_roads.plot(ax=road_ax, color="#d3d3d3", linewidth=0.15, alpha=0.55, zorder=2.5)
        if not damaged.empty:
            severity_bins = [
                (0.90, "#facc15"),
                (0.75, "#f59e0b"),
                (0.40, "#dc2626"),
                (0.00, "#7f1d1d"),
            ]
            for threshold, color in severity_bins:
                if threshold == 0.90:
                    subset = damaged[np.isclose(damaged["speed_multiplier"], 0.90)]
                elif threshold == 0.75:
                    subset = damaged[np.isclose(damaged["speed_multiplier"], 0.75) | np.isclose(damaged["speed_multiplier"], 0.70)]
                elif threshold == 0.40:
                    subset = damaged[
                        np.isclose(damaged["speed_multiplier"], 0.45)
                        | np.isclose(damaged["speed_multiplier"], 0.40)
                        | np.isclose(damaged["speed_multiplier"], 0.20)
                    ]
                else:
                    subset = damaged[np.isclose(damaged["speed_multiplier"], 0.05)]
                if not subset.empty:
                    subset.plot(ax=road_ax, color=color, linewidth=0.28, alpha=0.92, zorder=3.5)
    road_ax.set_title(f"Деградация дорог, {week_start:%d.%m.%Y}", fontsize=15, fontweight="semibold", pad=8)
    damage_colors = ["#facc15", "#f59e0b", "#dc2626", "#7f1d1d"]
    damage_labels = ["слабое", "среднее", "сильное", "закрытие"]
    damage_cmap = ListedColormap(damage_colors)
    damage_norm = BoundaryNorm([0, 1, 2, 3, 4], damage_cmap.N)
    damage_cax = fig.add_axes([0.16, 0.020, 0.68, 0.012])
    damage_sm = ScalarMappable(norm=damage_norm, cmap=damage_cmap)
    damage_sm.set_array([])
    damage_cbar = fig.colorbar(damage_sm, cax=damage_cax, orientation="horizontal", ticks=[0.5, 1.5, 2.5, 3.5])
    damage_cbar.ax.set_xticklabels(damage_labels, fontsize=10)
    fig.text(0.5, 0.040, "Уровень деградации дорог", ha="center", va="bottom", fontsize=12)

    scatter = precip_ax.scatter(
        grid_inside["cell_lon"],
        grid_inside["cell_lat"],
        c=grid_inside["tp_sum_weekly_mm"],
        marker="s",
        s=8,
        cmap=cmap,
        norm=norm,
        linewidths=0,
        alpha=0.92,
        zorder=3,
    )
    precip_ax.set_title(f"Осадки, {week_start:%d.%m.%Y}", fontsize=15, fontweight="semibold", pad=8)
    cax = fig.add_axes([0.14, 0.515, 0.72, 0.014])
    cbar = fig.colorbar(scatter, cax=cax, orientation="horizontal", ticks=levels[:-1])
    cbar.set_label("мм за неделю", fontsize=13, labelpad=4)
    cbar.ax.tick_params(labelsize=11)
    fig.savefig(out_path, dpi=180, transparent=False)
    plt.close(fig)
    return {
        "path": str(out_path),
        "country_code": country_code,
        "week_start": week_start.isoformat(),
        "era5_points_inside_country": int(len(grid_inside)),
        "roads_total": int(len(road_damage)),
        "roads_damaged": int(road_damage["is_damaged"].sum()) if not road_damage.empty else 0,
        "median_inside_mm": float(grid_inside["tp_sum_weekly_mm"].median()) if not grid_inside.empty else None,
    }


def main() -> None:
    args = parse_args()
    months = parse_months(args.months)
    country_code = args.country_code.upper()
    out_dir = Path(args.out_dir)
    thesis_image = Path(args.thesis_image)
    boundary = load_country_boundary(country_code)
    country_geometry = boundary.geometry.union_all()

    with psycopg.connect(args.db_url) as conn:
        if args.week_start.strip():
            week_start = date.fromisoformat(args.week_start.strip())
        else:
            weekly = fetch_weekly_medians(conn, country_code)
            week_start = select_wettest_week(weekly, months)
        grid = fetch_week_grid(conn, country_code, week_start)
        road_damage = fetch_weekly_road_damage(conn, country_code, week_start)

    grid_inside = mask_points_inside_country(grid, country_geometry)
    out_png = out_dir / f"{country_code.lower()}_precip_grid_week_{week_start:%Y_%m_%d}.png"
    plot = render_precip_grid(grid_inside, road_damage, boundary, country_code, week_start, out_png)
    thesis_image.parent.mkdir(parents=True, exist_ok=True)
    render_precip_grid(grid_inside, road_damage, boundary, country_code, week_start, thesis_image)

    manifest = {
        "country_code": country_code,
        "selected_months": months,
        "week_start": week_start.isoformat(),
        "source_table": "eq.era5_precip_weekly_grid",
        "country_boundary": str(ROOT / "data" / "raw" / "gadm" / country_code / f"gadm41_{country_code}.gpkg"),
        "era5_points_total_before_country_mask": int(len(grid)),
        "era5_points_inside_country": int(len(grid_inside)),
        "era5_points_dropped_outside_country": int(len(grid) - len(grid_inside)),
        "plot": plot,
        "thesis_image": str(thesis_image),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
