#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import zipfile
from pathlib import Path

import geopandas as gpd
import psycopg
import pycountry


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
CITY_POP_MIN = 5_000
CITY_POP_MAX = 100_000


def log(message: str) -> None:
    print(message, flush=True)


def qident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def ensure_tables(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS eq")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eq.city_destinations_5k_100k (
                country_code text NOT NULL,
                geoname_id bigint NOT NULL,
                name text NOT NULL,
                ascii_name text,
                feature_class text,
                feature_code text,
                admin1_code text,
                population bigint,
                lon double precision NOT NULL,
                lat double precision NOT NULL,
                geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
                PRIMARY KEY (country_code, geoname_id)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS city_destinations_5k_100k_country_pop_idx
            ON eq.city_destinations_5k_100k (country_code, population DESC)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS city_destinations_5k_100k_geometry_gist
            ON eq.city_destinations_5k_100k USING GIST (geometry)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eq.port_destinations (
                port_id text PRIMARY KEY,
                name text,
                website text,
                natlscale integer,
                featurecla text,
                lon double precision NOT NULL,
                lat double precision NOT NULL,
                geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS port_destinations_geometry_gist
            ON eq.port_destinations USING GIST (geometry)
            """
        )
    conn.commit()


def loaded_country_codes(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code FROM (
                SELECT DISTINCT country_code FROM eq.city_destinations
                UNION
                SELECT DISTINCT country_code FROM eq.crop_origin_candidates
            ) c
            ORDER BY country_code
            """
        )
        return [row[0] for row in cur.fetchall()]


def load_cities(conn: psycopg.Connection, countries: list[str], force: bool) -> int:
    existing = 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM eq.city_destinations_5k_100k")
        existing = int(cur.fetchone()[0])
    if existing and not force:
        log(f"[skip] cities exist rows={existing:,}")
        return existing

    alpha2_to_iso3 = {}
    for iso in countries:
        country = pycountry.countries.get(alpha_3=iso)
        if country is not None:
            alpha2_to_iso3[country.alpha_2] = iso

    rows: list[tuple[object, ...]] = []
    cities_path = ROOT / "data/raw/cities/global/cities500.zip"
    with zipfile.ZipFile(cities_path) as zf:
        with zf.open("cities500.txt") as fh:
            reader = csv.reader(io.TextIOWrapper(fh, encoding="utf-8"), delimiter="\t")
            for row in reader:
                if len(row) < 19 or row[8] not in alpha2_to_iso3:
                    continue
                try:
                    pop = int(row[14] or 0)
                    lat = float(row[4])
                    lon = float(row[5])
                    geoname_id = int(row[0])
                except ValueError:
                    continue
                if CITY_POP_MIN <= pop <= CITY_POP_MAX:
                    rows.append((alpha2_to_iso3[row[8]], geoname_id, row[1], row[2], row[6], row[7], row[10], pop, lon, lat))

    with conn.cursor() as cur:
        cur.execute("TRUNCATE eq.city_destinations_5k_100k")
        with cur.copy(
            """
            COPY eq.city_destinations_5k_100k (
                country_code, geoname_id, name, ascii_name, feature_class, feature_code,
                admin1_code, population, lon, lat
            ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
            """
        ) as cp:
            for row in rows:
                cp.write_row(row)
        cur.execute("ANALYZE eq.city_destinations_5k_100k")
    conn.commit()
    log(f"[done] cities rows={len(rows):,} pop_range={CITY_POP_MIN}-{CITY_POP_MAX}")
    return len(rows)


def load_ports(conn: psycopg.Connection, force: bool) -> int:
    existing = 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM eq.port_destinations")
        existing = int(cur.fetchone()[0])
    if existing and not force:
        log(f"[skip] ports exist rows={existing:,}")
        return existing

    ports_zip = ROOT / "data/raw/ports/global/ne_10m_ports.zip"
    gdf = gpd.read_file(f"zip://{ports_zip}")
    rows: list[tuple[object, ...]] = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        point = geom.representative_point()
        name = row.get("name") or row.get("Name") or row.get("NAME") or ""
        port_id = str(row.get("ne_id") or row.get("scalerank") or idx)
        rows.append(
            (
                port_id,
                str(name),
                str(row.get("website") or ""),
                int(row.get("natlscale") or 0),
                str(row.get("featurecla") or ""),
                float(point.x),
                float(point.y),
            )
        )

    with conn.cursor() as cur:
        cur.execute("TRUNCATE eq.port_destinations")
        with cur.copy(
            """
            COPY eq.port_destinations (
                port_id, name, website, natlscale, featurecla, lon, lat
            ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
            """
        ) as cp:
            for row in rows:
                cp.write_row(row)
        cur.execute("ANALYZE eq.port_destinations")
    conn.commit()
    log(f"[done] ports rows={len(rows):,}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load 5k-100k cities and global ports to PostGIS.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    with psycopg.connect(args.db_url) as conn:
        ensure_tables(conn)
        countries = loaded_country_codes(conn)
        log(f"[countries] {','.join(countries)}")
        load_cities(conn, countries, args.force)
        load_ports(conn, args.force)


if __name__ == "__main__":
    main()
