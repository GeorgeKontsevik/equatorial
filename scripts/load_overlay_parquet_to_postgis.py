#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine, text
from shapely import from_wkb


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load overlay weekly parquet into eq.road_weekly_factors")
    p.add_argument("--db-url", required=True)
    p.add_argument("--country-code", required=True)
    p.add_argument("--overlay-dir", type=Path, required=True)
    p.add_argument("--replace-country", action="store_true")
    return p.parse_args()


def week_from_dir(name: str) -> str:
    # week_2024_01_01 -> 2024-01-01
    token = name.replace("week_", "")
    y, m, d = token.split("_")
    return f"{y}-{m}-{d}"


def main() -> None:
    args = parse_args()
    iso = args.country_code.upper()
    engine = create_engine(args.db_url)

    with engine.begin() as conn:
        conn.execute(text(open('/Users/gk/Code/super-duper-disser/equatorial/sql/01_db_only_schema.sql','r',encoding='utf-8').read()))
        if args.replace_country:
            conn.execute(text("DELETE FROM eq.road_weekly_factors WHERE country_code=:c"), {"c": iso})

    static_file = args.overlay_dir / "static" / "part_00000.parquet"
    if not static_file.exists():
        raise RuntimeError(f"Missing static parquet: {static_file}")
    static_df = pd.read_parquet(static_file)[["road_row_id", "surface_group", "geometry"]].copy()
    if "geometry" in static_df.columns:
        sample = static_df["geometry"].dropna().head(1)
        if not sample.empty and isinstance(sample.iloc[0], (bytes, bytearray, memoryview)):
            static_df["geometry"] = from_wkb(static_df["geometry"])

    weekly_dir = args.overlay_dir / 'weekly'
    week_dirs = sorted([p for p in weekly_dir.iterdir() if p.is_dir() and p.name.startswith('week_')])
    if not week_dirs:
        raise RuntimeError(f"No week_* dirs in {weekly_dir}")

    total_rows = 0
    for wdir in week_dirs:
        wstart = week_from_dir(wdir.name)
        parts = sorted(wdir.glob('part_*.parquet'))
        if not parts:
            continue
        # Read only needed columns
        cols = ['road_row_id', f'era5_tp_week_{wdir.name.replace("week_", "")}_sum', f'era5_tp_1h_max_week_{wdir.name.replace("week_", "")}_mm_per_h']
        existing = None
        frames = []
        for part in parts:
            g = pd.read_parquet(part)
            if existing is None:
                existing = set(g.columns)
            keep = [c for c in cols if c in g.columns]
            g = g[keep].copy()
            # normalize column names
            rename = {}
            for c in g.columns:
                if c.startswith('era5_tp_week_') and c.endswith('_sum'):
                    rename[c] = 'era5_tp_sum_weekly_mm'
                if c.startswith('era5_tp_1h_max_week_') and c.endswith('_mm_per_h'):
                    rename[c] = 'era5_tp_1h_max_weekly_mm_per_h'
            g = g.rename(columns=rename)
            for req in ['era5_tp_sum_weekly_mm', 'era5_tp_1h_max_weekly_mm_per_h', 'surface_group']:
                if req not in g.columns:
                    g[req] = pd.NA
            g['week_start'] = wstart
            g['country_code'] = iso
            g = g.merge(static_df, on="road_row_id", how="left", suffixes=("", "_static"))
            if "surface_group_static" in g.columns:
                g["surface_group"] = g["surface_group_static"]
            if "geometry_static" in g.columns:
                g["geometry"] = g["geometry_static"]
            frames.append(g[['country_code', 'week_start', 'road_row_id', 'surface_group', 'era5_tp_sum_weekly_mm', 'era5_tp_1h_max_weekly_mm_per_h', 'geometry']])
        if not frames:
            continue
        chunk = pd.concat(frames, ignore_index=True)
        chunk = gpd.GeoDataFrame(chunk, geometry='geometry', crs='EPSG:4326')
        chunk.to_postgis('road_weekly_factors', engine, schema='eq', if_exists='append', index=False)
        total_rows += len(chunk)
        print(f"[load] {iso} week={wstart} rows={len(chunk):,}")

    print(f"[done] {iso} total_rows={total_rows:,}")


if __name__ == '__main__':
    main()
