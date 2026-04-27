"""Build SPAM crop-specific top-cell origins for a country."""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.mask import mask
from rasterio.transform import xy
from scipy.ndimage import label as label_components
from shapely.geometry import Point

from src.data.render_country_previews import SPAM_CODES
from src.data.run_road_monthly_scenarios import _country_layers


matplotlib.use("Agg")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build top-N SPAM harvested-area origins per crop type.")
    parser.add_argument("--country-code", required=True, help="ISO3 country code, for example NAM.")
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument("--spam-dir", type=Path, default=Path("spam_tifs"))
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _top_cells_for_crop(
    tif_path: Path,
    country_wgs84: gpd.GeoDataFrame,
    crop_code: str,
    crop_name: str,
    top_n: int,
) -> list[dict[str, object]]:
    with rasterio.open(tif_path) as src:
        clipped, transform = mask(src, country_wgs84.geometry, crop=True, filled=True, nodata=0)
    arr = clipped[0].astype("float64")
    rows, cols = np.where(np.isfinite(arr) & (arr > 0))
    if rows.size == 0 or top_n <= 0:
        return []

    country_geom = country_wgs84.geometry.union_all()
    values = arr[rows, cols]
    component_records: list[dict[str, object]] = []
    structure = np.ones((3, 3), dtype=np.uint8)
    for quantile in (99.5, 99.0, 98.0, 97.0, 95.0, 90.0, 75.0, 0.0):
        threshold = float(np.nanpercentile(values, quantile))
        peak_mask = np.isfinite(arr) & (arr > 0) & (arr >= threshold)
        labeled, n_components = label_components(peak_mask, structure=structure)
        component_records = []
        for component_id in range(1, n_components + 1):
            comp_rows, comp_cols = np.where(labeled == component_id)
            if comp_rows.size == 0:
                continue
            comp_values = arr[comp_rows, comp_cols]
            peak_pos = int(np.nanargmax(comp_values))
            row = int(comp_rows[peak_pos])
            col = int(comp_cols[peak_pos])
            lon, lat = xy(transform, row, col, offset="center")
            point = Point(float(lon), float(lat))
            if not point.within(country_geom):
                continue
            component_records.append(
                {
                    "row": row,
                    "col": col,
                    "lon": float(lon),
                    "lat": float(lat),
                    "harvested_area_index": float(comp_values[peak_pos]),
                    "cluster_id": int(component_id),
                    "cluster_n_cells": int(comp_rows.size),
                    "cluster_harvested_area_index": float(np.nansum(comp_values)),
                    "selection_method": f"peak_cluster_q{quantile:g}",
                }
            )
        if len(component_records) >= top_n or quantile == 0.0:
            break

    component_records.sort(
        key=lambda item: (
            float(item["harvested_area_index"]),
            float(item["cluster_harvested_area_index"]),
            int(item["cluster_n_cells"]),
        ),
        reverse=True,
    )
    records: list[dict[str, object]] = []
    used_cells: set[tuple[int, int]] = set()
    for item in component_records[:top_n]:
        used_cells.add((int(item["row"]), int(item["col"])))
        records.append(
            {
                "crop_code": crop_code,
                "crop_name": crop_name,
                "crop_rank": len(records) + 1,
                "harvested_area_index": float(item["harvested_area_index"]),
                "cluster_id": int(item["cluster_id"]),
                "cluster_n_cells": int(item["cluster_n_cells"]),
                "cluster_harvested_area_index": float(item["cluster_harvested_area_index"]),
                "selection_method": str(item["selection_method"]),
                "lon": float(item["lon"]),
                "lat": float(item["lat"]),
            }
        )

    if len(records) >= top_n:
        return records

    order = np.argsort(values)[::-1]
    for pos in order:
        if len(records) >= top_n:
            break
        row = int(rows[int(pos)])
        col = int(cols[int(pos)])
        if (row, col) in used_cells:
            continue
        lon, lat = xy(transform, row, col, offset="center")
        point = Point(float(lon), float(lat))
        if not point.within(country_geom):
            continue
        used_cells.add((row, col))
        records.append(
            {
                "crop_code": crop_code,
                "crop_name": crop_name,
                "crop_rank": len(records) + 1,
                "harvested_area_index": float(values[int(pos)]),
                "cluster_id": None,
                "cluster_n_cells": None,
                "cluster_harvested_area_index": None,
                "selection_method": "ranked_cell_fallback",
                "lon": float(lon),
                "lat": float(lat),
            }
        )
    return records


