#!/usr/bin/env python3

from __future__ import annotations

import argparse
import struct
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import numpy as np
import pandas as pd
import pycountry
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from tqdm import trange


CROP_FILE = "Table_CROPGRIDSv1.08_COU.xlsx"
EXCEL_DATA_FILE = "41467_2019_10442_MOESM3_ESM.xlsx"
SPAM_ZIP = "spam2010v2r0_global_harv_area.geotiff.zip"
GADM_BASE_URL = "https://geodata.ucdavis.edu/gadm/gadm4.1/shp"

CROPS = {
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load crop, hazard, GADM, and optional SPAM harvested-area data.",
    )
    parser.add_argument(
        "--country-code",
        default="BOL",
        help="ISO3 country code or ALL.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory with the Excel and SPAM zip inputs.",
    )
    parser.add_argument(
        "--gadm-cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "gadm_cache",
        help="Directory for downloaded GADM archives.",
    )
    parser.add_argument(
        "--spam-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "spam_tifs",
        help="Directory for extracted SPAM GeoTIFFs.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "outputs",
        help="Directory for CSV outputs.",
    )
    parser.add_argument(
        "--run-spam-zonal-stats",
        action="store_true",
        help="Force SPAM zonal statistics even for ALL scope.",
    )
    parser.add_argument(
        "--skip-spam-zonal-stats",
        action="store_true",
        help="Skip SPAM zonal statistics even for single-country scope.",
    )
    return parser.parse_args()


def download_file(url: str, target_path: Path) -> Path:
    if target_path.exists():
        return target_path
    print(f"downloading {target_path.name} from {url}")
    urlretrieve(url, target_path)
    return target_path


def iso3_to_iso2(iso3: str) -> str:
    country = pycountry.countries.get(alpha_3=iso3)
    if country is None:
        raise ValueError(f"Unknown ISO3 code: {iso3}")
    return country.alpha_2


def extract_country_code(series: pd.Series) -> pd.Series:
    return series.astype(str).str.extract(r"^([A-Z]{3})", expand=False)


def ensure_gadm_zip(iso3: str, gadm_cache_dir: Path) -> Path:
    zip_path = gadm_cache_dir / f"gadm41_{iso3}_shp.zip"
    url = f"{GADM_BASE_URL}/gadm41_{iso3}_shp.zip"
    zip_path = download_file(url, zip_path)
    if zipfile.is_zipfile(zip_path):
        return zip_path
    print(f"cached archive is invalid, re-downloading {zip_path.name}")
    zip_path.unlink(missing_ok=True)
    return download_file(url, zip_path)


def ensure_gadm_extract_dir(iso3: str, gadm_cache_dir: Path) -> Path:
    zip_path = ensure_gadm_zip(iso3, gadm_cache_dir)
    extract_dir = gadm_cache_dir / f"gadm41_{iso3}_shp"
    if not zipfile.is_zipfile(zip_path):
        raise zipfile.BadZipFile(f"Cached GADM archive is not a valid zip: {zip_path}")
    needs_extract = not extract_dir.exists() or not any(extract_dir.iterdir())
    if needs_extract:
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as archive:
            archive.extractall(extract_dir)
    return extract_dir


def find_gadm_component(
    extract_dir: Path,
    iso3: str,
    suffix: str,
    preferred_level: int = 2,
) -> tuple[Path, int]:
    for level in trange(preferred_level, -1, -1, leave=False):
        candidate = extract_dir / f"gadm41_{iso3}_{level}.{suffix}"
        if candidate.exists():
            return candidate, level
    fallback_candidates = sorted(extract_dir.glob(f"gadm41_{iso3}_*.{suffix}"))
    for candidate in fallback_candidates:
        level_part = candidate.stem.rsplit("_", 1)[-1]
        if level_part.isdigit():
            return candidate, int(level_part)
    raise FileNotFoundError(f"No GADM .{suffix} file found for {iso3}")


