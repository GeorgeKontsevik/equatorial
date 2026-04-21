"""Fetcher for global liquefaction susceptibility data."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record, validate_download


LIQUEFACTION_PRODUCT_URL = "https://zenodo.org/records/2583746"
LIQUEFACTION_DIRECT_URL = "https://zenodo.org/records/2583746/files/liquefaction_v1_deg.tif?download=1"
LIQUEFACTION_LICENSE_NOTE = "Zenodo dataset under ODC Open Database License v1.0; review citation and licence requirements."


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download or catalog the global liquefaction susceptibility raster."""

    raw_dir = context.raw_root / "liquefaction" / "global"
    target_path = ensure_directory(raw_dir) / "liquefaction_v1_deg.tif"
    legacy_path = context.raw_root / "liquefaction" / "liquefaction_v1_deg.tif"
    source_url = str(dataset_cfg.get("source_url", LIQUEFACTION_DIRECT_URL))
    existing_files = [path for path in raw_dir.rglob("*") if path.is_file()]
    records: list[CatalogRecord] = []

    # Migrate the previously downloaded flat-path file into the global/ layout.
    if legacy_path.exists() and not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.replace(target_path)

    for path in sorted(existing_files):
        ok, _ = validate_download(path)
        if not ok:
            continue
        records.append(
            downloaded_record(
                dataset_name="liquefaction",
                source_url=source_url,
                local_path=path,
                context=context,
                license_or_access_note=str(dataset_cfg.get("license_or_access_note", LIQUEFACTION_LICENSE_NOTE)),
                spatial_resolution_raw=str(
                    dataset_cfg.get(
                        "spatial_resolution_raw",
                        "Global raster in EPSG:4326; susceptibility classes 0-5 from the Zhu model family",
                    ),
                ),
                temporal_resolution="static susceptibility map",
                bbox=dataset_cfg.get("bbox"),
                notes="Existing liquefaction susceptibility raster was cataloged.",
            ),
        )

    if records:
        return records

    try:
        local_path, reused = ensure_local_copy(source_url, target_path, context)
        return [
            downloaded_record(
                dataset_name="liquefaction",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=str(dataset_cfg.get("license_or_access_note", LIQUEFACTION_LICENSE_NOTE)),
                spatial_resolution_raw=str(
                    dataset_cfg.get(
                        "spatial_resolution_raw",
                        "Global raster in EPSG:4326; susceptibility classes 0-5 from the Zhu model family",
                    ),
                ),
                temporal_resolution="static susceptibility map",
                bbox=dataset_cfg.get("bbox"),
                notes=join_notes(
                    "Global liquefaction susceptibility raster.",
                    "Reused an existing local copy." if reused else "Downloaded from the Zenodo direct file URL.",
                ),
            ),
        ]
    except Exception as exc:  # pragma: no cover - runtime/provider dependent
        instructions = f"""# Manual Steps For Liquefaction

Automatic download from the Zenodo direct file URL did not complete successfully.

Dataset page:
- {LIQUEFACTION_PRODUCT_URL}

Direct file URL:
- {source_url}

What to do:
1. Download `liquefaction_v1_deg.tif`.
2. Place it under `data/raw/liquefaction/global/`.
3. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="liquefaction",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=str(dataset_cfg.get("license_or_access_note", LIQUEFACTION_LICENSE_NOTE)),
                spatial_resolution_raw=str(
                    dataset_cfg.get(
                        "spatial_resolution_raw",
                        "Global raster in EPSG:4326; susceptibility classes 0-5 from the Zhu model family",
                    ),
                ),
                temporal_resolution="static susceptibility map",
                bbox=dataset_cfg.get("bbox"),
                notes=f"Automatic liquefaction download did not succeed: {exc}",
            ),
        ]
