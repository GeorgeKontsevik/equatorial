#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import yaml
from shapely.geometry import box

ROOT = Path(__file__).resolve().parents[1]
CFG_DIR = ROOT / "config/generated/full_year_2024_era5_tp_remaining_20260517_203158"
NATURAL_EARTH = ROOT / ".venv/lib/python3.12/site-packages/pyogrio/tests/fixtures/naturalearth_lowres/naturalearth_lowres.shp"
OUT = ROOT / "outputs/equator_country_belt_road_surface_missing_hatched.png"
BELT_KM = 700
BELT_DEG = BELT_KM / 111.32
NO_CROP_CANDIDATES = {"GNQ"}

NAME_OVERRIDES = {
    "BRA": "Brazil",
    "COD": "DR Congo",
    "COG": "Congo",
    "CIV": "Coast",
    "GNQ": "Eq. Guinea",
    "SSD": "South Sudan",
    "CAF": "Central African\nRepublic",
    "PNG": "Papua New\nGuinea",
    "FRA": "France\n(French Guiana)",
    "PHL": "Philippines",
    "THA": "Thailand",
    "GHA": "Ghana",
}
# Hand-tuned small offsets keep labels readable in the crowded 700 km belt.
LABEL_OFFSETS = {
    "FRA": (-52.4, 8.8),
    "GHA": (-3.5, 1.8),
    "PHL": (3.8, 3.2),
    "THA": (0.8, 4.0),
    "BRN": (1.3, -0.9),
    "GNQ": (-0.8, -1.6),
    "RWA": (1.3, -1.5),
    "BDI": (1.3, -2.4),
    "UGA": (1.2, 1.2),
    "KEN": (1.2, -0.8),
    "TZA": (1.5, -2.0),
    "COG": (2.0, 0.5),
    "COD": (2.0, -2.6),
    "GAB": (-2.0, -1.3),
    "CMR": (-1.5, 1.3),
    "BEN": (-1.2, 1.5),
    "TGO": (-0.8, 0.2),
    "NGA": (0.7, 1.6),
    "SSD": (1.0, 2.0),
    "ETH": (1.8, 1.6),
    "SOM": (2.3, 0.8),
    "LKA": (2.5, 1.0),
    "MYS": (1.0, 2.0),
    "IDN": (0.0, -2.0),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render equatorial 700 km belt country input-status map.")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--xmin", type=float, default=-180.0)
    parser.add_argument("--xmax", type=float, default=180.0)
    parser.add_argument("--ymin", type=float, default=-28.0)
    parser.add_argument("--ymax", type=float, default=28.0)
    parser.add_argument("--fig-width", type=float, default=20.0)
    parser.add_argument("--fig-height", type=float, default=4.2)
    parser.add_argument("--aspect", default=None, choices=["auto", "equal"], help="Optional matplotlib axis aspect override.")
    return parser.parse_args()