def read_dbf(raw_bytes: bytes) -> pd.DataFrame:
    header = raw_bytes[:32]
    num_records = struct.unpack_from("<I", header, 4)[0]
    header_size = struct.unpack_from("<H", header, 8)[0]
    record_size = struct.unpack_from("<H", header, 10)[0]
    fields = []
    pos = 32
    while raw_bytes[pos] != 0x0D:
        field_name = raw_bytes[pos : pos + 11].split(b"\x00")[0].decode("latin-1").strip()
        field_len = raw_bytes[pos + 16]
        fields.append((field_name, field_len))
        pos += 32

    records = []
    pos = header_size
    for _ in range(num_records):
        if raw_bytes[pos] == 0x2A:
            pos += record_size
            continue
        pos += 1
        record = {}
        for field_name, field_len in fields:
            record[field_name] = raw_bytes[pos : pos + field_len].decode("latin-1").strip()
            pos += field_len
        records.append(record)
    return pd.DataFrame(records)


def load_excel_sheet_by_required_columns(
    path: Path,
    required_columns: list[str],
) -> tuple[pd.DataFrame, str]:
    workbook = pd.ExcelFile(path)
    for sheet_name in workbook.sheet_names:
        dataframe = pd.read_excel(workbook, sheet_name=sheet_name)
        if len(dataframe.columns) and str(dataframe.columns[0]).startswith("Unnamed"):
            dataframe = dataframe.rename(columns={dataframe.columns[0]: "idx"})
        if set(required_columns).issubset(dataframe.columns):
            return dataframe, sheet_name
    raise ValueError(f"No worksheet in {path} contains columns: {required_columns}")


def normalize_gadm_attributes(attr_df: pd.DataFrame) -> pd.DataFrame:
    result = attr_df.copy()
    for column in ["NAME_0", "NAME_1", "NAME_2"]:
        if column in result.columns:
            result[column] = (
                result[column]
                .astype(str)
                .str.encode("latin-1", errors="ignore")
                .str.decode("utf-8", errors="replace")
            )
    if "GID_2" in result.columns:
        result["GID_2_risk"] = (
            result["GID_2"]
            .str.replace(".", "_", regex=False)
            .str.replace(r"_2$", "_1", regex=True)
        )
    return result


