"""Fetcher for JRC / Copernicus global river flood hazard tiles."""

from __future__ import annotations

import re
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


FLOOD_ROOT = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-GLOFAS/flood_hazard"
FLOOD_LICENSE_NOTE = "Open Copernicus Emergency Management Service / JRC product. Review the accompanying README and copyright notice."


def _select_tile_column(frame: gpd.GeoDataFrame) -> str | None:
    preferred = ["tile_name", "tile", "tile_id", "name", "id", "filename", "basename"]
    for column in preferred:
        if column in frame.columns:
            return column

    pattern = re.compile(r"^ID\d+_[NS]\d+_[EW]\d+$")
    for column in frame.columns:
        values = frame[column].dropna().astype(str)
        if not values.empty and values.str.match(pattern).all():
            return column
    return None


def _build_tile_stem(row, tile_column: str) -> str:
    tile_name = str(row[tile_column]).strip()
    if "id" in row and str(row["id"]).strip():
        try:
            tile_id = int(float(row["id"]))
            return f"ID{tile_id}_{tile_name}"
        except ValueError:
            pass
    return tile_name


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download JRC/Copernicus global flood hazard tiles that intersect a bbox."""

    bbox = dataset_cfg.get("bbox")
    return_periods = [int(value) for value in dataset_cfg.get("return_periods", [100])]
    download_reclass = bool(dataset_cfg.get("download_reclass", False))

    dataset_dir = ensure_directory(context.raw_root / "flood" / "jrc_glofas")
    records: list[CatalogRecord] = []

    auxiliary_files = [
        ("README.txt", f"{FLOOD_ROOT}/README.txt"),
        ("tile_extents.geojson", f"{FLOOD_ROOT}/tile_extents.geojson"),
        ("copyright.txt", f"{FLOOD_ROOT}/copyright.txt"),
    ]
    local_aux_paths: dict[str, Path] = {}
    for filename, source_url in auxiliary_files:
        local_path, reused = ensure_local_copy(source_url, dataset_dir / filename, context)
        local_aux_paths[filename] = local_path
        records.append(
            downloaded_record(
                dataset_name="flood",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=FLOOD_LICENSE_NOTE,
                spatial_resolution_raw="3 arc-seconds (~90 m)",
                temporal_resolution="static return-period scenario metadata",
                bbox=bbox,
                notes=join_notes(
                    f"Auxiliary JRC flood hazard file: {filename}.",
                    "Reused an existing local copy." if reused else "Downloaded from the official JRC flood hazard directory.",
                ),
            ),
        )

    if not bbox or len(bbox) != 4:
        instructions = f"""# Manual Steps For JRC / Copernicus Flood Hazard

The fetcher downloaded the global tile index and README, but no bounding box was configured for tile selection.

Official root:
- {FLOOD_ROOT}/

What to do:
1. Set `datasets.flood.bbox` to `[minx, miny, maxx, maxy]` in WGS84.
2. Optionally adjust `return_periods`.
3. Re-run `python -m src.data.fetch --config config/datasets.yaml`.

The downloaded `tile_extents.geojson` can be used to preview which tiles overlap your study area.
"""
        records.append(
            manual_record(
                dataset_name="flood",
                source_url=FLOOD_ROOT,
                context=context,
                instruction_text=instructions,
                license_or_access_note=FLOOD_LICENSE_NOTE,
                spatial_resolution_raw="3 arc-seconds (~90 m)",
                temporal_resolution="static return periods",
                bbox=bbox,
                notes="No flood bbox was configured, so hazard tiles were not selected automatically.",
                instruction_name="flood_bbox_required",
            ),
        )
        return records

    tile_index = gpd.read_file(local_aux_paths["tile_extents.geojson"])
    tile_column = _select_tile_column(tile_index)
    if tile_column is None:
        instructions = f"""# Manual Steps For JRC / Copernicus Flood Hazard

The tile index schema was not recognized automatically, so tile names could not be derived safely.

Inspect this file:
- `data/raw/flood/jrc_glofas/tile_extents.geojson`

Official root:
- {FLOOD_ROOT}/

Recommended next step:
1. Inspect the tile index attributes.
2. Determine the tile name field.
3. Update the fetcher or download the needed tiles manually.
"""
        records.append(
            manual_record(
                dataset_name="flood",
                source_url=FLOOD_ROOT,
                context=context,
                instruction_text=instructions,
                license_or_access_note=FLOOD_LICENSE_NOTE,
                spatial_resolution_raw="3 arc-seconds (~90 m)",
                temporal_resolution="static return periods",
                bbox=bbox,
                notes="Flood tile index schema was not recognized safely.",
                instruction_name="flood_tile_schema",
            ),
        )
        return records

    subset = tile_index.loc[tile_index.intersects(box(*bbox))].copy()
    if subset.empty:
        instructions = f"""# Manual Steps For JRC / Copernicus Flood Hazard

No flood tiles intersected the configured bbox:
- {bbox}

What to do:
1. Confirm the bbox is in WGS84 and correctly ordered as `[minx, miny, maxx, maxy]`.
2. Inspect `data/raw/flood/jrc_glofas/tile_extents.geojson`.
3. Re-run after adjusting the bbox if needed.
"""
        records.append(
            manual_record(
                dataset_name="flood",
                source_url=FLOOD_ROOT,
                context=context,
                instruction_text=instructions,
                license_or_access_note=FLOOD_LICENSE_NOTE,
                spatial_resolution_raw="3 arc-seconds (~90 m)",
                temporal_resolution="static return periods",
                bbox=bbox,
                notes="No JRC flood hazard tiles intersected the configured bbox.",
                instruction_name="flood_no_tiles",
            ),
        )
        return records

    suffix = "_depth_reclass.tif" if download_reclass else "_depth.tif"
    for return_period in return_periods:
        rp_dir = ensure_directory(dataset_dir / f"RP{return_period}")
        for row in subset.itertuples(index=False):
            tile_stem = _build_tile_stem(row._asdict(), tile_column)
            filename = f"{tile_stem}_RP{return_period}{suffix}"
            source_url = f"{FLOOD_ROOT}/RP{return_period}/{filename}"
            local_path, reused = ensure_local_copy(source_url, rp_dir / filename, context)
            records.append(
                downloaded_record(
                    dataset_name="flood",
                    source_url=source_url,
                    local_path=local_path,
                    context=context,
                    license_or_access_note=FLOOD_LICENSE_NOTE,
                    spatial_resolution_raw="3 arc-seconds (~90 m) flood depth map",
                    temporal_resolution=f"static RP{return_period} scenario",
                    bbox=bbox,
                    notes=join_notes(
                        f"Selected from tile index by bbox intersection using tile `{tile_stem}`.",
                        "Reclassified depth product." if download_reclass else "Raw depth product.",
                        "Reused an existing local copy." if reused else "Downloaded from the official JRC flood hazard tile directory.",
                    ),
                ),
            )

    return records
