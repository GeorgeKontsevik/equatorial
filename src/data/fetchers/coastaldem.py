"""Fetcher for CoastalDEM access placeholders and manual cataloging."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, manual_record, validate_download


COASTALDEM_PRODUCT_URL = "https://www.climatecentral.org/coastaldem-v2.1"
COASTALDEM_LICENSE_NOTE = "Climate Central CoastalDEM access and licence terms must be reviewed on the product page or request workflow."


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Catalog existing local CoastalDEM files or emit manual instructions."""

    raw_dir = context.raw_root / "coastaldem"
    existing_files = [path for path in raw_dir.rglob("*") if path.is_file()]

    records: list[CatalogRecord] = []
    for path in sorted(existing_files):
        ok, _ = validate_download(path)
        if not ok:
            continue
        records.append(
            downloaded_record(
                dataset_name="coastaldem",
                source_url=str(dataset_cfg.get("source_url", COASTALDEM_PRODUCT_URL)),
                local_path=path,
                context=context,
                license_or_access_note=str(dataset_cfg.get("license_or_access_note", COASTALDEM_LICENSE_NOTE)),
                spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "Near-global coastal DEM; see CoastalDEM version notes")),
                temporal_resolution="static elevation",
                bbox=dataset_cfg.get("bbox"),
                notes="Existing manually acquired CoastalDEM file was cataloged.",
            ),
        )

    if records:
        return records

    instructions = f"""# Manual Steps For CoastalDEM

Direct automated download is not implemented in this conservative first pass because CoastalDEM distribution is request-based and may involve additional approval or licensing steps.

Official product page:
- {COASTALDEM_PRODUCT_URL}

What to do:
1. Open the product page and use the request workflow if access is needed.
2. Download the approved files manually.
3. Place them under `data/raw/coastaldem/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` to catalog them.
"""
    return [
        manual_record(
            dataset_name="coastaldem",
            source_url=str(dataset_cfg.get("source_url", COASTALDEM_PRODUCT_URL)),
            context=context,
            instruction_text=instructions,
            license_or_access_note=str(dataset_cfg.get("license_or_access_note", COASTALDEM_LICENSE_NOTE)),
            spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "See CoastalDEM version documentation")),
            temporal_resolution="static elevation",
            bbox=dataset_cfg.get("bbox"),
            notes="CoastalDEM requires manual acquisition in this first-pass pipeline.",
        ),
    ]