def load_tabular_inputs(data_dir: Path, country_code: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    risk_df, risk_sheet_name = load_excel_sheet_by_required_columns(
        data_dir / EXCEL_DATA_FILE,
        ["GID_2", "tot_risk_road", "all_risk_road", "rel_risk"],
    )
    dom_df, dom_sheet_name = load_excel_sheet_by_required_columns(
        data_dir / EXCEL_DATA_FILE,
        ["GID_2", "dom_hz", "areadeg"],
    )

    risk_df["country_code"] = extract_country_code(risk_df["GID_2"])
    dom_df["country_code"] = extract_country_code(dom_df["GID_2"])

    for column in ["tot_risk_road", "all_risk_road", "rel_risk"]:
        risk_df[column] = pd.to_numeric(risk_df[column], errors="coerce")

    crop_df = pd.read_excel(data_dir / CROP_FILE, sheet_name="Harvested area (ha)")
    iso2_col = next((c for c in crop_df.columns if "ISO2" in str(c).upper()), crop_df.columns[0])
    name_col = next(
        (c for c in crop_df.columns if "COUNTRY NAME" in str(c).upper()),
        crop_df.columns[1],
    )
    crop_long = crop_df.melt(id_vars=[iso2_col, name_col], var_name="crop", value_name="harvested_ha")
    crop_long = crop_long.rename(columns={iso2_col: "country_iso2", name_col: "country_name"})
    crop_long["country_iso2"] = crop_long["country_iso2"].astype(str).str.strip()
    crop_long["harvested_ha"] = pd.to_numeric(crop_long["harvested_ha"], errors="coerce").fillna(0)
    crop_long = crop_long[crop_long["harvested_ha"] > 0].copy()

    if country_code == "ALL":
        risk_scope = risk_df.copy()
        dom_scope = dom_df.copy()
        crop_scope = crop_long.copy()
        target_iso3 = sorted(risk_scope["country_code"].dropna().unique())
    else:
        country_iso2 = iso3_to_iso2(country_code)
        risk_scope = risk_df[risk_df["country_code"] == country_code].copy()
        dom_scope = dom_df[dom_df["country_code"] == country_code].copy()
        crop_scope = crop_long[crop_long["country_iso2"] == country_iso2].copy()
        target_iso3 = [country_code]

    risk_scope = risk_scope.merge(dom_scope[["GID_2", "dom_hz", "areadeg"]], on="GID_2", how="left")

    print("risk sheet:", risk_sheet_name)
    print("hazard sheet:", dom_sheet_name)
    print("risk rows:", len(risk_scope))
    print("hazard rows:", len(dom_scope))
    print("crop rows:", len(crop_scope))
    print("target iso3 count:", len(target_iso3))

    return risk_scope, dom_scope, crop_scope, target_iso3


def load_gadm_shapes(target_iso3: list[str], gadm_cache_dir: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, pd.DataFrame]:
    country_frames = []
    admin_frames = []
    attr_frames = []

    for idx, iso3 in enumerate(target_iso3, start=1):
        print(f"[{idx}/{len(target_iso3)}] loading GADM for {iso3}")
        extract_dir = ensure_gadm_extract_dir(iso3, gadm_cache_dir)
        adm0_path, adm0_level = find_gadm_component(extract_dir, iso3, "shp", preferred_level=0)
        adm2_path, adm2_level = find_gadm_component(extract_dir, iso3, "shp", preferred_level=2)
        dbf_path, dbf_level = find_gadm_component(extract_dir, iso3, "dbf", preferred_level=2)

        country_gdf = gpd.read_file(adm0_path)
        admin_gdf = gpd.read_file(adm2_path)
        attr_df = read_dbf(dbf_path.read_bytes())

        country_gdf["country_code"] = iso3
        country_gdf["gadm_level"] = adm0_level
        admin_gdf["country_code"] = iso3
        admin_gdf["gadm_level"] = adm2_level
        attr_df["country_code"] = iso3
        attr_df["gadm_level"] = dbf_level
        attr_df = normalize_gadm_attributes(attr_df)

        country_frames.append(country_gdf)
        admin_frames.append(admin_gdf)
        attr_frames.append(attr_df)

    country_shapes = gpd.GeoDataFrame(
        pd.concat(country_frames, ignore_index=True),
        geometry="geometry",
        crs=country_frames[0].crs,
    )
    admin_shapes = gpd.GeoDataFrame(
        pd.concat(admin_frames, ignore_index=True),
        geometry="geometry",
        crs=admin_frames[0].crs,
    )
    gadm_attrs = pd.concat(attr_frames, ignore_index=True)

    print("country shapes:", len(country_shapes))
    print("admin shapes:", len(admin_shapes))
    print("gadm attrs:", len(gadm_attrs))

    return country_shapes, admin_shapes, gadm_attrs


def build_spam_inventory(
    spam_zip_path: Path,
    spam_dir: Path,
    run_spam_zonal_stats: bool,
) -> pd.DataFrame:
    spam_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(spam_zip_path) as archive:
        zip_members = {Path(name).name for name in archive.namelist() if name.endswith(".tif")}
        rows = []
        for code, crop_name in CROPS.items():
            filename = f"spam2010V2r0_global_H_{code}_A.tif"
            present_in_zip = filename in zip_members
            if present_in_zip and run_spam_zonal_stats:
                target_path = spam_dir / filename
                if not target_path.exists():
                    archive.extract(filename, spam_dir)
            rows.append(
                {
                    "crop_code": code,
                    "crop_name": crop_name,
                    "filename": filename,
                    "present_in_zip": present_in_zip,
                },
            )
    inventory = pd.DataFrame(rows)
    print(inventory)
    return inventory


def _safe_window_from_bounds(src: rasterio.io.DatasetReader, bounds: tuple[float, float, float, float]) -> Window | None:
    window = from_bounds(*bounds, transform=src.transform)
    full_window = Window(0, 0, src.width, src.height)
    col_off = max(0, int(np.floor(window.col_off)))
    row_off = max(0, int(np.floor(window.row_off)))
    col_end = min(src.width, int(np.ceil(window.col_off + window.width)))
    row_end = min(src.height, int(np.ceil(window.row_off + window.height)))
    clipped = Window(col_off, row_off, col_end - col_off, row_end - row_off).intersection(full_window)
    if clipped.width <= 0 or clipped.height <= 0:
        return None
    return clipped


def _zonal_sum_for_geometry(
    src: rasterio.io.DatasetReader,
    geometry,
    nodata_value: float = -1,
) -> float:
    window = _safe_window_from_bounds(src, geometry.bounds)
    if window is None:
        return 0.0

    data = src.read(1, window=window, masked=True)
    if data.size == 0:
        return 0.0

    geom_mask = geometry_mask(
        [geometry.__geo_interface__],
        out_shape=data.shape,
        transform=src.window_transform(window),
        invert=True,
    )
    valid_mask = geom_mask & ~np.ma.getmaskarray(data)
    if nodata_value is not None:
        valid_mask &= np.asarray(data) != nodata_value
    if not np.any(valid_mask):
        return 0.0
    return float(np.asarray(data)[valid_mask].sum())


def compute_spam_zonal_stats(admin_shapes: gpd.GeoDataFrame, spam_dir: Path) -> pd.DataFrame:
    records = []
    for code, crop_name in CROPS.items():
        tif_path = spam_dir / f"spam2010V2r0_global_H_{code}_A.tif"
        if not tif_path.exists():
            continue
        print(f"running zonal stats for {crop_name} from {tif_path.name}")
        with rasterio.open(tif_path) as src:
            zonal_sums = [
                _zonal_sum_for_geometry(src, geometry, nodata_value=-1)
                for geometry in admin_shapes.geometry
            ]
        for row_idx, zonal_sum in enumerate(zonal_sums):
            admin_row = admin_shapes.iloc[row_idx]
            records.append(
                {
                    "country_code": admin_row.get("country_code"),
                    "GID_2": admin_row.get("GID_2"),
                    "NAME_1": admin_row.get("NAME_1"),
                    "NAME_2": admin_row.get("NAME_2"),
                    "crop_name": crop_name,
                    "harv_ha": zonal_sum,
                },
            )
    spam_long = pd.DataFrame(records)
    if not spam_long.empty:
        spam_long["harv_ha"] = spam_long["harv_ha"].fillna(0.0)
    print("spam zonal rows:", len(spam_long))
    return spam_long


def write_outputs(
    output_dir: Path,
    risk_scope: pd.DataFrame,
    crop_scope: pd.DataFrame,
    country_shapes: gpd.GeoDataFrame,
    admin_shapes: gpd.GeoDataFrame,
    gadm_attrs: pd.DataFrame,
    spam_inventory: pd.DataFrame,
    spam_long: pd.DataFrame | None,
    country_code: str,
) -> None:
    run_dir = output_dir / country_code
    run_dir.mkdir(parents=True, exist_ok=True)

    risk_scope.to_csv(run_dir / "risk_scope.csv", index=False)
    crop_scope.to_csv(run_dir / "crop_scope.csv", index=False)
    gadm_attrs.to_csv(run_dir / "gadm_attrs.csv", index=False)
    spam_inventory.to_csv(run_dir / "spam_inventory.csv", index=False)
    country_shapes.to_file(run_dir / "country_shapes.geojson", driver="GeoJSON")
    admin_shapes.to_file(run_dir / "admin_shapes.geojson", driver="GeoJSON")

    if spam_long is not None:
        spam_long.to_csv(run_dir / "spam_zonal_stats.csv", index=False)

    print("wrote outputs to", run_dir)


def main() -> None:
    args = parse_args()
    country_code = (args.country_code or "ALL").strip().upper()
    if country_code != "ALL" and len(country_code) != 3:
        raise ValueError("COUNTRY_CODE must be a 3-letter ISO3 code or 'ALL'.")

    run_spam_zonal_stats = country_code != "ALL"
    if args.run_spam_zonal_stats:
        run_spam_zonal_stats = True
    if args.skip_spam_zonal_stats:
        run_spam_zonal_stats = False

    args.gadm_cache_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print("scope:", country_code)
    print("run spam zonal stats:", run_spam_zonal_stats)

    risk_scope, _, crop_scope, target_iso3 = load_tabular_inputs(args.data_dir, country_code)
    country_shapes, admin_shapes, gadm_attrs = load_gadm_shapes(target_iso3, args.gadm_cache_dir)
    spam_inventory = build_spam_inventory(
        args.data_dir / SPAM_ZIP,
        args.spam_dir,
        run_spam_zonal_stats,
    )

    spam_long = None
    if run_spam_zonal_stats:
        spam_long = compute_spam_zonal_stats(admin_shapes, args.spam_dir)
    else:
        print("Skipping zonal stats.")

    write_outputs(
        args.output_dir,
        risk_scope,
        crop_scope,
        country_shapes,
        admin_shapes,
        gadm_attrs,
        spam_inventory,
        spam_long,
        country_code,
    )


if __name__ == "__main__":
    main()
