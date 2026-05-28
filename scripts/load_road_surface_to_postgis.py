#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load road_surface Parquet (preferred) or GPKG into PostGIS tables road_surface_<iso3>.")
    p.add_argument("--dsn", required=True, help="SQLAlchemy DSN, e.g. postgresql+psycopg://user:pass@host:5432/db")
    p.add_argument("--iso-list", required=True, help="Comma-separated ISO3 list")
    p.add_argument("--raw-root", type=Path, default=Path("/Users/gk/Code/super-duper-disser/equatorial/data/raw/road_surface"))
    p.add_argument("--if-exists", choices=("replace", "append"), default="replace")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import sqlalchemy as sa
    except Exception as exc:
        raise RuntimeError("sqlalchemy is required") from exc

    engine = sa.create_engine(args.dsn)
    iso_list = [x.strip().upper() for x in args.iso_list.split(",") if x.strip()]
    for iso in iso_list:
        base = args.raw_root / iso
        parquet = base / f"heigit_{iso.lower()}_roadsurface_lines.parquet"
        gpkg = base / f"heigit_{iso.lower()}_roadsurface_lines.gpkg"
        if parquet.exists():
            src = parquet
            gdf = gpd.read_parquet(src)
        elif gpkg.exists():
            src = gpkg
            gdf = gpd.read_file(src)
        else:
            print(f"[skip] {iso} missing parquet/gpkg in {base}")
            continue
        print(f"[load] {iso} {src}")
        gdf = gdf.to_crs("EPSG:4326")
        table = f"road_surface_{iso.lower()}"
        geom_col = gdf.geometry.name or "geometry"
        gdf.to_postgis(table, engine, if_exists=args.if_exists, index=True, index_label="id")
        with engine.begin() as conn:
            conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {table}_{geom_col}_gist ON {table} USING GIST ({geom_col})"))
            conn.execute(sa.text(f"ANALYZE {table}"))
        print(f"[ok] {iso} table={table} rows={len(gdf):,}")


if __name__ == "__main__":
    main()