def load_country_configs() -> dict[str, str]:
    countries: dict[str, str] = {}
    for path in sorted(CFG_DIR.glob("*_datasets_2024_full_year.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        study_area = doc.get("study_area", {}) if isinstance(doc, dict) else {}
        iso = str(study_area.get("country_code", "")).upper()
        if not iso:
            continue
        name = str(study_area.get("country_name", iso))
        countries[iso] = NAME_OVERRIDES.get(iso, name)
    return countries


def has_road_surface(iso: str) -> bool:
    root = ROOT / "data/raw/road_surface" / iso
    return (root / f"heigit_{iso.lower()}_roadsurface_lines.gpkg").exists() or (root / f"heigit_{iso.lower()}_roadsurface_lines.parquet").exists()


def country_geometry(world: gpd.GeoDataFrame, iso: str):
    if iso == "FRA":
        return world.loc[world["name"] == "France"]
    hit = world.loc[world["iso_a3"] == iso]
    return hit


def label_point(row, iso: str) -> tuple[float, float]:
    if iso == "FRA":
        return LABEL_OFFSETS["FRA"]
    point = row.geometry.representative_point()
    x, y = float(point.x), float(point.y)
    dx, dy = LABEL_OFFSETS.get(iso, (0.0, 0.0))
    return x + dx, y + dy


def main() -> None:
    args = parse_args()
    countries = load_country_configs()
    missing = {iso for iso in countries if not has_road_surface(iso)}
    world = gpd.read_file(NATURAL_EARTH).to_crs("EPSG:4326")
    world_clip = gpd.clip(world, box(args.xmin, args.ymin, args.xmax, args.ymax))

    selected_rows = []
    for iso in countries:
        rows = country_geometry(world, iso)
        if rows.empty:
            print(f"[warn] no Natural Earth geometry for {iso}")
            continue
        row = rows.iloc[0].copy()
        row["country_code"] = iso
        row["label"] = countries[iso]
        row["missing_road_surface"] = iso in missing
        row["no_crop_candidates"] = iso in NO_CROP_CANDIDATES
        selected_rows.append(row)
    selected = gpd.GeoDataFrame(selected_rows, geometry="geometry", crs=world.crs)

    fig, ax = plt.subplots(figsize=(args.fig_width, args.fig_height))
    world_clip.plot(ax=ax, color="#f7f7f4", edgecolor="#dadada", linewidth=0.55, zorder=1)
    regular = ~(selected["missing_road_surface"] | selected["no_crop_candidates"])
    selected.loc[regular].plot(
        ax=ax,
        color="#f2d59a",
        edgecolor="#9c650f",
        linewidth=0.9,
        zorder=3,
    )
    selected.loc[selected["missing_road_surface"]].plot(
        ax=ax,
        color="#f2d59a",
        edgecolor="#8f1d1d",
        linewidth=1.25,
        hatch="////",
        zorder=4,
    )
    selected.loc[selected["no_crop_candidates"]].plot(
        ax=ax,
        color="#f2d59a",
        edgecolor="#2f5597",
        linewidth=1.25,
        hatch="\\\\\\\\",
        zorder=4,
    )

    ax.axhline(0, color="#e31a1c", linewidth=2.2, label="Equator", zorder=5)
    ax.axhline(BELT_DEG, color="#1f70c1", linewidth=1.8, linestyle="--", label="+700 km belt edge", zorder=5)
    ax.axhline(-BELT_DEG, color="#1f70c1", linewidth=1.8, linestyle="--", label="-700 km belt edge", zorder=5)

    for row in selected.itertuples(index=False):
        x, y = label_point(row, row.country_code)
        ax.text(
            x,
            y,
            row.label,
            ha="center",
            va="center",
            fontsize=7.5,
            color="#333333",
            bbox={"boxstyle": "round,pad=0.16", "facecolor": "white", "edgecolor": "#777777", "alpha": 0.86},
            clip_on=False,
            zorder=6,
        )

    loaded_patch = mpatches.Patch(facecolor="#f2d59a", edgecolor="#9c650f", label="Road surface raw layer present")
    missing_patch = mpatches.Patch(facecolor="#f2d59a", edgecolor="#8f1d1d", hatch="////", label="Missing road surface raw layer")
    no_crop_patch = mpatches.Patch(facecolor="#f2d59a", edgecolor="#2f5597", hatch="\\\\\\\\", label="No crop candidates")
    handles, labels = ax.get_legend_handles_labels()
    handles.extend([loaded_patch, missing_patch, no_crop_patch])
    ax.legend(handles=handles, loc="lower left", frameon=True, fontsize=10)

    ax.set_xlim(args.xmin, args.xmax)
    ax.set_ylim(args.ymin, args.ymax)
    if args.aspect:
        ax.set_aspect(args.aspect)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Countries Within 700 km Of The Equator: Road Surface / Crop Input Status")
    ax.grid(alpha=0.18)
    fig.tight_layout()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180)
    plt.close(fig)
    print(f"wrote={args.out}")
    print(f"countries={len(countries)} missing_roads={','.join(sorted(missing))} no_crop={','.join(sorted(NO_CROP_CANDIDATES))}")


if __name__ == "__main__":
    main()
