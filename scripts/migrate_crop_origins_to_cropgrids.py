#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg
import pycountry
import xarray as xr
from pyproj import Transformer
from shapely import contains_xy
from sklearn.cluster import KMeans

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
MAX_CLUSTERS_PER_CROP = 20

PRODUCTS = {
    "avocado": {"faostat_codes": [572], "faostat_items": ["Avocados"], "cropgrids_cols": ["avocado"]},
    "banana_plus_plantain": {
        "faostat_codes": [486, 489],
        "faostat_items": ["Bananas", "Plantains and cooking bananas"],
        "cropgrids_cols": ["banana", "plantain"],
    },
    "mango_guava_mangosteen": {
        "faostat_codes": [571],
        "faostat_items": ["Mangoes, guavas and mangosteens"],
        "cropgrids_cols": ["mango"],
    },
    "pineapple": {"faostat_codes": [574], "faostat_items": ["Pineapples"], "cropgrids_cols": ["pineapple"]},
}

CROP_LAYERS = ["avocado", "banana", "plantain", "mango", "pineapple"]
CROP_COLORS = {
    "avocado": "#1b9e77",
    "banana": "#d95f02",
    "plantain": "#7570b3",
    "mango": "#e7298a",
    "pineapple": "#66a61e",
}

CROP_TRANSPORT_VULNERABILITY = [
    {
        "crop_code": "mango",
        "vulnerability_class": "severe",
        "vulnerability_score": 4.0,
        "main_mechanism": "time-temperature deterioration plus transport vibration",
        "evidence_note": "Kinetic mango quality decay evidence and road-vibration mango study; delays and vibration accelerate firmness loss, weight loss, browning, and shelf-life decline.",
        "source_rows": "0;10",
    },
    {
        "crop_code": "banana",
        "vulnerability_class": "high",
        "vulnerability_score": 3.0,
        "main_mechanism": "vibration bruising plus stress respiration",
        "evidence_note": "Banana road transport evidence links rough/unpaved-road vibration to bruising, mechanical damage, and accelerated respiration; damage can exceed about 20% under harsh conditions.",
        "source_rows": "2;3;8;9",
    },
    {
        "crop_code": "plantain",
        "vulnerability_class": "high",
        "vulnerability_score": 3.0,
        "main_mechanism": "distance and road-quality related mechanical damage",
        "evidence_note": "Cooking-banana value-chain evidence is used as the plantain proxy; losses are significantly associated with transport distance and poor road quality.",
        "source_rows": "3",
    },
    {
        "crop_code": "avocado",
        "vulnerability_class": "high",
        "vulnerability_score": 3.0,
        "main_mechanism": "bad-road vibration, mechanical damage, and overheating",
        "evidence_note": "Avocado producer-level evidence attributes 17% of total post-harvest losses to transportation mode on bad roads, carts/human labor, and overheating.",
        "source_rows": "14",
    },
    {
        "crop_code": "pineapple",
        "vulnerability_class": "moderate",
        "vulnerability_score": 2.0,
        "main_mechanism": "handling and transport-stage mechanical damage",
        "evidence_note": "Pineapple evidence reports measurable wholesale/transport/unloading losses, but losses are distributed across the whole chain rather than dominated by travel-time exposure.",
        "source_rows": "13",
    },
]


@dataclass(frozen=True)
class CountryBoundary:
    iso3: str
    geometry: object
    bounds: tuple[float, float, float, float]


def log(message: str) -> None:
    print(message, flush=True)


def qident(name: str) -> str:
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ValueError(f"Unsafe identifier: {name}")
    return '"' + name + '"'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replace SPAM crop origins with CROPGRIDS layers.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default=None, help="Comma-separated ISO3 list. Default: current heatmap manifest countries.")
    parser.add_argument("--cropgrids-zip", default=str(ROOT / "data/raw/cropgrids/CROPGRIDSv1.08_NC_maps.zip"))
    parser.add_argument("--cropgrids-country-xlsx", default=str(ROOT / "data/raw/faostat/Table_CROPGRIDSv1.08_COU.xlsx"))
    parser.add_argument(
        "--faostat-zip",
        default=str(ROOT / "data/raw/faostat/Production_Crops_Livestock_E_All_Data_Normalized.zip"),
    )
    parser.add_argument("--out-dir", default=str(ROOT / "outputs/cropgrids_transition"))
    parser.add_argument("--max-clusters-per-crop", type=int, default=MAX_CLUSTERS_PER_CROP)
    parser.add_argument("--top-n", type=int, dest="max_clusters_per_crop", help=argparse.SUPPRESS)
    parser.add_argument("--skip-cleanup", action="store_true")
    return parser.parse_args()


def manifest_countries() -> list[str]:
    saved = ROOT / "outputs/cropgrids_transition/selected_countries.json"
    if saved.exists():
        return json.loads(saved.read_text())
    path = ROOT / "outputs/astar_accessibility_weekly/weekly_sum_penalty_v1_top5_per_crop_delta_minutes_heatmaps/manifest.json"
    return [row["country_code"] for row in json.loads(path.read_text())]


def load_boundaries(countries: list[str]) -> dict[str, CountryBoundary]:
    boundaries: dict[str, CountryBoundary] = {}
    for iso in countries:
        path = ROOT / "data/raw/gadm" / iso / f"gadm41_{iso}.gpkg"
        if not path.exists():
            raise FileNotFoundError(f"Missing GADM boundary for {iso}: {path}")
        frame = gpd.read_file(path, layer="ADM_ADM_0").to_crs("EPSG:4326")
        geom = frame.geometry.union_all()
        boundaries[iso] = CountryBoundary(iso3=iso, geometry=geom, bounds=geom.bounds)
    return boundaries


