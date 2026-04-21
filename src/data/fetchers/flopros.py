"""Fetcher for FLOPROS protection-standard data."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record, relative_to_project


FLOPROS_ARTICLE_URL = "https://nhess.copernicus.org/articles/16/1049/2016/nhess-16-1049-2016.html"
FLOPROS_SUPPLEMENT_URL = "https://nhess.copernicus.org/articles/16/1049/2016/nhess-16-1049-2016-supplement.zip"
FLOPROS_LICENSE_NOTE = (
    "FLOPROS is a protection-standard database described in the NHESS publication and supplement. It should not be "
    "treated as a raster hazard layer."
)


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
    """Download and catalog the official FLOPROS supplement archive."""

    global_dir = ensure_directory(context.raw_root / "flopros" / "global")
    original_dir = ensure_directory(global_dir / "original")
    legacy_note_path = context.raw_root / "flopros" / "flopros_parameter_scale_note.csv"
    if legacy_note_path.exists():
        legacy_note_path.unlink(missing_ok=True)

    source_url = str(dataset_cfg.get("source_url", FLOPROS_SUPPLEMENT_URL))
    article_url = str(dataset_cfg.get("article_url", FLOPROS_ARTICLE_URL))
    license_note = str(dataset_cfg.get("license_or_access_note", FLOPROS_LICENSE_NOTE))

    note_path = global_dir / "flopros_parameter_scale_note.csv"
    with note_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["dataset_name", "dataset_type", "is_raster_hazard_layer", "message", "source_url", "article_url"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "dataset_name": "FLOPROS",
                "dataset_type": "flood protection standards / parameters",
                "is_raster_hazard_layer": "no",
                "message": "Use FLOPROS as protection-standard metadata or model parameter inputs, not as a raster hazard surface.",
                "source_url": source_url,
                "article_url": article_url,
            },
        )

    zip_path = global_dir / "nhess-16-1049-2016-supplement.zip"

    try:
        local_zip, reused = ensure_local_copy(source_url, zip_path, context)
        extracted_paths = _extract_zip_members(local_zip, original_dir)
    except Exception:
        instructions = f"""# Manual Steps For FLOPROS

Automatic download of the official FLOPROS supplement archive did not succeed in the current environment.

Primary reference:
- {article_url}

Supplement archive:
- {source_url}

What to do:
1. Download the official supplement ZIP archive.
2. Place it under `data/raw/flopros/global/`.
3. Extract it under `data/raw/flopros/global/original/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="flopros",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=license_note,
                spatial_resolution_raw="Protection-standard metadata; shapefile and spreadsheet in official supplement",
                temporal_resolution="parameter / metadata scale",
                bbox=dataset_cfg.get("bbox"),
                notes="FLOPROS auto-download fell back to manual instructions.",
            ),
        ]

    records: list[CatalogRecord] = []
    shapefile_path = next((path for path in extracted_paths if path.name == "FLOPROS_shp_V1.shp"), None)
    workbook_path = next((path for path in extracted_paths if path.name == "FLOPROS_Database_Design_&_Policy_layers_V1.xlsx"), None)

    records.append(
        downloaded_record(
            dataset_name="flopros",
            source_url=source_url,
            local_path=local_zip,
            context=context,
            license_or_access_note=license_note,
            spatial_resolution_raw="Official FLOPROS supplement archive",
            temporal_resolution="snapshot",
            bbox=dataset_cfg.get("bbox"),
            notes="Reused the existing FLOPROS supplement ZIP archive." if reused else "Downloaded the official FLOPROS supplement ZIP archive.",
        )
    )

    if shapefile_path is not None and shapefile_path.exists():
        records.append(
            downloaded_record(
                dataset_name="flopros",
                source_url=source_url,
                local_path=shapefile_path,
                context=context,
                license_or_access_note=license_note,
                spatial_resolution_raw="Protection-standard polygons and attributes from official FLOPROS supplement shapefile",
                temporal_resolution="parameter / metadata scale",
                bbox=dataset_cfg.get("bbox"),
                notes=join_notes(
                    "Official FLOPROS shapefile extracted from supplement archive.",
                    "This is an adjustment/protection input, not a hazard raster.",
                ),
            )
        )

    if workbook_path is not None and workbook_path.exists():
        records.append(
            downloaded_record(
                dataset_name="flopros",
                source_url=source_url,
                local_path=workbook_path,
                context=context,
                license_or_access_note=license_note,
                spatial_resolution_raw="Design and policy layers workbook from official FLOPROS supplement",
                temporal_resolution="parameter / metadata scale",
                bbox=dataset_cfg.get("bbox"),
                notes="Official FLOPROS design/policy spreadsheet extracted from supplement archive.",
            )
        )

    records.append(
        downloaded_record(
            dataset_name="flopros",
            source_url=source_url,
            local_path=note_path,
            context=context,
            license_or_access_note=license_note,
            spatial_resolution_raw="Parameter note file",
            temporal_resolution="parameter / metadata scale",
            bbox=dataset_cfg.get("bbox"),
            notes=f"Parameter note file written to {relative_to_project(note_path, context.project_root)}.",
        )
    )

    return records
