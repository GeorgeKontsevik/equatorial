"""Fetcher for ESA WorldCover tiles intersecting the configured bbox."""

from __future__ import annotations

import math

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


WORLDCOVER_HTTP_ROOT = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
WORLDCOVER_LICENSE_NOTE = "ESA WorldCover is distributed under CC-BY 4.0. Follow ESA WorldCover citation guidance."


def _tile_code(lat: int, lon: int) -> str:
    lat_prefix = "N" if lat >= 0 else "S"
    lon_prefix = "E" if lon >= 0 else "W"
    return f"{lat_prefix}{abs(lat):02d}{lon_prefix}{abs(lon):03d}"


def _tile_starts(min_value: float, max_value: float) -> list[int]:
    start = int(math.floor(min_value / 3.0) * 3)
    end = int(math.floor((max_value - 1e-9) / 3.0) * 3)
    return list(range(start, end + 1, 3))


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download ESA WorldCover 3x3 degree tiles intersecting the configured bbox."""

    bbox = dataset_cfg.get("bbox")
    if not bbox:
        instructions = """# Manual Steps For ESA WorldCover

No `bbox` was configured for the WorldCover fetcher.

What to do:
1. Set `study_area.bbox` or `datasets.worldcover.bbox` in `config/datasets.yaml`.
2. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="worldcover",
                source_url="https://esa-worldcover.org/en/data-access",
                context=context,
                instruction_text=instructions,
                license_or_access_note=WORLDCOVER_LICENSE_NOTE,
                spatial_resolution_raw="10 m global land cover map",
                temporal_resolution="annual snapshot",
                bbox=bbox,
                notes="No bbox was configured for WorldCover tile selection.",
            ),
        ]

    year = int(dataset_cfg.get("year", 2021))
    version = str(dataset_cfg.get("version", "v200"))
    layer = str(dataset_cfg.get("layer", "Map"))
    if layer not in {"Map", "InputQuality"}:
        raise ValueError(f"Unsupported WorldCover layer: {layer}")

    minx, miny, maxx, maxy = [float(value) for value in bbox]
    lon_starts = _tile_starts(minx, maxx)
    lat_starts = _tile_starts(miny, maxy)
    if not lon_starts or not lat_starts:
        raise ValueError(f"Invalid bbox for WorldCover tile selection: {bbox}")

    target_dir = ensure_directory(context.raw_root / "worldcover" / str(year) / version / layer.lower())
    records: list[CatalogRecord] = []
    missing_tiles: list[str] = []
    failed_tiles: list[tuple[str, str]] = []

    for lat in lat_starts:
        for lon in lon_starts:
            tile = _tile_code(lat, lon)
            filename = f"ESA_WorldCover_10m_{year}_{version}_{tile}_{layer}.tif"
            source_url = f"{WORLDCOVER_HTTP_ROOT}/{version}/{year}/map/{filename}"
            try:
                local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
            except Exception as exc:  # pragma: no cover - runtime/provider dependent
                if "404" in str(exc):
                    missing_tiles.append(tile)
                    continue
                failed_tiles.append((tile, str(exc)))
                continue
            records.append(
                downloaded_record(
                    dataset_name="worldcover",
                    source_url=source_url,
                    local_path=local_path,
                    context=context,
                    license_or_access_note=WORLDCOVER_LICENSE_NOTE,
                    spatial_resolution_raw="10 m global land cover map",
                    temporal_resolution="annual snapshot",
                    bbox=bbox,
                    notes=join_notes(
                        f"ESA WorldCover {year} {version} tile `{tile}` layer `{layer}`.",
                        "Reused an existing local copy." if reused else "Downloaded from the public ESA WorldCover S3 bucket.",
                    ),
                ),
            )

    if records:
        if missing_tiles and context.logger:
            context.logger.warning("WorldCover tiles missing from public bucket and skipped: %s", ", ".join(sorted(missing_tiles)))
        if failed_tiles and context.logger:
            context.logger.warning(
                "WorldCover tiles failed but other tiles were downloaded: %s",
                "; ".join(f"{tile}: {message}" for tile, message in failed_tiles),
            )
        return records

    if missing_tiles or failed_tiles:
        details = []
        if missing_tiles:
            details.append(f"Missing tiles in public bucket: {', '.join(sorted(missing_tiles))}")
        if failed_tiles:
            details.append("Failed tile downloads: " + "; ".join(f"{tile}: {message}" for tile, message in failed_tiles))
        instructions = f"""# Manual Steps For ESA WorldCover

Automatic download of the required WorldCover tiles did not complete successfully.

Dataset page:
- https://esa-worldcover.org/en/data-access

Requested configuration:
- year: {year}
- version: {version}
- layer: {layer}
- bbox: {bbox}

What to do:
1. Check the expected 3x3 degree tiles in the ESA WorldCover public bucket or download portal.
2. Download the required tiles manually.
3. Place them under `data/raw/worldcover/{year}/{version}/{layer.lower()}/`.
4. Re-run the fetch and inspect commands.
"""
        return [
            manual_record(
                dataset_name="worldcover",
                source_url="https://esa-worldcover.org/en/data-access",
                context=context,
                instruction_text=instructions,
                license_or_access_note=WORLDCOVER_LICENSE_NOTE,
                spatial_resolution_raw="10 m global land cover map",
                temporal_resolution="annual snapshot",
                bbox=bbox,
                notes=" ".join(details),
            ),
        ]

    return records
