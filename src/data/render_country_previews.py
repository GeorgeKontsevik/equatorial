"""Render quick-look PNG previews for downloaded datasets in the active study area."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pycountry
import rasterio
import xarray as xr
from matplotlib.lines import Line2D
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from rasterio.mask import mask
from rasterio.merge import merge
from shapely.geometry import LineString, box

from src.data.config import load_config


matplotlib.use("Agg")

CROP_FILE = "Table_CROPGRIDSv1.08_COU.xlsx"
SPAM_CODES = {
    "SOYB": "Soybean",
    "MAIZ": "Maize",
    "RICE": "Rice",
    "SUGC": "Sugarcane",
    "SUNF": "Sunflower",
    "COTT": "Cotton",
    "BEAN": "Bean",
    "POTA": "Potato",
    "WHEA": "Wheat",
    "SORG": "Sorghum",
}
SPAM_CROP_NAMES = {name.lower() for name in SPAM_CODES.values()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render PNG previews for all currently downloaded datasets in the active study area.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"), help="Path to the dataset configuration YAML file.")
    parser.add_argument("--country-code", type=str, default="", help="Optional ISO3 code to render a different country without changing config.")
    parser.add_argument(
        "--city-pop-threshold",
        type=int,
        default=50_000,
        help="Minimum population for city points drawn on preview maps.",
    )
    parser.add_argument(
        "--cropgrids-match-spam",
        action="store_true",
        help="When enabled, the CropGrids bar chart is filtered to products that are also present in the current SPAM crop set.",
    )
    return parser.parse_args()


def _country_boundary(project_root: Path, iso3: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    gadm_path = project_root / "data" / "raw" / "gadm" / iso3 / f"gadm41_{iso3}.gpkg"
    layers = ["ADM_ADM_0", "ADM_ADM_1", "ADM_ADM_2"]
    admin_frames: list[gpd.GeoDataFrame] = []
    for layer in layers:
        try:
            frame = gpd.read_file(gadm_path, layer=layer)
        except Exception:
            continue
        if not frame.empty:
            admin_frames.append(frame.to_crs("EPSG:4326"))

    if not admin_frames:
        raise FileNotFoundError(f"No readable GADM layers found in {gadm_path}")

    country = admin_frames[0][["geometry"]].copy()
    country["name"] = iso3
    admin = pd.concat(admin_frames, ignore_index=True)
    admin = gpd.GeoDataFrame(admin, geometry="geometry", crs="EPSG:4326")
    return country, admin


def _setup_axes(country: gpd.GeoDataFrame, title: str) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=(8, 8))
    minx, miny, maxx, maxy = country.total_bounds
    dx = max(maxx - minx, 0.2)
    dy = max(maxy - miny, 0.2)
    pad_x = dx * 0.1
    pad_y = dy * 0.1
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    country.boundary.plot(ax=ax, color="black", linewidth=1.2, zorder=10)
    return fig, ax


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def _format_resolution_label(res_x: float, res_y: float, latitude: float) -> str:
    if abs(res_x) <= 0 or abs(res_y) <= 0:
        return ""
    km_per_deg_lat = 111.32
    km_per_deg_lon = km_per_deg_lat * np.cos(np.deg2rad(latitude))
    if km_per_deg_lon <= 0:
        km_per_deg_lon = km_per_deg_lat
    x_km = abs(res_x) * km_per_deg_lon
    y_km = abs(res_y) * km_per_deg_lat
    return f"cell size: {abs(res_x):.4f}° x {abs(res_y):.4f}° (~{x_km:.1f} x {y_km:.1f} km)"


def _annotate_resolution(ax: plt.Axes, label: str) -> None:
    if not label:
        return
    ax.text(
        0.99,
        0.01,
        label,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.85, "edgecolor": "#666666"},
        zorder=20,
    )


def _add_scale_bar(ax: plt.Axes, country: gpd.GeoDataFrame) -> None:
    bounds = country.to_crs("EPSG:4326").total_bounds
    minx, miny, maxx, maxy = [float(v) for v in bounds]
    mid_lat = (miny + maxy) / 2
    km_per_deg_lon = 111.32 * np.cos(np.deg2rad(mid_lat))
    if km_per_deg_lon <= 0:
        return

    width_deg = maxx - minx
    width_km = width_deg * km_per_deg_lon
    if width_km <= 0:
        return

    candidates_km = [5, 10, 20, 25, 50, 100, 200]
    target_km = width_km * 0.2
    scale_km = candidates_km[0]
    for candidate in candidates_km:
        scale_km = candidate
        if candidate >= target_km:
            break

    scale_deg = scale_km / km_per_deg_lon
    x0 = minx + width_deg * 0.06
    y0 = miny + (maxy - miny) * 0.05
    x1 = x0 + scale_deg

    ax.plot([x0, x1], [y0, y0], color="black", linewidth=3, solid_capstyle="butt", zorder=20)
    ax.plot([x0, x0], [y0, y0 + (maxy - miny) * 0.01], color="black", linewidth=2, zorder=20)
    ax.plot([x1, x1], [y0, y0 + (maxy - miny) * 0.01], color="black", linewidth=2, zorder=20)
    ax.text(
        (x0 + x1) / 2,
        y0 + (maxy - miny) * 0.015,
        f"{scale_km:g} km",
        ha="center",
        va="bottom",
        fontsize=8,
        bbox={"boxstyle": "round,pad=0.2", "facecolor": "white", "alpha": 0.85, "edgecolor": "#666666"},
        zorder=21,
    )


def _render_note(country: gpd.GeoDataFrame, out_path: Path, title: str, message: str) -> Path:
    fig, ax = _setup_axes(country, title)
    ax.text(0.5, 0.5, message, transform=ax.transAxes, ha="center", va="center")
    _save(fig, out_path)
    return out_path


def _render_gadm(country: gpd.GeoDataFrame, admin: gpd.GeoDataFrame, out_dir: Path) -> Path:
    fig, ax = _setup_axes(country, "GADM Administrative Boundaries")
    admin.boundary.plot(ax=ax, color="#2c7fb8", linewidth=0.6, alpha=0.7)
    country.boundary.plot(ax=ax, color="black", linewidth=1.3)
    _add_scale_bar(ax, country)
    out = out_dir / "gadm_boundaries.png"
    _save(fig, out)
    return out


def _road_surface_class(frame: gpd.GeoDataFrame) -> pd.Series:
    preferred = [
        "combined_surface_DL_priority",
        "combined_surface_osm_priority",
        "osm_surface_class",
        "pred_label",
        "surface",
    ]
    values = pd.Series("unknown", index=frame.index, dtype="object")
    for column in preferred:
        if column not in frame.columns:
            continue
        raw = frame[column].astype("string").str.lower()
        values = values.where(~raw.isin(["paved", "unpaved"]), raw.fillna(values))
    return values.fillna("unknown")


def _render_road_surface(country: gpd.GeoDataFrame, path: Path, out_dir: Path) -> Path:
    roads = gpd.read_file(path).to_crs("EPSG:4326")
    roads = roads.clip(country)
    roads["surface_group"] = _road_surface_class(roads)

    fig, ax = _setup_axes(country, "Road Surface")
    palette = {"paved": "#1a9641", "unpaved": "#d7191c", "unknown": "#999999"}
    for group, color in palette.items():
        subset = roads.loc[roads["surface_group"] == group]
        if not subset.empty:
            subset.plot(ax=ax, linewidth=0.6, color=color, alpha=0.8)

    legend_handles = [Line2D([0], [0], color=color, lw=2, label=group) for group, color in palette.items()]
    ax.legend(handles=legend_handles, loc="lower left")
    _add_scale_bar(ax, country)
    out = out_dir / "road_surface.png"
    _save(fig, out)
    return out


def _plot_cities(ax: plt.Axes, cities: gpd.GeoDataFrame | None, label_cities: bool = True) -> list[Line2D]:
    if cities is None or cities.empty:
        return []

    cities = cities.to_crs("EPSG:4326")
    cities.plot(ax=ax, color="#2b83ba", markersize=85, marker="o", edgecolor="black", linewidth=0.7, zorder=12)
    if label_cities:
        for row in cities.itertuples():
            ax.text(
                row.geometry.x,
                row.geometry.y,
                f" {row.name}",
                fontsize=8,
                va="center",
                ha="left",
                zorder=13,
                bbox={"boxstyle": "round,pad=0.18", "facecolor": "white", "alpha": 0.88, "edgecolor": "#666666"},
            )
    return [Line2D([0], [0], marker="o", linestyle="", color="#2b83ba", label="cities", markersize=8)]


def _render_single_raster(country: gpd.GeoDataFrame, path: Path, out_path: Path, title: str, cmap: str = "viridis") -> Path | None:
    with rasterio.open(path) as src:
        nodata = src.nodata
        resolution_label = _format_resolution_label(src.res[0], src.res[1], float(country.to_crs("EPSG:4326").geometry.unary_union.centroid.y))
        mask_shapes = country.geometry
        if src.crs:
            mask_shapes = country.to_crs(src.crs).geometry
        try:
            clipped, transform = mask(src, mask_shapes, crop=True, filled=False)
        except ValueError:
            return None
    band = clipped[0]
    data = np.ma.masked_invalid(band)
    if nodata is not None:
        data = np.ma.masked_where(data == nodata, data)
    data = np.ma.masked_where(data == 0, data)

    fig, ax = _setup_axes(country, title)
    if data.count() > 0:
        left = transform.c
        top = transform.f
        right = left + transform.a * data.shape[1]
        bottom = top + transform.e * data.shape[0]
        image = ax.imshow(data, extent=[left, right, bottom, top], origin="upper", cmap=cmap, alpha=0.85, zorder=1)
        fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02)
    else:
        ax.text(0.5, 0.5, "No raster values in study area", transform=ax.transAxes, ha="center", va="center")

    _annotate_resolution(ax, resolution_label)
    _save(fig, out_path)
    return out_path


def _render_era5_spi(country: gpd.GeoDataFrame, raw_root: Path, out_dir: Path) -> Path | None:
    spi_paths = sorted((raw_root / "era5_spi" / "global" / "monthly").glob("GLOBAL-ERA5_LAND_DAILY-spi-*.tif"))
    if not spi_paths:
        return None
    spi_paths = sorted(spi_paths, key=lambda path: int(path.stem.split("-spi-")[-1].removesuffix("mo")))

    clipped_layers: list[tuple[str, np.ma.MaskedArray, object]] = []
    resolution_label = ""
    for path in spi_paths:
        scale = path.stem.split("-spi-")[-1]
        with rasterio.open(path) as src:
            if not resolution_label:
                resolution_label = _format_resolution_label(
                    src.res[0],
                    src.res[1],
                    float(country.to_crs("EPSG:4326").geometry.unary_union.centroid.y),
                )
            mask_shapes = country.to_crs(src.crs).geometry if src.crs else country.geometry
            try:
                clipped, transform = mask(src, mask_shapes, crop=True, filled=False)
            except ValueError:
                continue
            band = clipped[0]
            data = np.ma.masked_invalid(band)
            if src.nodata is not None:
                data = np.ma.masked_where(data == src.nodata, data)
            clipped_layers.append((scale, data, transform))

    if not clipped_layers:
        return None

    fig, axes = plt.subplots(2, 3, figsize=(14, 8), sharex=False, sharey=False)
    fig.subplots_adjust(left=0.05, right=0.88, bottom=0.08, top=0.9, wspace=0.25, hspace=0.32)
    axes_flat = axes.ravel()
    image = None
    for ax, (scale, data, transform) in zip(axes_flat, clipped_layers, strict=False):
        minx, miny, maxx, maxy = country.total_bounds
        pad_x = max(maxx - minx, 0.2) * 0.08
        pad_y = max(maxy - miny, 0.2) * 0.08
        ax.set_xlim(minx - pad_x, maxx + pad_x)
        ax.set_ylim(miny - pad_y, maxy + pad_y)
        ax.set_aspect("equal")
        ax.set_title(f"SPI {scale}")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        if data.count() > 0:
            left = transform.c
            top = transform.f
            right = left + transform.a * data.shape[1]
            bottom = top + transform.e * data.shape[0]
            image = ax.imshow(
                data,
                extent=[left, right, bottom, top],
                origin="upper",
                cmap="RdBu",
                vmin=-2.0,
                vmax=2.0,
                alpha=0.9,
                zorder=1,
            )
            vals = np.asarray(data.compressed(), dtype="float64")
            inset = inset_axes(ax, width="34%", height="24%", loc="lower right", borderpad=0.8)
            inset.hist(vals[np.isfinite(vals)], bins=24, color="#555555", alpha=0.85)
            inset.axvline(-1.0, color="#3b7ddd", linestyle="--", linewidth=1.0)
            inset.axvline(-1.5, color="#d65f00", linestyle="-.", linewidth=1.0)
            inset.axvline(-2.0, color="#b00020", linestyle="-", linewidth=1.0)
            inset.set_xticks([-2, -1, 0, 1, 2])
            inset.set_yticks([])
            inset.set_title("cells", fontsize=7)
            inset.tick_params(labelsize=7)
        else:
            ax.text(0.5, 0.5, "No raster values", transform=ax.transAxes, ha="center", va="center")
        country.boundary.plot(ax=ax, color="black", linewidth=1.0, zorder=10)

    for ax in axes_flat[len(clipped_layers) :]:
        ax.axis("off")

    if image is not None:
        cbar_ax = fig.add_axes([0.91, 0.18, 0.018, 0.64])
        cbar = fig.colorbar(image, cax=cbar_ax)
        cbar.set_label("SPI")
    fig.suptitle("ERA5 SPI Raw Raster Distribution (Gabon clip)", y=0.98)
    if resolution_label:
        fig.text(0.99, 0.01, resolution_label, ha="right", va="bottom", fontsize=8)
    out = out_dir / "era5_spi_raw_distribution.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=180, bbox_inches="tight")
    plt.close(fig)
    return out


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _flood_window_dates(flood_cfg: dict | None) -> set[date] | None:
    if not flood_cfg:
        return None
    if flood_cfg.get("dates"):
        return {_parse_iso_date(str(item)) for item in flood_cfg["dates"]}
    if flood_cfg.get("start_date") and flood_cfg.get("end_date"):
        start = _parse_iso_date(str(flood_cfg["start_date"]))
        end = _parse_iso_date(str(flood_cfg["end_date"]))
        if end < start:
            raise ValueError(f"Flood window end_date before start_date: {start}..{end}")
        step_days = int(flood_cfg.get("aggregation_period_days", flood_cfg.get("step_days", 1)))
        if step_days <= 0:
            raise ValueError("flood.aggregation_period_days must be a positive integer.")
        days = (end - start).days
        dates = {start + timedelta(days=offset) for offset in range(0, days + 1, step_days)}
        dates.add(end)
        return dates
    return None


def _snapshot_to_date(year: int, doy: int) -> date:
    return date(year, 1, 1) + timedelta(days=doy - 1)


def _render_flood(country: gpd.GeoDataFrame, raw_root: Path, out_dir: Path, flood_cfg: dict | None = None) -> Path | None:
    flood_root = raw_root / "flood"
    if not flood_root.exists():
        return None

    pattern = re.compile(r"^\d{3}$")
    snapshots: dict[tuple[int, int], list[Path]] = {}
    for tif_path in flood_root.glob("*/*/*/*/*.tif"):
        try:
            year = int(tif_path.parent.parent.name)
            doy_raw = tif_path.parent.name
            if not pattern.match(doy_raw):
                continue
            doy = int(doy_raw)
        except ValueError:
            continue
        snapshots.setdefault((year, doy), []).append(tif_path)
    if not snapshots:
        return None

    allowed_dates = _flood_window_dates(flood_cfg)
    bbox_geom = box(*country.total_bounds)
    bbox_by_crs: dict[str, object] = {}
    candidate_paths: list[Path] = []
    selected_snapshot: tuple[int, int] | None = None
    for snapshot in sorted(snapshots.keys(), reverse=True):
        if allowed_dates is not None and _snapshot_to_date(snapshot[0], snapshot[1]) not in allowed_dates:
            continue
        snapshot_paths = sorted(snapshots[snapshot])
        intersects: list[Path] = []
        for tif_path in snapshot_paths:
            with rasterio.open(tif_path) as src:
                crs_key = str(src.crs) if src.crs else "EPSG:4326"
                if crs_key not in bbox_by_crs:
                    if src.crs:
                        bbox_by_crs[crs_key] = box(*country.to_crs(src.crs).total_bounds)
                    else:
                        bbox_by_crs[crs_key] = bbox_geom
                if box(*src.bounds).intersects(bbox_by_crs[crs_key]):
                    intersects.append(tif_path)
        if intersects:
            candidate_paths = intersects
            selected_snapshot = snapshot
            break

    if not candidate_paths:
        return None

    datasets = [rasterio.open(path) for path in candidate_paths]
    try:
        merged, transform = merge(datasets)
        meta = datasets[0].meta.copy()
        meta.update(
            {
                "height": merged.shape[1],
                "width": merged.shape[2],
                "transform": transform,
                "count": merged.shape[0],
            }
        )
    finally:
        for dataset in datasets:
            dataset.close()

    temp_path = out_dir / "_temp_flood_mosaic.tif"
    with rasterio.open(temp_path, "w", **meta) as dst:
        dst.write(merged)

    snapshot_label = ""
    if selected_snapshot is not None:
        snapshot_label = (
            f" ({selected_snapshot[0]}-DOY{selected_snapshot[1]:03d}, "
            f"{_snapshot_to_date(selected_snapshot[0], selected_snapshot[1]).isoformat()})"
        )
    try:
        out = _render_single_raster(
            country,
            temp_path,
            out_dir / "flood_observed_latest.png",
            f"NASA Observed Flood Water{snapshot_label}",
            cmap="Blues",
        )
    finally:
        temp_path.unlink(missing_ok=True)
    return out


def _render_ibtracs(country: gpd.GeoDataFrame, path: Path, out_dir: Path) -> Path:
    bbox = country.total_bounds
    ds = xr.open_dataset(path)
    try:
        lon = ds["lon"].values
        lat = ds["lat"].values
        in_bbox = (
            np.isfinite(lon)
            & np.isfinite(lat)
            & (lon >= bbox[0])
            & (lon <= bbox[2])
            & (lat >= bbox[1])
            & (lat <= bbox[3])
        )
        storm_ids = np.where(in_bbox.any(axis=1))[0]

        geoms: list[LineString] = []
        for storm_idx in storm_ids:
            storm_lon = lon[storm_idx]
            storm_lat = lat[storm_idx]
            valid = np.isfinite(storm_lon) & np.isfinite(storm_lat)
            coords = list(zip(storm_lon[valid], storm_lat[valid], strict=False))
            if len(coords) >= 2:
                geoms.append(LineString(coords))

        tracks = gpd.GeoDataFrame({"geometry": geoms}, crs="EPSG:4326")
    finally:
        ds.close()

    fig, ax = _setup_axes(country, "IBTrACS Tracks In Study Area")
    if not tracks.empty:
        tracks.plot(ax=ax, color="#4dac26", linewidth=1.0, alpha=0.8)
    else:
        ax.text(0.5, 0.5, "No IBTrACS track points in study-area bbox", transform=ax.transAxes, ha="center", va="center")

    _add_scale_bar(ax, country)
    out = out_dir / "ibtracs_tracks.png"
    _save(fig, out)
    return out


def _iso3_to_iso2(iso3: str) -> str | None:
    country = pycountry.countries.get(alpha_3=iso3.upper())
    return None if country is None else str(country.alpha_2)


def _load_geonames_cities(project_root: Path, iso3: str, min_population: int = 500_000) -> gpd.GeoDataFrame | None:
    iso2 = _iso3_to_iso2(iso3)
    if not iso2:
        return None

    zip_path = project_root / "data" / "raw" / "cities" / iso2.upper() / f"{iso2.upper()}.zip"
    if not zip_path.exists():
        return None

    columns = [
        "geonameid",
        "name",
        "asciiname",
        "alternatenames",
        "latitude",
        "longitude",
        "feature_class",
        "feature_code",
        "country_code",
        "cc2",
        "admin1_code",
        "admin2_code",
        "admin3_code",
        "admin4_code",
        "population",
        "elevation",
        "dem",
        "timezone",
        "modification_date",
    ]

    with zipfile.ZipFile(zip_path) as archive:
        preferred = f"{iso2.upper()}.txt"
        member = next((name for name in archive.namelist() if Path(name).name.upper() == preferred.upper()), None)
        if member is None:
            member = next((name for name in archive.namelist() if name.lower().endswith(".txt") and "readme" not in Path(name).name.lower()), None)
        if member is None:
            return None
        with archive.open(member) as handle:
            frame = pd.read_csv(handle, sep="\t", header=None, names=columns, dtype={"country_code": "string"})

    frame["population"] = pd.to_numeric(frame["population"], errors="coerce").fillna(0)
    frame = frame[(frame["feature_class"] == "P") & (frame["population"] >= min_population)].copy()
    if frame.empty:
        return None

    return gpd.GeoDataFrame(
        frame[["name", "population"]].copy(),
        geometry=gpd.points_from_xy(frame["longitude"], frame["latitude"]),
        crs="EPSG:4326",
    )


def _render_crop_summary(project_root: Path, iso3: str, out_dir: Path, match_spam_only: bool = False) -> Path | None:
    crop_path = project_root / CROP_FILE
    if not crop_path.exists():
        return None

    iso2 = _iso3_to_iso2(iso3)
    if not iso2:
        return None

    crop_df = pd.read_excel(crop_path, sheet_name="Harvested area (ha)")
    iso2_col = next((c for c in crop_df.columns if "ISO2" in str(c).upper()), crop_df.columns[0])
    name_col = next((c for c in crop_df.columns if "COUNTRY NAME" in str(c).upper()), crop_df.columns[1])
    crop_long = crop_df.melt(id_vars=[iso2_col, name_col], var_name="crop", value_name="harvested_ha")
    crop_long = crop_long.rename(columns={iso2_col: "country_iso2", name_col: "country_name"})
    crop_long["country_iso2"] = crop_long["country_iso2"].astype(str).str.strip()
    crop_long["crop"] = crop_long["crop"].astype(str).str.strip().str.lower()
    crop_long["harvested_ha"] = pd.to_numeric(crop_long["harvested_ha"], errors="coerce").fillna(0)
    country_rows = crop_long[(crop_long["country_iso2"] == iso2) & (crop_long["harvested_ha"] > 0)].copy()
    if country_rows.empty:
        return None

    spam_overlap = country_rows[country_rows["crop"].isin(SPAM_CROP_NAMES)].copy()
    overlap_names = sorted(spam_overlap["crop"].unique().tolist())
    missing_names = sorted(SPAM_CROP_NAMES - set(overlap_names))
    print(
        f"[cropgrids] {iso3} overlap_with_spam={len(overlap_names)} "
        f"overlap_crops={overlap_names} missing_spam_crops={missing_names}",
        flush=True,
    )

    chart_rows = country_rows.copy()
    title = f"Top Harvested Crops ({iso3})"
    out_name = "cropgrids_top_crops.png"

    if match_spam_only:
        chart_rows = spam_overlap.copy()
        if chart_rows.empty:
            return None
        title = f"CropGrids Harvested Crops Covered By SPAM ({iso3})"
        out_name = "cropgrids_spam_overlap.png"

    top_rows = chart_rows.sort_values("harvested_ha", ascending=False).head(12 if not match_spam_only else len(chart_rows))
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.barh(top_rows["crop"][::-1], top_rows["harvested_ha"][::-1], color="#2c7fb8")
    ax.set_title(title)
    ax.set_xlabel("Harvested area (ha)")
    ax.set_ylabel("Crop")
    out = out_dir / out_name
    _save(fig, out)

    summary_csv = out_dir / "cropgrids_country_summary.csv"
    country_rows.sort_values("harvested_ha", ascending=False).to_csv(summary_csv, index=False)
    overlap_csv = out_dir / "cropgrids_spam_overlap_summary.csv"
    spam_overlap.sort_values("harvested_ha", ascending=False).to_csv(overlap_csv, index=False)
    return out


def _render_spam_total_metric(
    country: gpd.GeoDataFrame,
    tif_paths: list[Path],
    out_dir: Path,
    title: str,
    out_name: str,
    cmap: str,
    cities: gpd.GeoDataFrame | None = None,
) -> Path | None:
    all_tif_paths = [path for path in tif_paths if path.exists()]
    if not all_tif_paths:
        return None

    total_array = None
    transform = None
    resolution_label = ""

    for path in all_tif_paths:
        with rasterio.open(path) as src:
            if not resolution_label:
                resolution_label = _format_resolution_label(
                    src.res[0],
                    src.res[1],
                    float(country.to_crs("EPSG:4326").geometry.unary_union.centroid.y),
                )
            try:
                clipped, current_transform = mask(src, country.geometry, crop=True, filled=True, nodata=0)
            except ValueError:
                continue
        data = clipped[0].astype("float32")
        data[data < 0] = 0
        if float(data.sum()) <= 0:
            continue
        if total_array is None:
            total_array = np.zeros_like(data, dtype="float32")
            transform = current_transform
        total_array += data

    if total_array is None or transform is None:
        return None

    total_masked = np.ma.masked_where(total_array <= 0, total_array)
    if total_masked.count() <= 0:
        return None

    fig, ax = _setup_axes(country, title)
    left = transform.c
    top = transform.f
    right = left + transform.a * total_masked.shape[1]
    bottom = top + transform.e * total_masked.shape[0]
    image = ax.imshow(total_masked, extent=[left, right, bottom, top], origin="upper", cmap=cmap, alpha=0.85, zorder=1)
    fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02)
    city_handles = _plot_cities(ax, cities)
    if city_handles:
        ax.legend(handles=city_handles, loc="lower left", fontsize=8)
    _annotate_resolution(ax, resolution_label)
    out = out_dir / out_name
    _save(fig, out)
    return out


def _spam_total_metric_array(country: gpd.GeoDataFrame, tif_paths: list[Path]) -> tuple[np.ndarray | None, str]:
    all_tif_paths = [path for path in tif_paths if path.exists()]
    if not all_tif_paths:
        return None, ""

    total_array = None
    resolution_label = ""
    for path in all_tif_paths:
        with rasterio.open(path) as src:
            if not resolution_label:
                resolution_label = _format_resolution_label(
                    src.res[0],
                    src.res[1],
                    float(country.to_crs("EPSG:4326").geometry.unary_union.centroid.y),
                )
            try:
                clipped, _ = mask(src, country.geometry, crop=True, filled=True, nodata=0)
            except ValueError:
                continue
        data = clipped[0].astype("float32")
        data[data < 0] = 0
        if float(data.sum()) <= 0:
            continue
        if total_array is None:
            total_array = np.zeros_like(data, dtype="float32")
        total_array += data

    return total_array, resolution_label


def _render_spam_distribution(
    values: np.ndarray,
    out_dir: Path,
    title: str,
    out_name: str,
    x_label: str,
) -> Path | None:
    positive = values[np.isfinite(values) & (values > 0)]
    if positive.size == 0:
        return None

    fig, ax = plt.subplots(figsize=(8, 5))
    bins = np.geomspace(float(positive.min()), float(positive.max()), 30)
    ax.hist(positive, bins=bins, color="#8c510a", alpha=0.85, edgecolor="white")
    ax.set_xscale("log")
    ax.set_title(title)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Pixel count")
    stats = (
        f"n={positive.size}\n"
        f"sum={positive.sum():,.0f}\n"
        f"median={np.median(positive):,.2f}\n"
        f"p95={np.percentile(positive, 95):,.2f}\n"
        f"max={positive.max():,.2f}"
    )
    ax.text(
        0.98,
        0.98,
        stats,
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.9, "edgecolor": "#666666"},
    )
    out = out_dir / out_name
    _save(fig, out)
    return out


def _render_spam(
    country: gpd.GeoDataFrame,
    project_root: Path,
    out_dir: Path,
    cities: gpd.GeoDataFrame | None = None,
) -> tuple[Path | None, Path | None, Path | None, Path | None]:
    spam_dir = project_root / "spam_tifs"
    all_tif_paths = [spam_dir / f"spam2010V2r0_global_H_{code}_A.tif" for code in SPAM_CODES]
    all_tif_paths = [path for path in all_tif_paths if path.exists()]
    if not all_tif_paths:
        return None, None, None, None

    total_array = None
    dominant_index = None
    dominant_value = None
    transform = None
    active_tif_paths: list[Path] = []
    resolution_label = ""

    for path in all_tif_paths:
        with rasterio.open(path) as src:
            if not resolution_label:
                resolution_label = _format_resolution_label(src.res[0], src.res[1], float(country.to_crs("EPSG:4326").geometry.unary_union.centroid.y))
            try:
                clipped, current_transform = mask(src, country.geometry, crop=True, filled=True, nodata=0)
            except ValueError:
                continue
        data = clipped[0].astype("float32")
        data[data < 0] = 0
        if float(data.sum()) <= 0:
            continue
        idx = len(active_tif_paths)
        active_tif_paths.append(path)
        if total_array is None:
            total_array = np.zeros_like(data, dtype="float32")
            dominant_index = np.full(data.shape, -1, dtype="int16")
            dominant_value = np.full(data.shape, -np.inf, dtype="float32")
            transform = current_transform
        total_array += data
        better = data > dominant_value
        dominant_value[better] = data[better]
        dominant_index[better] = idx

    if total_array is None or transform is None or not active_tif_paths:
        return None, None, None, None

    total_masked = np.ma.masked_where(total_array <= 0, total_array)
    total_out = None
    if total_masked.count() > 0:
        fig, ax = _setup_axes(country, "SPAM Total Harvested Area")
        left = transform.c
        top = transform.f
        right = left + transform.a * total_masked.shape[1]
        bottom = top + transform.e * total_masked.shape[0]
        image = ax.imshow(total_masked, extent=[left, right, bottom, top], origin="upper", cmap="YlGn", alpha=0.85, zorder=1)
        fig.colorbar(image, ax=ax, fraction=0.04, pad=0.02)
        city_handles = _plot_cities(ax, cities)
        if city_handles:
            ax.legend(handles=city_handles, loc="lower left", fontsize=8)
        _annotate_resolution(ax, resolution_label)
        total_out = out_dir / "spam_total_harvested_area.png"
        _save(fig, total_out)

    dom_masked = np.ma.masked_where((dominant_index < 0) | (dominant_value <= 0), dominant_index)
    dominant_out = None
    if dom_masked.count() > 0:
        fig, ax = _setup_axes(country, "SPAM Dominant Crop")
        left = transform.c
        top = transform.f
        right = left + transform.a * dom_masked.shape[1]
        bottom = top + transform.e * dom_masked.shape[0]
        cmap = plt.get_cmap("tab10", len(active_tif_paths))
        ax.imshow(dom_masked, extent=[left, right, bottom, top], origin="upper", cmap=cmap, alpha=0.8, zorder=1, vmin=0, vmax=max(len(active_tif_paths) - 1, 1))
        labels = [SPAM_CODES[path.stem.split("_")[3]] for path in active_tif_paths]
        handles = [Line2D([0], [0], marker="s", linestyle="", color=cmap(i), label=labels[i], markersize=8) for i in range(len(labels))]
        handles += _plot_cities(ax, cities)
        ax.legend(handles=handles, loc="lower left", fontsize=8)
        _annotate_resolution(ax, resolution_label)
        dominant_out = out_dir / "spam_dominant_crop.png"
        _save(fig, dominant_out)

    prod_dir = project_root / "spam_prod_tifs"
    prod_paths = [prod_dir / f"spam2010V2r0_global_P_{code}_A.tif" for code in SPAM_CODES]
    production_out = _render_spam_total_metric(
        country,
        prod_paths,
        out_dir,
        "SPAM Total Production",
        "spam_total_production.png",
        cmap="YlOrBr",
        cities=cities,
    )
    prod_total_array, _ = _spam_total_metric_array(country, prod_paths)
    production_distribution_out = None
    if prod_total_array is not None:
        production_distribution_out = _render_spam_distribution(
            prod_total_array,
            out_dir,
            "SPAM Total Production Distribution",
            "spam_total_production_distribution.png",
            "Total production per pixel (tons, log scale)",
        )
    return total_out, dominant_out, production_out, production_distribution_out


def _render_combined(country: gpd.GeoDataFrame, road_surface_path: Path | None, cities: gpd.GeoDataFrame | None, out_dir: Path) -> Path:
    fig, ax = _setup_axes(country, "Road Surface And Cities")
    if road_surface_path is not None and road_surface_path.exists():
        roads = gpd.read_file(road_surface_path).to_crs("EPSG:4326").clip(country)
        roads["surface_group"] = _road_surface_class(roads)
        palette = {"paved": "#1a9641", "unpaved": "#d7191c", "unknown": "#999999"}
        for group, color in palette.items():
            subset = roads.loc[roads["surface_group"] == group]
            if not subset.empty:
                subset.plot(ax=ax, linewidth=0.45, color=color, alpha=0.8, zorder=5)
        road_handles = [Line2D([0], [0], color=color, lw=2, label=group) for group, color in palette.items()]
    else:
        road_handles = []

    city_handles = _plot_cities(ax, cities)

    country.boundary.plot(ax=ax, color="black", linewidth=1.4, zorder=10)
    legend_handles = road_handles + city_handles
    if legend_handles:
        ax.legend(handles=legend_handles, loc="lower left", fontsize=8)
    _add_scale_bar(ax, country)
    out = out_dir / "all_together.png"
    _save(fig, out)
    return out


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    project_root = args.config.resolve().parents[1]
    study_area = config.get("study_area", {})
    flood_cfg = config.get("datasets", {}).get("flood", {})
    iso3 = str(args.country_code or study_area.get("country_code", "")).upper()
    if not iso3:
        raise ValueError("study_area.country_code is required")

    raw_root = project_root / "data" / "raw"
    out_dir = project_root / "outputs" / "country_preview" / iso3
    country, admin = _country_boundary(project_root, iso3)

    created: dict[str, str] = {}
    created["gadm"] = str(_render_gadm(country, admin, out_dir).relative_to(project_root))

    road_surface_path = raw_root / "road_surface" / iso3 / f"heigit_{iso3.lower()}_roadsurface_lines.gpkg"
    if road_surface_path.exists():
        created["road_surface"] = str(_render_road_surface(country, road_surface_path, out_dir).relative_to(project_root))

    chirps_path = raw_root / "chirps" / "global" / "monthly" / "chirps-v3.0.2024.01.tif"
    if chirps_path.exists():
        rendered = _render_single_raster(country, chirps_path, out_dir / "chirps_2024_01.png", "CHIRPS 2024-01", cmap="Blues")
        if rendered is not None:
            created["chirps"] = str(rendered.relative_to(project_root))

    liquefaction_path = raw_root / "liquefaction" / "global" / "liquefaction_v1_deg.tif"
    if liquefaction_path.exists():
        rendered = _render_single_raster(country, liquefaction_path, out_dir / "liquefaction.png", "Liquefaction Susceptibility", cmap="magma")
        if rendered is not None:
            created["liquefaction"] = str(rendered.relative_to(project_root))

    gem_path = raw_root / "gem" / "global" / "v2023_1_pga_475_rock_3min.tif"
    if gem_path.exists():
        rendered = _render_single_raster(country, gem_path, out_dir / "gem_pga_475y.png", "GEM Seismic Hazard PGA 475y", cmap="inferno")
        if rendered is not None:
            created["gem"] = str(rendered.relative_to(project_root))

    spi_path = _render_era5_spi(country, raw_root, out_dir)
    if spi_path is not None:
        created["era5_spi"] = str(spi_path.relative_to(project_root))

    for soil_path in sorted((raw_root / "soilgrids").glob("*.tif")):
        key = f"soilgrids_{soil_path.stem}"
        rendered = _render_single_raster(country, soil_path, out_dir / f"{soil_path.stem}.png", f"SoilGrids {soil_path.stem}", cmap="YlGn")
        if rendered is not None:
            created[key] = str(rendered.relative_to(project_root))

    flood_path = _render_flood(country, raw_root, out_dir, flood_cfg=flood_cfg)
    if flood_path is not None:
        created["flood_observed"] = str(flood_path.relative_to(project_root))
    else:
        # Keep preview outputs semantically clean when no flood raster matched the configured period.
        (out_dir / "flood_observed_latest.png").unlink(missing_ok=True)

    ibtracs_path = raw_root / "ibtracs" / "global" / "v04r01" / "netcdf" / "IBTrACS.since1980.v04r01.nc"
    if ibtracs_path.exists():
        created["ibtracs"] = str(_render_ibtracs(country, ibtracs_path, out_dir).relative_to(project_root))

    crop_summary = _render_crop_summary(project_root, iso3, out_dir, match_spam_only=args.cropgrids_match_spam)
    if crop_summary is not None:
        created["cropgrids"] = str(crop_summary.relative_to(project_root))

    cities = _load_geonames_cities(project_root, iso3, min_population=args.city_pop_threshold)

    spam_total, spam_dom, spam_prod, spam_prod_dist = _render_spam(country, project_root, out_dir, cities=cities)
    if spam_total is not None:
        created["spam_total"] = str(spam_total.relative_to(project_root))
    if spam_dom is not None:
        created["spam_dominant"] = str(spam_dom.relative_to(project_root))
    if spam_prod is not None:
        created["spam_total_production"] = str(spam_prod.relative_to(project_root))
    if spam_prod_dist is not None:
        created["spam_total_production_distribution"] = str(spam_prod_dist.relative_to(project_root))

    created["all_together"] = str(
        _render_combined(country, road_surface_path if road_surface_path.exists() else None, cities, out_dir).relative_to(project_root)
    )

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"country_code": iso3, "outputs": created}, indent=2), encoding="utf-8")
    print(f"Rendered {len(created)} preview files to {out_dir}")


if __name__ == "__main__":
    main()
