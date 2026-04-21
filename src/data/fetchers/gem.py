"""Fetcher for the GEM global seismic hazard map."""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record, validate_download


GEM_PRODUCT_URL = "https://www.globalquakemodel.org/product/global-seismic-hazard-map/"
GEM_OPEN_ZIP_URL = "https://zenodo.org/records/8409647/files/GEM-GSHM_PGA-475y-rock_v2023.zip?download=1"
GEM_LICENSE_NOTE = (
    "The GEM open version is distributed via Zenodo; review the accompanying licence and README before redistribution or commercial use."
)


def _migrate_legacy_files(raw_dir: Path) -> None:
    legacy_dir = raw_dir.parent
    if not legacy_dir.exists() or legacy_dir == raw_dir:
        return
    raw_dir.mkdir(parents=True, exist_ok=True)
    for legacy_path in legacy_dir.iterdir():
        if legacy_path.name == "global":
            continue
        target_path = raw_dir / legacy_path.name
        if legacy_path.is_file() and not target_path.exists():
            legacy_path.replace(target_path)


def _extract_zip_members(zip_path: Path, target_dir: Path) -> list[Path]:
    extracted_paths: list[Path] = []
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            target_path = target_dir / member.filename
            if not target_path.exists():
                archive.extract(member, path=target_dir)
            extracted_paths.append(target_path)
    return extracted_paths


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download or reuse the GEM global seismic hazard map open version."""

    raw_dir = ensure_directory(context.raw_root / "gem" / "global")
    _migrate_legacy_files(raw_dir)

    source_url = str(dataset_cfg.get("source_url", GEM_OPEN_ZIP_URL))
    license_note = str(dataset_cfg.get("license_or_access_note", GEM_LICENSE_NOTE))
    spatial_resolution_raw = str(
        dataset_cfg.get(
            "spatial_resolution_raw",
            "Global seismic hazard raster interpolated from hazard values calculated at about ~6 km point spacing",
        )
    )

    zip_path = raw_dir / "GEM-GSHM_PGA-475y-rock_v2023.zip"
    tif_name = "v2023_1_pga_475_rock_3min.tif"
    tif_path = raw_dir / tif_name

    records: list[CatalogRecord] = []

    if tif_path.exists():
        ok, _ = validate_download(tif_path)
        if ok:
            records.append(
                downloaded_record(
                    dataset_name="gem",
                    source_url=source_url,
                    local_path=tif_path,
                    context=context,
                    license_or_access_note=license_note,
                    spatial_resolution_raw=spatial_resolution_raw,
                    temporal_resolution="static hazard map",
                    bbox=dataset_cfg.get("bbox"),
                    notes="Reused the existing local GEM seismic hazard raster.",
                ),
            )
            return records

    try:
        local_zip, reused = ensure_local_copy(source_url, zip_path, context)
        extracted_paths = _extract_zip_members(local_zip, raw_dir)
    except Exception:
        instructions = f"""# Manual Steps For GEM Global Seismic Hazard

Automatic download of the GEM open version did not succeed in the current environment.

Official product page:
- {GEM_PRODUCT_URL}

Open dataset reference:
- {GEM_OPEN_ZIP_URL}

What to do:
1. Download the GEM open-version ZIP archive.
2. Place the ZIP under `data/raw/gem/global/`.
3. Extract it so that `v2023_1_pga_475_rock_3min.tif` is present under `data/raw/gem/global/`.
4. Re-run the fetch and inspect commands.
"""
        return [
            manual_record(
                dataset_name="gem",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=license_note,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static hazard map",
                bbox=dataset_cfg.get("bbox"),
                notes="GEM auto-download fell back to manual instructions.",
            ),
        ]

    extracted_tif = tif_path if tif_path.exists() else None
    if extracted_tif is None:
        for path in extracted_paths:
            if path.name == tif_name:
                extracted_tif = path
                break

    if extracted_tif is None or not extracted_tif.exists():
        return [
            manual_record(
                dataset_name="gem",
                source_url=source_url,
                context=context,
                instruction_text=(
                    "# Manual Steps For GEM Global Seismic Hazard\n\n"
                    "The ZIP archive was downloaded, but the expected TIFF was not found after extraction.\n"
                    f"Expected file: `{tif_name}`\n"
                    "Inspect the archive contents under `data/raw/gem/global/` and re-run the pipeline.\n"
                ),
                license_or_access_note=license_note,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static hazard map",
                bbox=dataset_cfg.get("bbox"),
                notes="Downloaded GEM archive but did not find the expected hazard TIFF after extraction.",
            ),
        ]

    records.append(
        downloaded_record(
            dataset_name="gem",
            source_url=source_url,
            local_path=extracted_tif,
            context=context,
            license_or_access_note=license_note,
            spatial_resolution_raw=spatial_resolution_raw,
            temporal_resolution="static hazard map",
            bbox=dataset_cfg.get("bbox"),
            notes=join_notes(
                "GEM global seismic hazard raster (open version).",
                "Reused an existing local ZIP archive." if reused else "Downloaded the ZIP archive from the GEM Zenodo open-version record.",
            ),
        )
    )
    return records
