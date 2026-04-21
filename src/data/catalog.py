"""Catalog helpers for the local data lake metadata."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


CATALOG_FIELDS = [
    "dataset_name",
    "source_url",
    "download_date_utc",
    "local_path",
    "file_format",
    "license_or_access_note",
    "spatial_resolution_raw",
    "temporal_resolution",
    "bbox_if_known",
    "checksum_sha256",
    "status",
    "notes",
    "crs",
    "pixel_size",
    "geometry_type",
    "layer_names",
    "raster_shape",
]


@dataclass(slots=True)
class CatalogRecord:
    """Single metadata row for a downloaded, manual, skipped, or failed asset."""

    dataset_name: str
    source_url: str
    download_date_utc: str
    local_path: str
    file_format: str
    license_or_access_note: str
    spatial_resolution_raw: str
    temporal_resolution: str
    bbox_if_known: str
    checksum_sha256: str
    status: str
    notes: str
    crs: str = ""
    pixel_size: str = ""
    geometry_type: str = ""
    layer_names: str = ""
    raster_shape: str = ""

    def to_dict(self) -> dict[str, str]:
        """Convert the record to a dictionary with stable catalog columns."""

        row = asdict(self)
        return {field: str(row.get(field, "")) if row.get(field, "") is not None else "" for field in CATALOG_FIELDS}


def _empty_catalog_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=CATALOG_FIELDS)


def load_catalog(csv_path: Path) -> pd.DataFrame:
    """Load an existing catalog or return an empty frame with the expected schema."""

    if not csv_path.exists():
        return _empty_catalog_frame()

    frame = pd.read_csv(csv_path, dtype=str).fillna("")
    for field in CATALOG_FIELDS:
        if field not in frame.columns:
            frame[field] = ""
    return frame[CATALOG_FIELDS]


def upsert_records(existing: pd.DataFrame, records: Iterable[CatalogRecord]) -> pd.DataFrame:
    """Replace catalog rows for datasets touched in the current run and append new rows."""

    frame = existing.copy()
    incoming = list(records)
    touched_datasets = sorted({record.dataset_name for record in incoming})
    if touched_datasets:
        frame = frame.loc[~frame["dataset_name"].isin(touched_datasets)].copy()

    for record in incoming:
        row = record.to_dict()
        frame = pd.concat([frame, pd.DataFrame([row])], ignore_index=True)

    frame = frame[CATALOG_FIELDS].sort_values(["dataset_name", "local_path"], kind="stable").reset_index(drop=True)
    return frame


def write_catalog(frame: pd.DataFrame, csv_path: Path, json_path: Path) -> None:
    """Persist the catalog to CSV and JSON."""

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = frame[CATALOG_FIELDS].fillna("")
    ordered.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(ordered.to_dict(orient="records"), indent=2), encoding="utf-8")


def summarize_status(frame: pd.DataFrame) -> dict[str, list[str]]:
    """Return dataset names grouped by status for console summaries."""

    summary: dict[str, list[str]] = {}
    if frame.empty:
        return summary

    for status, group in frame.groupby("status"):
        summary[status] = sorted(group["dataset_name"].astype(str).unique().tolist())
    return summary


def build_inventory_report(frame: pd.DataFrame) -> str:
    """Create a human-readable inventory report from the current catalog."""

    downloaded = frame.loc[frame["status"] == "downloaded"].copy()
    manual = frame.loc[frame["status"] == "manual"].copy()
    failed = frame.loc[frame["status"] == "failed"].copy()
    skipped = frame.loc[frame["status"] == "skipped"].copy()

    replacements = {
        "gadm": "Recommended replacement for legacy admin boundary layers: current GADM 4.1 country GeoPackages.",
        "osm": "Legacy placeholder only; not the active road-surface source in the current equatorial setup.",
        "road_surface": "Recommended paved/unpaved road-surface source: HeiGIT global road-surface dataset on HDX (Mapillary + OSM matching).",
        "chirps": "Recommended replacement for historical precipitation forcing: CHIRPS v3 precipitation rasters.",
        "era5": "Recommended replacement for older climate reanalysis inputs: ERA5-Land or ERA5 via the Copernicus CDS API.",
        "flood": "Recommended replacement for coarse flood proxies: JRC/Copernicus river flood hazard maps; GloFAS only as fallback.",
        "coastaldem": "Recommended coastal screening elevation source when access is granted: CoastalDEM.",
        "soilgrids": "Recommended replacement for coarse soil covariates: SoilGrids 250 m layers.",
        "ibtracs": "Recommended tropical cyclone track archive: NOAA IBTrACS v04r01.",
        "gem": "Recommended operational global seismic hazard source in this project: GEM open seismic hazard raster from Zenodo.",
        "liquefaction": "Recommended global liquefaction susceptibility source: Zhu model family raster from Zenodo.",
        "flopros": "FLOPROS is a protection-standard parameter dataset, not a raster hazard layer.",
    }

    lines = [
        "# Data Inventory",
        "",
        "This report is generated from `data/metadata/catalog.csv`.",
        "",
        "## Downloaded Successfully",
    ]

    if downloaded.empty:
        lines.append("- None yet.")
    else:
        for row in downloaded.itertuples(index=False):
            lines.append(
                f"- `{row.dataset_name}` -> `{row.local_path}` | raw resolution: {row.spatial_resolution_raw or 'n/a'}"
                f" | detected resolution: {row.pixel_size or 'n/a'} | CRS: {row.crs or 'n/a'}"
            )

    lines.extend(["", "## Manual Steps Required"])
    if manual.empty:
        lines.append("- None.")
    else:
        for row in manual.itertuples(index=False):
            lines.append(f"- `{row.dataset_name}` -> `{row.local_path}` | note: {row.notes or 'manual download required'}")

    lines.extend(["", "## Failed"])
    if failed.empty:
        lines.append("- None.")
    else:
        for row in failed.itertuples(index=False):
            lines.append(f"- `{row.dataset_name}` -> {row.notes or row.source_url or 'download failed'}")

    if not skipped.empty:
        lines.extend(["", "## Skipped"])
        for row in skipped.itertuples(index=False):
            lines.append(f"- `{row.dataset_name}` -> {row.notes or 'disabled in config'}")

    lines.extend(["", "## Recommended Replacements"])
    for dataset_name, note in replacements.items():
        lines.append(f"- `{dataset_name}`: {note}")

    return "\n".join(lines) + "\n"


def write_inventory_report(frame: pd.DataFrame, report_path: Path) -> None:
    """Write the markdown inventory report to disk."""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_inventory_report(frame), encoding="utf-8")