def faostat_area_mapping(faostat_zip: Path) -> tuple[dict[str, int], dict[str, str]]:
    with zipfile.ZipFile(faostat_zip) as archive:
        area_codes = pd.read_csv(archive.open("Production_Crops_Livestock_E_AreaCodes.csv"))
    m49_to_iso = {country.numeric.zfill(3): country.alpha_3 for country in pycountry.countries if hasattr(country, "numeric")}
    area_codes["M49_clean"] = area_codes["M49 Code"].astype(str).str.replace("'", "", regex=False).str.zfill(3)
    area_codes["iso3"] = area_codes["M49_clean"].map(m49_to_iso)
    area_codes.loc[area_codes["Area"].eq("World"), "iso3"] = "WORLD"
    return dict(zip(area_codes["iso3"], area_codes["Area Code"])), dict(zip(area_codes["iso3"], area_codes["Area"]))


def compute_production_shares(faostat_zip: Path, countries: list[str], out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    area_by_iso, name_by_iso = faostat_area_mapping(faostat_zip)
    needed_area_codes = [area_by_iso[iso] for iso in countries] + [area_by_iso["WORLD"]]
    needed_item_codes = sorted({code for product in PRODUCTS.values() for code in product["faostat_codes"]})
    usecols = ["Area Code", "Area", "Element Code", "Item Code", "Item", "Year", "Unit", "Value"]
    chunks = []
    with zipfile.ZipFile(faostat_zip) as archive:
        with archive.open("Production_Crops_Livestock_E_All_Data_(Normalized).csv") as handle:
            for chunk in pd.read_csv(handle, usecols=usecols, chunksize=500_000):
                mask = (
                    chunk["Year"].eq(2020)
                    & chunk["Element Code"].eq(5510)
                    & chunk["Item Code"].isin(needed_item_codes)
                    & chunk["Area Code"].isin(needed_area_codes)
                )
                if mask.any():
                    chunks.append(chunk.loc[mask].copy())
    frame = pd.concat(chunks, ignore_index=True)
    area_code_to_iso = {value: key for key, value in area_by_iso.items()}
    frame["iso3"] = frame["Area Code"].map(area_code_to_iso)

    country_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    for product, cfg in PRODUCTS.items():
        subset = frame[frame["Item Code"].isin(cfg["faostat_codes"])]
        world = float(subset.loc[subset["iso3"].eq("WORLD"), "Value"].sum())
        selected_total = float(subset.loc[subset["iso3"].isin(countries), "Value"].sum())
        summary_rows.append(
            {
                "product": product,
                "faostat_items": "; ".join(cfg["faostat_items"]),
                "world_production_tonnes_2020": world,
                "selected_countries_production_tonnes_2020": selected_total,
                "selected_countries_share_world_pct": 100.0 * selected_total / world if world else None,
            }
        )
        for iso in countries:
            value = float(subset.loc[subset["iso3"].eq(iso), "Value"].sum())
            country_rows.append(
                {
                    "product": product,
                    "iso3": iso,
                    "country_name": name_by_iso.get(iso, iso),
                    "faostat_items": "; ".join(cfg["faostat_items"]),
                    "production_tonnes_2020": value,
                    "world_production_tonnes_2020": world,
                    "country_share_world_pct": 100.0 * value / world if world else None,
                    "selected_countries_production_tonnes_2020": selected_total,
                    "selected_countries_share_world_pct": 100.0 * selected_total / world if world else None,
                }
            )
    summary = pd.DataFrame(summary_rows)
    all_world = float(summary["world_production_tonnes_2020"].sum())
    all_selected = float(summary["selected_countries_production_tonnes_2020"].sum())
    summary = pd.concat(
        [
            summary,
            pd.DataFrame(
                [
                    {
                        "product": "ALL_SELECTED_PRODUCTS",
                        "faostat_items": "Avocados; Bananas+Plantains; Mangoes/guavas/mangosteens; Pineapples",
                        "world_production_tonnes_2020": all_world,
                        "selected_countries_production_tonnes_2020": all_selected,
                        "selected_countries_share_world_pct": 100.0 * all_selected / all_world if all_world else None,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    country = pd.DataFrame(country_rows)
    summary.to_csv(out_dir / "faostat_2020_selected_countries_product_share_summary.csv", index=False)
    country.to_csv(out_dir / "faostat_2020_selected_countries_product_share_by_country.csv", index=False)
    return country, summary


def compute_cropgrids_country_area(cropgrids_country_xlsx: Path, countries: list[str], out_dir: Path) -> pd.DataFrame:
    harvested = pd.read_excel(cropgrids_country_xlsx, sheet_name="Harvested area (ha)")
    harvested["iso3"] = harvested["Country Name"].astype(str).str.extract(r"^([A-Z]{3});")[0]
    rows = []
    for product, cfg in PRODUCTS.items():
        cols = cfg["cropgrids_cols"]
        world = float(harvested[cols].sum().sum())
        selected = float(harvested.loc[harvested["iso3"].isin(countries), cols].sum().sum())
        for iso in countries:
            value = float(harvested.loc[harvested["iso3"].eq(iso), cols].sum(axis=1).sum())
            rows.append(
                {
                    "product": product,
                    "iso3": iso,
                    "cropgrids_columns": "; ".join(cols),
                    "harvested_area_ha_2020": value,
                    "world_harvested_area_ha_2020": world,
                    "country_share_world_harvested_area_pct": 100.0 * value / world if world else None,
                    "selected_countries_harvested_area_ha_2020": selected,
                    "selected_countries_share_world_harvested_area_pct": 100.0 * selected / world if world else None,
                }
            )
    frame = pd.DataFrame(rows)
    frame.to_csv(out_dir / "cropgrids_2020_selected_countries_harvested_area_share_by_country.csv", index=False)
    return frame


def extract_crop_layers(cropgrids_zip: Path, extract_dir: Path) -> dict[str, Path]:
    extract_dir.mkdir(parents=True, exist_ok=True)
    layer_paths: dict[str, Path] = {}
    with zipfile.ZipFile(cropgrids_zip) as archive:
        names = archive.namelist()
        lower_names = {name.lower(): name for name in names}
        for crop in CROP_LAYERS:
            matches = [
                original
                for lower, original in lower_names.items()
                if lower.endswith(".nc") and (f"_{crop}.nc" in lower or f"/{crop}.nc" in lower or f"{crop}.nc" in lower)
            ]
            if not matches:
                matches = [name for name in names if name.lower().endswith(".nc") and crop in Path(name).stem.lower().split("_")]
            if not matches:
                raise FileNotFoundError(f"Could not find CROPGRIDS NetCDF for crop={crop}")
            member = sorted(matches, key=len)[0]
            target = extract_dir / Path(member).name
            if not target.exists() or target.stat().st_size == 0:
                log(f"[extract] {crop}: {member}")
                with archive.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            layer_paths[crop] = target
    return layer_paths


def lon_lat_names(dataset: xr.Dataset) -> tuple[str, str]:
    lon_name = next((name for name in ["lon", "longitude", "x"] if name in dataset.coords), None)
    lat_name = next((name for name in ["lat", "latitude", "y"] if name in dataset.coords), None)
    if lon_name is None or lat_name is None:
        raise ValueError(f"Cannot identify lon/lat coordinates: {list(dataset.coords)}")
    return lon_name, lat_name


def country_metric_transformer(boundary: CountryBoundary) -> tuple[Transformer, str]:
    minx, miny, maxx, maxy = boundary.bounds
    lon0 = (minx + maxx) / 2.0
    lat0 = (miny + maxy) / 2.0
    crs = f"+proj=aeqd +lat_0={lat0:.8f} +lon_0={lon0:.8f} +datum=WGS84 +units=m +no_defs"
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True), crs


def weighted_cluster_representatives(
    iso: str,
    crop: str,
    path: Path,
    boundary: CountryBoundary,
    lons: np.ndarray,
    lats: np.ndarray,
    values: np.ndarray,
    max_clusters: int,
) -> list[tuple]:
    transformer, metric_crs = country_metric_transformer(boundary)
    x, y = transformer.transform(lons, lats)
    coords = np.column_stack([x, y]).astype("float64")
    finite = np.isfinite(coords).all(axis=1) & np.isfinite(values) & (values > 0)
    coords = coords[finite]
    lons = lons[finite]
    lats = lats[finite]
    values = values[finite]
    if len(values) == 0:
        return []

    unique_points = len(np.unique(coords, axis=0))
    k = min(max_clusters, len(values), unique_points)
    if k < 1:
        return []
    if k == 1:
        labels = np.zeros(len(values), dtype=int)
    else:
        labels = KMeans(n_clusters=k, random_state=0, n_init=10).fit_predict(coords, sample_weight=values)

    total_area = float(values.sum())
    cluster_rows: list[dict[str, float | int | str]] = []
    for label in range(k):
        mask = labels == label
        if not mask.any():
            continue
        cluster_values = values[mask]
        cluster_coords = coords[mask]
        cluster_total = float(cluster_values.sum())
        centroid = np.average(cluster_coords, axis=0, weights=cluster_values)
        squared_dist = np.sum((cluster_coords - centroid) ** 2, axis=1)
        representative_local = int(np.argmin(squared_dist))
        source_indices = np.flatnonzero(mask)
        representative_idx = int(source_indices[representative_local])
        representative_distance_m = float(math.sqrt(float(squared_dist[representative_local])))
        cluster_rows.append(
            {
                "harvested_area": cluster_total,
                "lon": float(lons[representative_idx]),
                "lat": float(lats[representative_idx]),
                "cluster_cell_count": int(mask.sum()),
                "representative_cell_harvested_area": float(values[representative_idx]),
                "cluster_share": cluster_total / total_area if total_area else None,
                "representative_distance_m": representative_distance_m,
                "metric_crs": metric_crs,
            }
        )

    cluster_rows.sort(key=lambda row: (-float(row["harvested_area"]), float(row["representative_distance_m"])))
    return [
        (
            iso,
            crop,
            rank,
            row["harvested_area"],
            row["lon"],
            row["lat"],
            path.name,
            row["cluster_cell_count"],
            row["representative_cell_harvested_area"],
            row["cluster_share"],
            row["representative_distance_m"],
            row["metric_crs"],
        )
        for rank, row in enumerate(cluster_rows, start=1)
    ]


def crop_candidates_from_layer(
    crop: str,
    path: Path,
    boundaries: dict[str, CountryBoundary],
    max_clusters: int,
) -> tuple[list[tuple], dict[str, pd.DataFrame]]:
    rows: list[tuple] = []
    preview_frames: dict[str, pd.DataFrame] = {}
    with xr.open_dataset(path) as dataset:
        data = dataset["harvarea"]
        lon_name, lat_name = lon_lat_names(dataset)
        lons = dataset[lon_name].values
        lats = dataset[lat_name].values
        for iso, boundary in boundaries.items():
            minx, miny, maxx, maxy = boundary.bounds
            lon_mask = (lons >= minx - 0.05) & (lons <= maxx + 0.05)
            lat_mask = (lats >= miny - 0.05) & (lats <= maxy + 0.05)
            if not lon_mask.any() or not lat_mask.any():
                continue
            subset = data.isel({lon_name: np.where(lon_mask)[0], lat_name: np.where(lat_mask)[0]})
            values = np.asarray(subset.values, dtype="float64")
            sub_lons = np.asarray(subset[lon_name].values, dtype="float64")
            sub_lats = np.asarray(subset[lat_name].values, dtype="float64")
            if data.dims.index(lat_name) > data.dims.index(lon_name):
                values = values.T
            lon_grid, lat_grid = np.meshgrid(sub_lons, sub_lats)
            valid = np.isfinite(values) & (values > 0)
            if not valid.any():
                preview_frames.setdefault(iso, pd.DataFrame(columns=["crop_code", "lon", "lat", "harvested_area"]))
                continue
            inside = contains_xy(boundary.geometry, lon_grid, lat_grid)
            valid &= inside
            if not valid.any():
                preview_frames.setdefault(iso, pd.DataFrame(columns=["crop_code", "lon", "lat", "harvested_area"]))
                continue
            flat_values = values[valid]
            flat_lons = lon_grid[valid]
            flat_lats = lat_grid[valid]
            order = np.argsort(flat_values)[::-1]
            preview_count = min(5000, len(order))
            preview_frames.setdefault(iso, pd.DataFrame())
            preview_frames[iso] = pd.concat(
                [
                    preview_frames[iso],
                    pd.DataFrame(
                        {
                            "crop_code": crop,
                            "lon": flat_lons[order[:preview_count]],
                            "lat": flat_lats[order[:preview_count]],
                            "harvested_area": flat_values[order[:preview_count]],
                        }
                    ),
                ],
                ignore_index=True,
            )
            rows.extend(
                weighted_cluster_representatives(
                    iso,
                    crop,
                    path,
                    boundary,
                    flat_lons,
                    flat_lats,
                    flat_values,
                    max_clusters,
                )
            )
    return rows, preview_frames


def create_tables(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS eq")
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eq.cropgrids_country_production_share_2020 (
                product text NOT NULL,
                iso3 text NOT NULL,
                country_name text,
                faostat_items text NOT NULL,
                production_tonnes_2020 double precision NOT NULL,
                world_production_tonnes_2020 double precision NOT NULL,
                country_share_world_pct double precision,
                selected_countries_production_tonnes_2020 double precision NOT NULL,
                selected_countries_share_world_pct double precision,
                PRIMARY KEY (product, iso3)
            );
            CREATE TABLE IF NOT EXISTS eq.cropgrids_selected_product_share_2020 (
                product text PRIMARY KEY,
                faostat_items text NOT NULL,
                world_production_tonnes_2020 double precision NOT NULL,
                selected_countries_production_tonnes_2020 double precision NOT NULL,
                selected_countries_share_world_pct double precision
            );
            CREATE TABLE IF NOT EXISTS eq.cropgrids_country_harvested_area_share_2020 (
                product text NOT NULL,
                iso3 text NOT NULL,
                cropgrids_columns text NOT NULL,
                harvested_area_ha_2020 double precision NOT NULL,
                world_harvested_area_ha_2020 double precision NOT NULL,
                country_share_world_harvested_area_pct double precision,
                selected_countries_harvested_area_ha_2020 double precision NOT NULL,
                selected_countries_share_world_harvested_area_pct double precision,
                PRIMARY KEY (product, iso3)
            );
            CREATE TABLE IF NOT EXISTS eq.crop_transport_vulnerability (
                crop_code text PRIMARY KEY,
                vulnerability_class text NOT NULL,
                vulnerability_score double precision NOT NULL,
                main_mechanism text NOT NULL,
                evidence_note text NOT NULL,
                source_rows text NOT NULL
            );
            CREATE TABLE IF NOT EXISTS eq.crop_origin_candidates (
                country_code text NOT NULL,
                crop_code text NOT NULL,
                candidate_rank integer NOT NULL,
                harvested_area double precision NOT NULL,
                lon double precision NOT NULL,
                lat double precision NOT NULL,
                source_file text NOT NULL,
                geometry geometry(Point, 4326) GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED,
                PRIMARY KEY (country_code, crop_code, candidate_rank)
            );
            CREATE INDEX IF NOT EXISTS crop_origin_candidates_country_crop_idx
                ON eq.crop_origin_candidates (country_code, crop_code, candidate_rank);
            CREATE INDEX IF NOT EXISTS crop_origin_candidates_geometry_gist
                ON eq.crop_origin_candidates USING GIST (geometry);
            ALTER TABLE eq.crop_origin_candidates
                ADD COLUMN IF NOT EXISTS cluster_cell_count integer,
                ADD COLUMN IF NOT EXISTS representative_cell_harvested_area double precision,
                ADD COLUMN IF NOT EXISTS cluster_share double precision,
                ADD COLUMN IF NOT EXISTS representative_distance_m double precision,
                ADD COLUMN IF NOT EXISTS metric_crs text;
            DROP TABLE IF EXISTS eq.cropgrids_origin_candidates;
            CREATE TABLE eq.cropgrids_origin_candidates (LIKE eq.crop_origin_candidates INCLUDING ALL);
            """
        )
    conn.commit()


def copy_dataframe(conn: psycopg.Connection, table: str, frame: pd.DataFrame, columns: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {table}")
        with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, DELIMITER E'\\t', NULL '')") as cp:
            for row in frame[columns].itertuples(index=False, name=None):
                cp.write_row(row)
        cur.execute(f"ANALYZE {table}")
    conn.commit()


def write_vulnerability(out_dir: Path) -> pd.DataFrame:
    frame = pd.DataFrame(CROP_TRANSPORT_VULNERABILITY)
    frame.to_csv(out_dir / "crop_transport_vulnerability.csv", index=False)
    return frame


def load_candidates(conn: psycopg.Connection, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE eq.crop_origin_candidates")
        with cur.copy(
            """
            COPY eq.crop_origin_candidates (
                country_code, crop_code, candidate_rank, harvested_area, lon, lat, source_file,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                representative_distance_m, metric_crs
            ) FROM STDIN WITH (FORMAT csv, DELIMITER E'\t', NULL '')
            """
        ) as cp:
            for row in rows:
                cp.write_row(row)
        cur.execute("TRUNCATE eq.cropgrids_origin_candidates")
        cur.execute(
            """
            INSERT INTO eq.cropgrids_origin_candidates (
                country_code, crop_code, candidate_rank, harvested_area, lon, lat, source_file,
                cluster_cell_count, representative_cell_harvested_area, cluster_share,
                representative_distance_m, metric_crs
            )
            SELECT country_code, crop_code, candidate_rank, harvested_area, lon, lat, source_file,
                   cluster_cell_count, representative_cell_harvested_area, cluster_share,
                   representative_distance_m, metric_crs
            FROM eq.crop_origin_candidates
            """
        )
        cur.execute("ANALYZE eq.crop_origin_candidates")
        cur.execute("ANALYZE eq.cropgrids_origin_candidates")
    conn.commit()


def build_origin_nodes(conn: psycopg.Connection, countries: list[str]) -> None:
    for iso in countries:
        suffix = iso.lower()
        nodes = f"road_graph_nodes_{suffix}"
        origin_nodes = f"crop_origin_nodes_{suffix}"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass(%s)
                """,
                (f"eq.{nodes}",),
            )
            if cur.fetchone()[0] is None:
                log(f"[skip] {iso} origin node snapping: missing {nodes}")
                continue
            cur.execute(f"DROP TABLE IF EXISTS eq.{qident(origin_nodes)}")
            cur.execute(
                f"""
                CREATE TABLE eq.{qident(origin_nodes)} AS
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area, o.lon, o.lat,
                       o.cluster_cell_count, o.representative_cell_harvested_area, o.cluster_share,
                       o.representative_distance_m, o.metric_crs,
                       n.node_id, ST_Distance(o.geometry::geography, n.geometry::geography) AS node_distance_m, o.geometry
                FROM eq.crop_origin_candidates o
                LEFT JOIN LATERAL (
                    SELECT node_id, geometry
                    FROM eq.{qident(nodes)} n
                    ORDER BY n.geometry <-> o.geometry
                    LIMIT 1
                ) n ON true
                WHERE o.country_code = %s
                """
                ,
                (iso,),
            )
            cur.execute(f"ALTER TABLE eq.{qident(origin_nodes)} ADD PRIMARY KEY (country_code, crop_code, candidate_rank)")
            cur.execute(f"CREATE INDEX {origin_nodes}_node_idx ON eq.{qident(origin_nodes)} (node_id)")
            cur.execute(f"CREATE INDEX {origin_nodes}_geom_idx ON eq.{qident(origin_nodes)} USING GIST (geometry)")
            cur.execute(f"ANALYZE eq.{qident(origin_nodes)}")
        conn.commit()
        log(f"[done] {iso} crop_origin_nodes")


def cleanup_old_results(conn: psycopg.Connection, out_dir: Path) -> None:
    audit = out_dir / "old_spam_tables_before_drop.csv"
    query = """
        SELECT schemaname, tablename
        FROM pg_tables
        WHERE schemaname = 'eq'
          AND (
            tablename LIKE 'crop_origin_selected_%'
            OR tablename LIKE 'crop_origin_nodes_%'
            OR tablename LIKE 'crop_accessibility_astar_od_%'
            OR tablename IN ('crop_accessibility_weekly_astar', 'crop_accessibility_weekly_bra', 'crop_accessibility_baseline_bra')
          )
        ORDER BY tablename
    """
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    pd.DataFrame(rows, columns=["schemaname", "tablename"]).to_csv(audit, index=False)
    with conn.cursor() as cur:
        for _, table in rows:
            cur.execute(f"DROP TABLE IF EXISTS eq.{qident(table)} CASCADE")
    conn.commit()

    old_dirs = [
        ROOT / "outputs/astar_accessibility_weekly/weekly_sum_penalty_v1_top5_per_crop",
        ROOT / "outputs/astar_accessibility_weekly/weekly_sum_penalty_v1_top5_per_crop_delta_minutes_heatmaps",
        ROOT / "outputs/astar_accessibility_weekly/aggregate_crop_weekly_summary",
    ]
    archive_dir = out_dir / "deleted_old_result_manifests"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in old_dirs:
        if path.exists():
            manifest = path / "manifest.json"
            if manifest.exists():
                shutil.copy2(manifest, archive_dir / f"{path.name}_manifest.json")
            shutil.rmtree(path)


def render_country_maps(boundaries: dict[str, CountryBoundary], preview_frames: dict[str, pd.DataFrame], out_dir: Path) -> None:
    png_dir = out_dir / "country_crop_distribution_png"
    png_dir.mkdir(parents=True, exist_ok=True)
    for iso, boundary in boundaries.items():
        frame = preview_frames.get(iso, pd.DataFrame())
        fig, axes = plt.subplots(1, len(CROP_LAYERS), figsize=(18, 4.2), constrained_layout=True)
        if len(CROP_LAYERS) == 1:
            axes = [axes]
        boundary_gdf = gpd.GeoDataFrame({"iso3": [iso]}, geometry=[boundary.geometry], crs="EPSG:4326")
        for ax, crop in zip(axes, CROP_LAYERS, strict=True):
            boundary_gdf.boundary.plot(ax=ax, color="#333333", linewidth=0.7)
            sub = frame[frame["crop_code"].eq(crop)]
            if not sub.empty:
                sizes = np.clip(np.sqrt(sub["harvested_area"].to_numpy(dtype=float)), 4, 45)
                ax.scatter(
                    sub["lon"],
                    sub["lat"],
                    c=sub["harvested_area"],
                    s=sizes,
                    cmap="viridis",
                    alpha=0.78,
                    linewidths=0,
                )
            ax.set_title(crop)
            minx, miny, maxx, maxy = boundary.bounds
            pad_x = max((maxx - minx) * 0.05, 0.1)
            pad_y = max((maxy - miny) * 0.05, 0.1)
            ax.set_xlim(minx - pad_x, maxx + pad_x)
            ax.set_ylim(miny - pad_y, maxy + pad_y)
            ax.set_xticks([])
            ax.set_yticks([])
        fig.suptitle(f"{iso} CROPGRIDS 2020 harvested-area distribution; plotted cells are top positive cells per crop")
        path = png_dir / f"{iso}_cropgrids_crop_distribution.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)


def fetch_cluster_node_frames(conn: psycopg.Connection, countries: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for iso in countries:
        suffix = iso.lower()
        origin_nodes = f"crop_origin_nodes_{suffix}"
        road_nodes = f"road_graph_nodes_{suffix}"
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s), to_regclass(%s)", (f"eq.{origin_nodes}", f"eq.{road_nodes}"))
            origin_exists, nodes_exist = cur.fetchone()
            if origin_exists is None or nodes_exist is None:
                continue
            cur.execute(
                f"""
                SELECT o.country_code, o.crop_code, o.candidate_rank, o.harvested_area,
                       o.lon AS representative_lon, o.lat AS representative_lat,
                       o.cluster_cell_count, o.cluster_share, o.representative_distance_m,
                       o.node_id, o.node_distance_m,
                       ST_X(n.geometry) AS node_lon, ST_Y(n.geometry) AS node_lat
                FROM eq.{qident(origin_nodes)} o
                JOIN eq.{qident(road_nodes)} n ON n.node_id = o.node_id
                WHERE o.country_code = %s
                ORDER BY o.crop_code, o.candidate_rank
                """,
                (iso,),
            )
            rows = cur.fetchall()
            frames[iso] = pd.DataFrame(
                rows,
                columns=[
                    "country_code",
                    "crop_code",
                    "candidate_rank",
                    "harvested_area",
                    "representative_lon",
                    "representative_lat",
                    "cluster_cell_count",
                    "cluster_share",
                    "representative_distance_m",
                    "node_id",
                    "node_distance_m",
                    "node_lon",
                    "node_lat",
                ],
            )
    return frames


def render_cluster_node_maps(
    boundaries: dict[str, CountryBoundary],
    preview_frames: dict[str, pd.DataFrame],
    cluster_node_frames: dict[str, pd.DataFrame],
    out_dir: Path,
) -> None:
    png_dir = out_dir / "country_crop_cluster_node_png"
    png_dir.mkdir(parents=True, exist_ok=True)
    for iso, boundary in boundaries.items():
        raw = preview_frames.get(iso, pd.DataFrame())
        clusters = cluster_node_frames.get(iso, pd.DataFrame())
        fig, axes = plt.subplots(1, len(CROP_LAYERS), figsize=(18, 4.2), constrained_layout=True)
        if len(CROP_LAYERS) == 1:
            axes = [axes]
        boundary_gdf = gpd.GeoDataFrame({"iso3": [iso]}, geometry=[boundary.geometry], crs="EPSG:4326")
        for ax, crop in zip(axes, CROP_LAYERS, strict=True):
            boundary_gdf.boundary.plot(ax=ax, color="#333333", linewidth=0.7)
            raw_sub = raw[raw["crop_code"].eq(crop)]
            if not raw_sub.empty:
                sizes = np.clip(np.sqrt(raw_sub["harvested_area"].to_numpy(dtype=float)), 2, 18)
                ax.scatter(
                    raw_sub["lon"],
                    raw_sub["lat"],
                    c="#7a7a7a",
                    s=sizes,
                    alpha=0.22,
                    linewidths=0,
                )
            cluster_sub = clusters[clusters["crop_code"].eq(crop)]
            if not cluster_sub.empty:
                for row in cluster_sub.itertuples(index=False):
                    ax.plot(
                        [row.representative_lon, row.node_lon],
                        [row.representative_lat, row.node_lat],
                        color="#111111",
                        linewidth=0.35,
                        alpha=0.5,
                    )
                cluster_sizes = np.clip(np.sqrt(cluster_sub["harvested_area"].to_numpy(dtype=float)), 35, 170)
                ax.scatter(
                    cluster_sub["representative_lon"],
                    cluster_sub["representative_lat"],
                    s=cluster_sizes,
                    facecolors="none",
                    edgecolors="#d7191c",
                    linewidths=1.4,
                    label="cluster representative",
                )
                ax.scatter(
                    cluster_sub["node_lon"],
                    cluster_sub["node_lat"],
                    s=22,
                    c="#2c7bb6",
                    marker="x",
                    linewidths=1.0,
                    label="snapped road node",
                )
            ax.set_title(crop)
            minx, miny, maxx, maxy = boundary.bounds
            pad_x = max((maxx - minx) * 0.05, 0.1)
            pad_y = max((maxy - miny) * 0.05, 0.1)
            ax.set_xlim(minx - pad_x, maxx + pad_x)
            ax.set_ylim(miny - pad_y, maxy + pad_y)
            ax.set_xticks([])
            ax.set_yticks([])
        fig.suptitle(f"{iso} CROPGRIDS clusters: red circles are selected cluster cells, blue x are snapped road nodes")
        path = png_dir / f"{iso}_cropgrids_clusters_and_nodes.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)


def fetch_destination_context_frames(
    conn: psycopg.Connection,
    countries: list[str],
    boundaries: dict[str, CountryBoundary],
) -> dict[str, dict[str, pd.DataFrame]]:
    frames: dict[str, dict[str, pd.DataFrame]] = {}
    for iso in countries:
        suffix = iso.lower()
        frames[iso] = {}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT geoname_id, name, population, lon, lat
                FROM eq.city_destinations_5k_100k
                WHERE country_code = %s
                ORDER BY population DESC NULLS LAST
                """,
                (iso,),
            )
            frames[iso]["city_5_100k"] = pd.DataFrame(
                cur.fetchall(),
                columns=["geoname_id", "name", "population", "lon", "lat"],
            )

            cur.execute(
                """
                SELECT geoname_id, name, population, lon, lat
                FROM eq.city_destinations
                WHERE country_code = %s AND population >= 100000
                ORDER BY population DESC NULLS LAST
                """,
                (iso,),
            )
            frames[iso]["city_100k_plus"] = pd.DataFrame(
                cur.fetchall(),
                columns=["geoname_id", "name", "population", "lon", "lat"],
            )

            minx, miny, maxx, maxy = boundaries[iso].bounds
            cur.execute(
                """
                SELECT port_id, name, natlscale, lon, lat
                FROM eq.port_destinations
                WHERE lon BETWEEN %s AND %s AND lat BETWEEN %s AND %s
                ORDER BY natlscale DESC NULLS LAST, name NULLS LAST
                """,
                (minx - 0.1, maxx + 0.1, miny - 0.1, maxy + 0.1),
            )
            ports = pd.DataFrame(cur.fetchall(), columns=["port_id", "name", "natlscale", "lon", "lat"])
            if not ports.empty:
                inside = contains_xy(boundaries[iso].geometry, ports["lon"].to_numpy(float), ports["lat"].to_numpy(float))
                ports = ports.loc[inside].copy()
            frames[iso]["ports"] = ports
    return frames


def render_destination_context_maps(
    boundaries: dict[str, CountryBoundary],
    cluster_node_frames: dict[str, pd.DataFrame],
    destination_frames: dict[str, dict[str, pd.DataFrame]],
    out_dir: Path,
) -> None:
    png_dir = out_dir / "country_crop_destination_context_png"
    png_dir.mkdir(parents=True, exist_ok=True)
    for iso, boundary in boundaries.items():
        clusters = cluster_node_frames.get(iso, pd.DataFrame())
        destinations = destination_frames.get(iso, {})
        city_5_100 = destinations.get("city_5_100k", pd.DataFrame())
        city_100 = destinations.get("city_100k_plus", pd.DataFrame())
        ports = destinations.get("ports", pd.DataFrame())

        fig, ax = plt.subplots(1, 1, figsize=(8.5, 8.5), constrained_layout=True)
        boundary_gdf = gpd.GeoDataFrame({"iso3": [iso]}, geometry=[boundary.geometry], crs="EPSG:4326")
        boundary_gdf.boundary.plot(ax=ax, color="#222222", linewidth=0.9)

        if not city_5_100.empty:
            ax.scatter(
                city_5_100["lon"],
                city_5_100["lat"],
                s=5,
                c="#4daf4a",
                alpha=0.28,
                linewidths=0,
                label="cities 5-100k",
                zorder=2,
            )
        if not city_100.empty:
            sizes = np.clip(np.sqrt(city_100["population"].fillna(100000).to_numpy(float)) / 12.0, 24, 110)
            ax.scatter(
                city_100["lon"],
                city_100["lat"],
                s=sizes,
                c="#ff7f00",
                marker="s",
                alpha=0.72,
                edgecolors="#7f3b08",
                linewidths=0.35,
                label="cities 100k+",
                zorder=3,
            )
        if not ports.empty:
            ax.scatter(
                ports["lon"],
                ports["lat"],
                s=90,
                c="#111111",
                marker="*",
                edgecolors="#fdbf6f",
                linewidths=0.55,
                label="ports in country",
                zorder=4,
            )

        if not clusters.empty:
            for crop, sub in clusters.groupby("crop_code"):
                color = CROP_COLORS.get(str(crop), "#d7191c")
                sizes = np.clip(np.sqrt(sub["harvested_area"].to_numpy(dtype=float)), 35, 160)
                ax.scatter(
                    sub["representative_lon"],
                    sub["representative_lat"],
                    s=sizes,
                    facecolors="none",
                    edgecolors=color,
                    linewidths=1.45,
                    alpha=0.95,
                    label=f"{crop} cluster",
                    zorder=5,
                )
                ax.scatter(
                    sub["node_lon"],
                    sub["node_lat"],
                    s=20,
                    c=color,
                    marker="x",
                    linewidths=0.9,
                    zorder=6,
                )
            ax.scatter(
                [],
                [],
                s=20,
                c="#333333",
                marker="x",
                linewidths=0.9,
                label="snapped cluster node",
            )

        minx, miny, maxx, maxy = boundary.bounds
        pad_x = max((maxx - minx) * 0.06, 0.1)
        pad_y = max((maxy - miny) * 0.06, 0.1)
        ax.set_xlim(minx - pad_x, maxx + pad_x)
        ax.set_ylim(miny - pad_y, maxy + pad_y)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(
            f"{iso} crop clusters with ports and city destinations\n"
            f"clusters={len(clusters):,}; ports={len(ports):,}; cities 5-100k={len(city_5_100):,}; cities 100k+={len(city_100):,}"
        )
        ax.legend(loc="lower left", fontsize=7, frameon=True, framealpha=0.86, ncols=2)
        path = png_dir / f"{iso}_crop_clusters_ports_cities.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)


def main() -> None:
    args = parse_args()
    countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()] if args.countries else manifest_countries()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "selected_countries.json").write_text(json.dumps(countries, indent=2))
    boundaries = load_boundaries(countries)
    log(f"[countries] {len(countries)}")

    production, production_summary = compute_production_shares(Path(args.faostat_zip), countries, out_dir)
    area = compute_cropgrids_country_area(Path(args.cropgrids_country_xlsx), countries, out_dir)
    vulnerability = write_vulnerability(out_dir)

    extract_dir = ROOT / "data/raw/cropgrids/selected_nc"
    layer_paths = extract_crop_layers(Path(args.cropgrids_zip), extract_dir)
    all_rows: list[tuple] = []
    preview_frames: dict[str, pd.DataFrame] = {}
    for crop, path in layer_paths.items():
        log(f"[load] {crop} {path.name}")
        rows, previews = crop_candidates_from_layer(crop, path, boundaries, args.max_clusters_per_crop)
        all_rows.extend(rows)
        for iso, frame in previews.items():
            preview_frames[iso] = pd.concat([preview_frames.get(iso, pd.DataFrame()), frame], ignore_index=True)
        log(f"[load] {crop} candidate_rows={len(rows):,}")
    candidates = pd.DataFrame(
        all_rows,
        columns=[
            "country_code",
            "crop_code",
            "candidate_rank",
            "harvested_area",
            "lon",
            "lat",
            "source_file",
            "cluster_cell_count",
            "representative_cell_harvested_area",
            "cluster_share",
            "representative_distance_m",
            "metric_crs",
        ],
    )
    candidates.to_csv(out_dir / "cropgrids_origin_candidates.csv", index=False)
    candidate_summary = (
        candidates.groupby(["country_code", "crop_code"], dropna=False)
        .agg(
            clusters=("candidate_rank", "count"),
            clustered_harvested_area=("harvested_area", "sum"),
            source_cells=("cluster_cell_count", "sum"),
            max_representative_distance_m=("representative_distance_m", "max"),
        )
        .reset_index()
    )
    candidate_summary.to_csv(out_dir / "cropgrids_origin_candidate_summary.csv", index=False)

    with psycopg.connect(args.db_url) as conn:
        conn.execute("SET statement_timeout = 0")
        create_tables(conn)
        copy_dataframe(
            conn,
            "eq.cropgrids_country_production_share_2020",
            production,
            [
                "product",
                "iso3",
                "country_name",
                "faostat_items",
                "production_tonnes_2020",
                "world_production_tonnes_2020",
                "country_share_world_pct",
                "selected_countries_production_tonnes_2020",
                "selected_countries_share_world_pct",
            ],
        )
        copy_dataframe(
            conn,
            "eq.cropgrids_selected_product_share_2020",
            production_summary,
            [
                "product",
                "faostat_items",
                "world_production_tonnes_2020",
                "selected_countries_production_tonnes_2020",
                "selected_countries_share_world_pct",
            ],
        )
        copy_dataframe(
            conn,
            "eq.cropgrids_country_harvested_area_share_2020",
            area,
            [
                "product",
                "iso3",
                "cropgrids_columns",
                "harvested_area_ha_2020",
                "world_harvested_area_ha_2020",
                "country_share_world_harvested_area_pct",
                "selected_countries_harvested_area_ha_2020",
                "selected_countries_share_world_harvested_area_pct",
            ],
        )
        copy_dataframe(
            conn,
            "eq.crop_transport_vulnerability",
            vulnerability,
            [
                "crop_code",
                "vulnerability_class",
                "vulnerability_score",
                "main_mechanism",
                "evidence_note",
                "source_rows",
            ],
        )
        load_candidates(conn, all_rows)
        if not args.skip_cleanup:
            cleanup_old_results(conn, out_dir)
        build_origin_nodes(conn, countries)
        cluster_node_frames = fetch_cluster_node_frames(conn, countries)
        destination_frames = fetch_destination_context_frames(conn, countries, boundaries)

    render_country_maps(boundaries, preview_frames, out_dir)
    render_cluster_node_maps(boundaries, preview_frames, cluster_node_frames, out_dir)
    render_destination_context_maps(boundaries, cluster_node_frames, destination_frames, out_dir)
    manifest = {
        "countries": countries,
        "crop_layers": CROP_LAYERS,
        "products": PRODUCTS,
        "max_clusters_per_country_crop": args.max_clusters_per_crop,
        "candidate_rows": len(all_rows),
        "outputs": {
            "candidate_csv": str(out_dir / "cropgrids_origin_candidates.csv"),
            "candidate_summary_csv": str(out_dir / "cropgrids_origin_candidate_summary.csv"),
            "production_share_csv": str(out_dir / "faostat_2020_selected_countries_product_share_by_country.csv"),
            "production_summary_csv": str(out_dir / "faostat_2020_selected_countries_product_share_summary.csv"),
            "vulnerability_csv": str(out_dir / "crop_transport_vulnerability.csv"),
            "png_dir": str(out_dir / "country_crop_distribution_png"),
            "cluster_node_png_dir": str(out_dir / "country_crop_cluster_node_png"),
            "destination_context_png_dir": str(out_dir / "country_crop_destination_context_png"),
        },
    }
    (out_dir / "cropgrids_migration_manifest.json").write_text(json.dumps(manifest, indent=2))
    log(f"[done] candidate_rows={len(all_rows):,} out_dir={out_dir}")


if __name__ == "__main__":
    main()
