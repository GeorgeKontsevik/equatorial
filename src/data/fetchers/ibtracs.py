"""Fetcher for the NOAA IBTrACS archive."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes


IBTRACS_ROOT = "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs"
IBTRACS_LICENSE_NOTE = "NOAA NCEI IBTrACS archive. Follow NOAA citation guidance and dataset documentation."


def _build_filename(subset: str, version: str, data_format: str) -> str:
    if data_format == "netcdf":
        return f"IBTrACS.{subset}.{version}.nc"
    if data_format == "csv":
        return f"ibtracs.{subset}.list.{version}.csv"
    raise ValueError(f"Unsupported IBTrACS format: {data_format}")


def _discover_mapping_filename(root_url: str, version: str, context) -> str | None:
    try:
        response = httpx.get(root_url, headers={"User-Agent": context.user_agent}, timeout=context.timeout_seconds, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    matches = re.findall(rf"IBTrACS_SerialNumber_NameMapping_{re.escape(version)}_[0-9]{{8}}\.txt", response.text)
    if not matches:
        return None
    return sorted(set(matches))[-1]


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download selected IBTrACS subsets in CSV or NetCDF format."""

    version = str(dataset_cfg.get("version", "v04r01"))
    data_format = str(dataset_cfg.get("format", "netcdf")).lower()
    subsets = [str(value).strip() for value in dataset_cfg.get("subsets", ["ALL"]) if str(value).strip()]
    temporal_resolution = str(dataset_cfg.get("temporal_resolution", "typically 6-hourly track positions"))
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "Track points / lines"))
    bbox = dataset_cfg.get("bbox")

    format_dir = "netcdf" if data_format == "netcdf" else "csv"
    root_url = f"{IBTRACS_ROOT}/{version}/access/{format_dir}"
    target_dir = ensure_directory(context.raw_root / "ibtracs" / "global" / version / format_dir)
    legacy_dir = context.raw_root / "ibtracs" / version / format_dir

    records: list[CatalogRecord] = []
    for subset in subsets:
        filename = _build_filename(subset, version, data_format)
        source_url = f"{root_url}/{filename}"
        legacy_path = legacy_dir / filename
        if legacy_path.exists() and not (target_dir / filename).exists():
            ensure_directory(target_dir)
            legacy_path.replace(target_dir / filename)
        local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
        records.append(
            downloaded_record(
                dataset_name="ibtracs",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=IBTRACS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes=join_notes(
                    f"IBTrACS subset `{subset}` in {data_format} format.",
                    "Reused an existing local copy." if reused else "Downloaded from the official NOAA NCEI access directory.",
                ),
            ),
        )

    if bool(dataset_cfg.get("download_name_mapping", True)):
        mapping_name = _discover_mapping_filename(root_url, version, context)
        if mapping_name:
            mapping_url = f"{root_url}/{mapping_name}"
            legacy_mapping_path = legacy_dir / mapping_name
            if legacy_mapping_path.exists() and not (target_dir / mapping_name).exists():
                ensure_directory(target_dir)
                legacy_mapping_path.replace(target_dir / mapping_name)
            mapping_path, reused = ensure_local_copy(mapping_url, target_dir / mapping_name, context)
            records.append(
                downloaded_record(
                    dataset_name="ibtracs",
                    source_url=mapping_url,
                    local_path=mapping_path,
                    context=context,
                    license_or_access_note=IBTRACS_LICENSE_NOTE,
                    spatial_resolution_raw="attribute lookup",
                    temporal_resolution="snapshot",
                    bbox=bbox,
                    notes=join_notes(
                        "Serial-number to storm-name mapping file.",
                        "Reused an existing local copy." if reused else "Discovered from the official NOAA NCEI directory listing.",
                    ),
                ),
            )
        elif context.logger:
            context.logger.warning("IBTrACS name-mapping file was not found in the NOAA directory listing for %s.", version)

    return records
