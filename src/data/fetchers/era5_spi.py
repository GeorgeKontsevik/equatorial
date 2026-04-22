"""Fetcher for global ERA5 SPI GeoTIFFs published by Drought.gov / NOAA NIDIS."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


ERA5_SPI_HTTP_ROOT = "https://storage.googleapis.com/noaa-nidis-drought-gov-data/current-conditions/tile/v1"
ERA5_SPI_SOURCE_URL = "https://www.drought.gov/data-maps-tools/era5-drought-indices"
ERA5_SPI_LICENSE_NOTE = (
    "ERA5 SPI GeoTIFFs are distributed via Drought.gov / NOAA NIDIS current-conditions storage. "
    "Review Drought.gov, Climate Engine, and ECMWF/C3S citation guidance before redistribution."
)
ERA5_SPI_SUPPORTED_TIMESCALES = [1, 2, 3, 6, 9, 12]


def _normalise_timescales(values) -> list[int]:
    if not values:
        return list(ERA5_SPI_SUPPORTED_TIMESCALES)
    return [int(value) for value in values]


def _timescale_suffix(months: int) -> str:
    return f"{months}mo"


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download current global ERA5 SPI GeoTIFFs for configured monthly timescales."""

    timescales = _normalise_timescales(dataset_cfg.get("timescales_months"))
    unsupported = [value for value in timescales if value not in ERA5_SPI_SUPPORTED_TIMESCALES]
    bbox = dataset_cfg.get("bbox")

    if unsupported:
        instructions = f"""# Manual Steps For ERA5 SPI

Only these timescales are automated in this first-pass fetcher:
- {', '.join(str(value) for value in ERA5_SPI_SUPPORTED_TIMESCALES)} months

Requested unsupported timescales:
- {', '.join(str(value) for value in unsupported)}

Dataset page:
- {ERA5_SPI_SOURCE_URL}
"""
        return [
            manual_record(
                dataset_name="era5_spi",
                source_url=ERA5_SPI_SOURCE_URL,
                context=context,
                instruction_text=instructions,
                license_or_access_note=ERA5_SPI_LICENSE_NOTE,
                spatial_resolution_raw="Global ~30 km SPI GeoTIFFs derived from ERA5 / Climate Engine",
                temporal_resolution="monthly",
                bbox=bbox,
                notes="Unsupported ERA5 SPI timescale requested.",
            ),
        ]

    target_dir = ensure_directory(context.raw_root / "era5_spi" / "global" / "monthly")
    records: list[CatalogRecord] = []

    for months in timescales:
        suffix = _timescale_suffix(months)
        dirname = f"ce-GLOBAL-ERA5_LAND_DAILY-spi-{suffix}"
        filename = f"GLOBAL-ERA5_LAND_DAILY-spi-{suffix}.tif"
        source_url = f"{ERA5_SPI_HTTP_ROOT}/{dirname}/{filename}"
        local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
        records.append(
            downloaded_record(
                dataset_name="era5_spi",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=ERA5_SPI_LICENSE_NOTE,
                spatial_resolution_raw="Global ~30 km SPI GeoTIFF derived from ERA5 / Climate Engine",
                temporal_resolution=f"{months}-month SPI (monthly product)",
                bbox=bbox,
                notes=join_notes(
                    f"Drought.gov / NOAA NIDIS global ERA5 SPI GeoTIFF for the {months}-month timescale.",
                    "This is a ready-made SPI layer, not raw precipitation.",
                    "Global raster; spatial subsetting is expected downstream if needed.",
                    "Reused an existing local copy." if reused else "Downloaded from the Drought.gov public storage bucket.",
                ),
            ),
        )

    return records
