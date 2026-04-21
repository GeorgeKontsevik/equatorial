"""Fetcher for GADM administrative boundaries."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


GADM_GPKG_ROOT = "https://geodata.ucdavis.edu/gadm/gadm4.1/gpkg"
GADM_LICENSE_NOTE = (
    "Review current GADM terms of use before redistribution; the files are widely used for research but are not "
    "an unrestricted public-domain product."
)


def _normalize_country_codes(country_codes: list[str] | tuple[str, ...] | None) -> list[str]:
    if not country_codes:
        return []
    return sorted({str(code).strip().upper() for code in country_codes if str(code).strip()})


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download current country-level GADM 4.1 GeoPackages."""

    country_codes = _normalize_country_codes(dataset_cfg.get("country_codes"))
    source_root = str(dataset_cfg.get("source_url", GADM_GPKG_ROOT)).rstrip("/")
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "Vector administrative boundaries"))
    license_note = str(dataset_cfg.get("license_or_access_note", GADM_LICENSE_NOTE))

    if not country_codes:
        instructions = f"""# Manual Steps For GADM

No `country_codes` were configured for the GADM fetcher.

Recommended automated mode:
1. Edit `config/datasets.yaml`.
2. Set `datasets.gadm.country_codes` to one or more ISO3 codes, for example `["BOL", "PER"]`.
3. Re-run `python -m src.data.fetch --config config/datasets.yaml`.

Official country GeoPackage directory:
- {source_root}/

If you prefer manual download:
1. Download one or more files named `gadm41_<ISO3>.gpkg`.
2. Place them under `data/raw/gadm/<ISO3>/`.
3. Re-run the fetch and inspect commands.
"""
        return [
            manual_record(
                dataset_name="gadm",
                source_url=source_root,
                context=context,
                instruction_text=instructions,
                license_or_access_note=license_note,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static",
                notes="No GADM country_codes were configured.",
            ),
        ]

    records: list[CatalogRecord] = []
    for iso3 in country_codes:
        target_dir = ensure_directory(context.raw_root / "gadm" / iso3)
        filename = f"gadm41_{iso3}.gpkg"
        source_url = f"{source_root}/{filename}"
        local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
        records.append(
            downloaded_record(
                dataset_name="gadm",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=license_note,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static",
                bbox=dataset_cfg.get("bbox"),
                notes=join_notes(
                    f"GADM 4.1 GeoPackage for {iso3}.",
                    "Reused an existing local copy." if reused else "Downloaded from the official GADM country GeoPackage directory.",
                ),
            ),
        )

    return records