def _plot_all(country: gpd.GeoDataFrame, origins: gpd.GeoDataFrame, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(10, 10))
    country.boundary.plot(ax=ax, color="black", linewidth=1.4)
    crops = sorted(origins["crop_code"].unique())
    cmap = plt.get_cmap("tab10", len(crops))
    for idx, crop_code in enumerate(crops):
        subset = origins[origins["crop_code"] == crop_code]
        subset.plot(ax=ax, markersize=42, color=cmap(idx), edgecolor="white", linewidth=0.7, label=crop_code, zorder=3)
        for row in subset.itertuples():
            ax.text(row.geometry.x, row.geometry.y, f"{row.crop_code}{row.crop_rank}", fontsize=7, ha="left", va="bottom")
    ax.set_title("SPAM Spatial Peak-Cluster Origins Per Crop")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(loc="lower left", ncol=2, fontsize=8)
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def _plot_crop(country: gpd.GeoDataFrame, tif_path: Path, crop_origins: gpd.GeoDataFrame, crop_name: str, out_path: Path) -> None:
    with rasterio.open(tif_path) as src:
        clipped, transform = mask(src, country.geometry, crop=True, filled=True, nodata=0)
    arr = clipped[0].astype("float64")
    arr[arr <= 0] = np.nan
    left, bottom, right, top = rasterio.transform.array_bounds(arr.shape[0], arr.shape[1], transform)

    fig, ax = plt.subplots(figsize=(9, 9))
    image = ax.imshow(arr, extent=(left, right, bottom, top), origin="upper", cmap="YlGn", alpha=0.9)
    country.boundary.plot(ax=ax, color="black", linewidth=1.3)
    crop_origins.plot(ax=ax, color="#d73027", markersize=70, edgecolor="white", linewidth=0.9, zorder=4)
    for row in crop_origins.itertuples():
        ax.text(row.geometry.x, row.geometry.y, str(row.crop_rank), fontsize=9, weight="bold", ha="left", va="bottom")
    ax.set_title(f"SPAM {crop_name}: Spatial Peak-Cluster Origins")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    fig.colorbar(image, ax=ax, shrink=0.72, label="Harvested-area index")
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    iso3 = args.country_code.upper()
    country, _admin = _country_layers(project_root, iso3)
    country_wgs84 = country.to_crs("EPSG:4326")

    records: list[dict[str, object]] = []
    for crop_code, crop_name in SPAM_CODES.items():
        tif_path = project_root / args.spam_dir / f"spam2010V2r0_global_H_{crop_code}_A.tif"
        if not tif_path.exists():
            print(f"[spam-top-origins] missing {tif_path}", flush=True)
            continue
        records.extend(_top_cells_for_crop(tif_path, country_wgs84, crop_code, crop_name, args.top_n))

    if not records:
        raise RuntimeError(f"No positive SPAM harvested-area cells found for {iso3}.")

    out_dir = args.output_dir
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    per_crop_dir = out_dir / "spam_top3_by_crop"
    per_crop_dir.mkdir(parents=True, exist_ok=True)

    frame = pd.DataFrame(records).sort_values(["crop_code", "crop_rank"]).reset_index(drop=True)
    frame["origin_id"] = np.arange(len(frame), dtype=int)
    origins = gpd.GeoDataFrame(frame.drop(columns=["lon", "lat"]), geometry=gpd.points_from_xy(frame["lon"], frame["lat"]), crs="EPSG:4326")

    gpkg_path = out_dir / f"spam_crop_top{args.top_n}_origins.gpkg"
    csv_path = out_dir / f"spam_crop_top{args.top_n}_origins.csv"
    map_path = out_dir / f"spam_crop_top{args.top_n}_origins_map.png"
    origins.to_file(gpkg_path, driver="GPKG")
    origins.drop(columns="geometry").to_csv(csv_path, index=False)
    _plot_all(country_wgs84, origins, map_path)

    for crop_code, crop_name in SPAM_CODES.items():
        subset = origins[origins["crop_code"] == crop_code]
        if subset.empty:
            continue
        tif_path = project_root / args.spam_dir / f"spam2010V2r0_global_H_{crop_code}_A.tif"
        _plot_crop(country_wgs84, tif_path, subset, crop_name, per_crop_dir / f"spam_{crop_code.lower()}_top{args.top_n}.png")

    print(
        {
            "country_code": iso3,
            "top_n": args.top_n,
            "n_origins": int(len(origins)),
            "n_crops": int(origins["crop_code"].nunique()),
            "gpkg": str(gpkg_path.relative_to(project_root)),
            "csv": str(csv_path.relative_to(project_root)),
            "map": str(map_path.relative_to(project_root)),
            "per_crop_dir": str(per_crop_dir.relative_to(project_root)),
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
