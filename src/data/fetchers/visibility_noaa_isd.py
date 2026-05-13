"""Fetcher for NOAA Global Hourly station visibility observations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd
import pycountry

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, manual_record, validate_download


HISTORY_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
GLOBAL_HOURLY_BASE_URL = "https://www.ncei.noaa.gov/data/global-hourly/access"
NOAA_LICENSE_NOTE = "NOAA/NCEI Global Hourly public data; review current NOAA use and citation guidance before redistribution."


def _download_text(url: str, target_path: Path, context) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(target_path.suffix + ".part")
    headers = {"User-Agent": context.user_agent}
    with httpx.stream("GET", url, timeout=context.timeout_seconds, headers=headers, follow_redirects=True) as response:
        response.raise_for_status()
        with temp_path.open("wb") as handle:
            for chunk in response.iter_bytes():
                if chunk:
                    handle.write(chunk)
    temp_path.replace(target_path)
    return target_path


def _parse_date(value: object, fallback: str) -> datetime:
    return datetime.strptime(str(value or fallback), "%Y-%m-%d")


def _station_id(row: pd.Series) -> str:
    usaf = str(row["USAF"]).strip().zfill(6)
    wban = str(row["WBAN"]).strip().zfill(5)
    return f"{usaf}{wban}"


def _load_history(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str).fillna("")
    required = {"USAF", "WBAN", "STATION NAME", "LAT", "LON", "BEGIN", "END"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"NOAA ISD history is missing required columns: {sorted(missing)}")
    frame["lat_num"] = pd.to_numeric(frame["LAT"], errors="coerce")
    frame["lon_num"] = pd.to_numeric(frame["LON"], errors="coerce")
    frame["begin_num"] = pd.to_numeric(frame["BEGIN"], errors="coerce")
    frame["end_num"] = pd.to_numeric(frame["END"], errors="coerce")
    return frame


def _select_stations(
    history: pd.DataFrame,
    bbox: list[float],
    start: datetime,
    end: datetime,
    max_stations: int,
    *,
    country_code: str = "",
) -> pd.DataFrame:
    minx, miny, maxx, maxy = [float(value) for value in bbox]
    start_num = int(start.strftime("%Y%m%d"))
    end_num = int(end.strftime("%Y%m%d"))
    subset = history.loc[
        history["lat_num"].between(miny, maxy)
        & history["lon_num"].between(minx, maxx)
        & (history["begin_num"] <= end_num)
        & (history["end_num"] >= start_num)
    ].copy()
    iso2 = ""
    iso3 = str(country_code).strip().upper()
    if iso3:
        country = pycountry.countries.get(alpha_3=iso3)
        if country is not None:
            iso2 = str(country.alpha_2).upper()
    if iso2 and "CTRY" in subset.columns:
        country_subset = subset.loc[subset["CTRY"].astype("string").str.upper() == iso2].copy()
        if not country_subset.empty:
            subset = country_subset
    subset["station_id"] = subset.apply(_station_id, axis=1)
    subset = subset.drop_duplicates("station_id").sort_values(["station_id"]).head(max_stations)
    return subset


def _has_visibility(csv_path: Path) -> bool:
    try:
        frame = pd.read_csv(csv_path, usecols=["DATE", "VIS"], nrows=500)
    except Exception:
        return False
    if "VIS" not in frame:
        return False
    return frame["VIS"].astype(str).str.extract(r"^(\d+)")[0].pipe(pd.to_numeric, errors="coerce").lt(999999).any()


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download NOAA station inventory and Global Hourly station CSVs with VIS fields."""

    bbox = dataset_cfg.get("bbox")
    if not bbox or len(bbox) != 4:
        return [
            manual_record(
                dataset_name="visibility_noaa_isd",
                source_url=HISTORY_URL,
                context=context,
                instruction_text="# Manual Steps For NOAA Visibility\n\nSet `datasets.visibility_noaa_isd.bbox` before fetching station data.\n",
                license_or_access_note=NOAA_LICENSE_NOTE,
                spatial_resolution_raw="station observations",
                temporal_resolution="hourly",
                bbox=bbox,
                notes="No bbox was configured for station selection.",
            )
        ]

    start = _parse_date(dataset_cfg.get("start_date"), "2024-01-01")
    end = _parse_date(dataset_cfg.get("end_date"), start.strftime("%Y-%m-%d"))
    max_stations = int(dataset_cfg.get("max_stations", 12))
    country_code = str(dataset_cfg.get("country_code") or context.project_root.name).upper()
    source_url = str(dataset_cfg.get("source_url", GLOBAL_HOURLY_BASE_URL))
    history_url = str(dataset_cfg.get("history_url", HISTORY_URL))
    target_root = ensure_directory(context.raw_root / "visibility_noaa_isd" / country_code)
    history_path = context.raw_root / "visibility_noaa_isd" / "isd-history.csv"

    if not history_path.exists():
        _download_text(history_url, history_path, context)
    history = _load_history(history_path)
    stations = _select_stations(history, bbox, start, end, max_stations, country_code=country_code)
    station_path = target_root / "stations.csv"
    stations.to_csv(station_path, index=False)

    if stations.empty:
        return [
            manual_record(
                dataset_name="visibility_noaa_isd",
                source_url=history_url,
                context=context,
                instruction_text=(
                    "# Manual Steps For NOAA Visibility\n\n"
                    "No NOAA ISD/Global Hourly stations were found inside the configured bbox and date window. "
                    "Use nearby stations manually or provide another visibility product in meters.\n"
                ),
                license_or_access_note=NOAA_LICENSE_NOTE,
                spatial_resolution_raw="station observations",
                temporal_resolution="hourly",
                bbox=bbox,
                notes="No stations found inside bbox/date window.",
            )
        ]

    records: list[CatalogRecord] = [
        downloaded_record(
            dataset_name="visibility_noaa_isd",
            source_url=history_url,
            local_path=station_path,
            context=context,
            license_or_access_note=NOAA_LICENSE_NOTE,
            spatial_resolution_raw="station metadata",
            temporal_resolution="static station inventory",
            bbox=bbox,
            notes=f"Selected {len(stations)} NOAA Global Hourly station(s) inside bbox/date window.",
        )
    ]

    years = range(start.year, end.year + 1)
    downloaded_any_visibility = False
    for _, station in stations.iterrows():
        station_id = str(station["station_id"])
        for year in years:
            url = f"{source_url.rstrip('/')}/{year}/{station_id}.csv"
            target_path = ensure_directory(target_root / str(year)) / f"{station_id}.csv"
            if not target_path.exists():
                try:
                    _download_text(url, target_path, context)
                except Exception as exc:
                    if context.logger:
                        context.logger.warning("NOAA visibility station download failed for %s: %s", url, exc)
                    target_path.unlink(missing_ok=True)
                    continue
            ok, _ = validate_download(target_path)
            if not ok:
                target_path.unlink(missing_ok=True)
                continue
            has_vis = _has_visibility(target_path)
            downloaded_any_visibility = downloaded_any_visibility or has_vis
            records.append(
                downloaded_record(
                    dataset_name="visibility_noaa_isd",
                    source_url=url,
                    local_path=target_path,
                    context=context,
                    license_or_access_note=NOAA_LICENSE_NOTE,
                    spatial_resolution_raw="station observations",
                    temporal_resolution="hourly",
                    bbox=bbox,
                    notes=f"NOAA Global Hourly station {station_id}; VIS present={has_vis}.",
                )
            )

    if not downloaded_any_visibility:
        records.append(
            manual_record(
                dataset_name="visibility_noaa_isd",
                source_url=source_url,
                context=context,
                instruction_text=(
                    "# Manual Steps For Visibility\n\n"
                    "NOAA station files were found, but no usable `VIS` values were detected. "
                    "Provide a visibility product in meters before activating dust visibility thresholds.\n"
                ),
                license_or_access_note=NOAA_LICENSE_NOTE,
                spatial_resolution_raw="station observations",
                temporal_resolution="hourly",
                bbox=bbox,
                notes="No usable VIS values detected in downloaded station files.",
                instruction_name="visibility_noaa_isd_no_vis_values",
            )
        )

    return records
