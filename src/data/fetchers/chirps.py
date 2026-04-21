"""Fetcher for CHIRPS precipitation products."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


CHIRPS_HTTP_ROOT = "https://data.chc.ucsb.edu/products/CHIRPS"
CHIRPS_LICENSE_NOTE = (
    "CHIRPS is distributed by the Climate Hazards Center. Review the product documentation and citation guidance "
    "for reuse requirements."
)


def _normalise_years(years) -> list[int]:
    return [int(value) for value in years or []]


def _normalise_months(months) -> list[int]:
    return [int(value) for value in months or []]


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download CHIRPS monthly global GeoTIFFs."""

    version = str(dataset_cfg.get("version", "v3.0"))
    frequency = str(dataset_cfg.get("frequency", dataset_cfg.get("temporal_resolution", "monthly"))).lower()
    region = str(dataset_cfg.get("region", "global")).lower()
    years = _normalise_years(dataset_cfg.get("years"))
    months = _normalise_months(dataset_cfg.get("months"))
    bbox = dataset_cfg.get("bbox")

    if frequency != "monthly" or region != "global":
        instructions = f"""# Manual Steps For CHIRPS

This first-pass implementation only automates CHIRPS monthly global GeoTIFF downloads.

Official CHC repository:
- https://data.chc.ucsb.edu/
- https://data.chc.ucsb.edu/products/CHIRPS/{version}/

Requested configuration:
- frequency: {frequency}
- region: {region}

What to do:
1. Download the required files manually from the official CHC repository.
2. Place them under `data/raw/chirps/{region}/{frequency}/`.
3. Re-run `python -m src.data.inspect --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="chirps",
                source_url=f"{CHIRPS_HTTP_ROOT}/{version}/",
                context=context,
                instruction_text=instructions,
                license_or_access_note=CHIRPS_LICENSE_NOTE,
                spatial_resolution_raw="~0.05 degree",
                temporal_resolution=frequency,
                bbox=bbox,
                notes="Only monthly global CHIRPS downloads are automated in this first pass.",
            ),
        ]

    if not years or not months:
        instructions = f"""# Manual Steps For CHIRPS

The CHIRPS fetcher requires explicit `years` and `months` lists in the config.

Example:
```yaml
datasets:
  chirps:
    years: [2024]
    months: [1, 2, 3]
```

Official monthly directory:
- https://data.chc.ucsb.edu/products/CHIRPS/{version}/monthly/global/tifs/
"""
        return [
            manual_record(
                dataset_name="chirps",
                source_url=f"{CHIRPS_HTTP_ROOT}/{version}/monthly/global/tifs/",
                context=context,
                instruction_text=instructions,
                license_or_access_note=CHIRPS_LICENSE_NOTE,
                spatial_resolution_raw="~0.05 degree",
                temporal_resolution="monthly",
                bbox=bbox,
                notes="CHIRPS years/months were not configured.",
            ),
        ]

    records: list[CatalogRecord] = []
    target_dir = ensure_directory(context.raw_root / "chirps" / "global" / "monthly")
    for year in years:
        for month in months:
            filename = f"chirps-{version}.{year}.{month:02d}.tif"
            source_url = f"{CHIRPS_HTTP_ROOT}/{version}/monthly/global/tifs/{filename}"
            local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
            records.append(
                downloaded_record(
                    dataset_name="chirps",
                    source_url=source_url,
                    local_path=local_path,
                    context=context,
                    license_or_access_note=CHIRPS_LICENSE_NOTE,
                    spatial_resolution_raw="~0.05 degree global precipitation grid",
                    temporal_resolution="monthly",
                    bbox=bbox,
                    notes=join_notes(
                        f"CHIRPS {version} monthly global precipitation for {year}-{month:02d}.",
                        "Global raster; spatial subsetting is expected downstream if needed.",
                        "Reused an existing local copy." if reused else "Downloaded from the official CHC monthly GeoTIFF directory.",
                    ),
                ),
            )

    return records
