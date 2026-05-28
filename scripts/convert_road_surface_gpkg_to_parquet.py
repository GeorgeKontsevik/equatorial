#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert road_surface GPKG files to Parquet per country.")
    p.add_argument("--iso-list", required=True, help="Comma-separated ISO3 list")
    p.add_argument("--raw-root", type=Path, default=Path("/Users/gk/Code/super-duper-disser/equatorial/data/raw/road_surface"))
    p.add_argument("--overwrite", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    iso_list = [x.strip().upper() for x in args.iso_list.split(",") if x.strip()]
    for iso in iso_list:
        base = args.raw_root / iso
        gpkg = base / f"heigit_{iso.lower()}_roadsurface_lines.gpkg"
        pq = base / f"heigit_{iso.lower()}_roadsurface_lines.parquet"
        if not gpkg.exists():
            print(f"[skip] {iso} missing {gpkg}")
            continue
        if pq.exists() and not args.overwrite:
            print(f"[skip] {iso} parquet exists {pq}")
            continue
        print(f"[convert] {iso} {gpkg} -> {pq}")
        gdf = gpd.read_file(gpkg).to_crs("EPSG:4326")
        pq.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_parquet(pq, index=False)
        print(f"[ok] {iso} rows={len(gdf):,}")


if __name__ == "__main__":
    main()
