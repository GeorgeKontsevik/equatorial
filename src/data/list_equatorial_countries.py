"""Build a country list for a belt around the equator from a global GADM boundary file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import box


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List countries whose boundaries intersect a belt around the equator.")
    parser.add_argument("--gadm-path", type=Path, required=True, help="Path to a global GADM file with country polygons.")
    parser.add_argument("--layer", type=str, default="", help="Optional layer name. If omitted, the default layer is used.")
    parser.add_argument("--radius-km", type=float, default=500.0, help="Half-width of the equatorial belt in kilometers.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("outputs/equator_country_list"),
        help="Directory where text and JSON outputs will be written.",
    )
    return parser.parse_args()


def _pick_iso_column(frame: gpd.GeoDataFrame) -> str:
    for name in ["GID_0", "ADM0_A3", "ISO_A3", "ISO3", "iso3", "COUNTRY", "NAME_0", "NAME_ENGLI", "NAME"]:
        if name in frame.columns:
            return name
    raise KeyError(f"Could not find an ISO/name column in {list(frame.columns)}")


def _pick_name_column(frame: gpd.GeoDataFrame) -> str:
    for name in ["COUNTRY", "ADMIN", "NAME_0", "NAME_ENGLI", "NAME", "country", "admin"]:
        if name in frame.columns:
            return name
    return _pick_iso_column(frame)


def main() -> None:
    args = parse_args()
    belt_lat = float(args.radius_km) / 111.32
    belt_geom = box(-180.0, -belt_lat, 180.0, belt_lat)

    read_kwargs = {}
    if args.layer:
        read_kwargs["layer"] = args.layer

    countries = gpd.read_file(args.gadm_path, **read_kwargs).to_crs("EPSG:4326")
    iso_col = _pick_iso_column(countries)
    name_col = _pick_name_column(countries)
    countries = countries[[iso_col, name_col, "geometry"]].copy()
    countries = countries.rename(columns={iso_col: "iso3", name_col: "country_name"})
    countries["iso3"] = countries["iso3"].astype(str).str.strip()
    countries["country_name"] = countries["country_name"].astype(str).str.strip()

    hits = countries[countries.intersects(belt_geom)].copy()
    hits = hits.drop_duplicates(subset=["iso3", "country_name"]).reset_index(drop=True)
    equal_area = hits.to_crs("EPSG:6933")
    hits["area_sq_km"] = equal_area.geometry.area / 1_000_000.0
    hits = hits.sort_values(["area_sq_km", "iso3"], ascending=[False, True]).reset_index(drop=True)
    hits["belt_radius_km"] = float(args.radius_km)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    text_path = out_dir / f"countries_within_{int(args.radius_km)}km_of_equator.txt"
    json_path = out_dir / f"countries_within_{int(args.radius_km)}km_of_equator.json"
    csv_path = out_dir / f"countries_within_{int(args.radius_km)}km_of_equator.csv"

    text_path.write_text("\n".join(hits["iso3"].tolist()) + ("\n" if not hits.empty else ""), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "radius_km": float(args.radius_km),
                "count": int(len(hits)),
                "countries": hits[["iso3", "country_name"]].to_dict(orient="records"),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hits[["iso3", "country_name", "area_sq_km", "belt_radius_km"]].to_csv(csv_path, index=False)

    print(f"Wrote {len(hits)} countries to {text_path}")


if __name__ == "__main__":
    main()
