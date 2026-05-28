"""Fetcher for NASA global landslide susceptibility map clips."""

from __future__ import annotations

import math
from urllib.parse import urlencode

import rasterio

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, join_notes, manual_record, validate_download


LANDSLIDE_IMAGE_SERVICE_URL = (
    "https://gis.earthdata.nasa.gov/gis05/rest/services/Landslides/Global_Landslide_Susceptibility/ImageServer"
)
LANDSLIDE_LEGACY_SERVICE_URL = "https://maps.nccs.nasa.gov/server/rest/services/global_landslide_catalog/landslide_susceptibility/MapServer"
LANDSLIDE_FALLBACK_SERVICE_URL = (
    "https://maps.nccs.nasa.gov/mapping/rest/services/landslide_viewer/Landslide_Susceptibility_Update_2023/MapServer"
)
LANDSLIDE_LICENSE_NOTE = (
    "NASA global landslide susceptibility service based on Stanley and Kirschbaum (2017). Review the cited methodology and service terms before redistribution."
)


def _bbox_to_size(bbox: list[float], pixel_deg: float) -> tuple[int, int]:
    minx, miny, maxx, maxy = [float(value) for value in bbox]
    width = max(1, int(math.ceil((maxx - minx) / pixel_deg)))
    height = max(1, int(math.ceil((maxy - miny) / pixel_deg)))
    return width, height


def _validate_raster(path) -> tuple[bool, str]:
    ok, reason = validate_download(path)
    if not ok:
        return False, reason
    try:
        with rasterio.open(path) as dataset:
            if dataset.width <= 0 or dataset.height <= 0:
                return False, "raster has invalid dimensions"
    except Exception as exc:
        return False, f"raster validation failed: {exc}"
    return True, ""


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Export a bbox-clipped TIFF from the NASA landslide susceptibility map service."""

    bbox = dataset_cfg.get("bbox")
    if not bbox:
        instructions = f"""# Manual Steps For NASA Global Landslide Susceptibility

No `bbox` was configured for the landslide susceptibility fetcher.

Service root:
- {LANDSLIDE_IMAGE_SERVICE_URL}

What to do:
1. Set `study_area.bbox` or `datasets.landslide_susceptibility.bbox` in `config/datasets.yaml`.
2. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="landslide_susceptibility",
                source_url=LANDSLIDE_IMAGE_SERVICE_URL,
                context=context,
                instruction_text=instructions,
                license_or_access_note=LANDSLIDE_LICENSE_NOTE,
                spatial_resolution_raw="Global susceptibility map at approximately 30 arc-seconds",
                temporal_resolution="static susceptibility ranking",
                bbox=bbox,
                notes="No bbox was configured for landslide susceptibility export.",
            ),
        ]

    pixel_deg = float(dataset_cfg.get("export_resolution_deg", 1.0 / 120.0))
    width, height = _bbox_to_size([float(value) for value in bbox], pixel_deg)
    target_dir = ensure_directory(context.raw_root / "landslide_susceptibility" / "global")
    slug = str(dataset_cfg.get("target_slug", "study_area")).strip() or "study_area"
    target_path = target_dir / f"nasa_landslide_susceptibility_{slug}.tif"

    if target_path.exists():
        ok, _ = _validate_raster(target_path)
        if ok:
            return [
                downloaded_record(
                    dataset_name="landslide_susceptibility",
                    source_url=LANDSLIDE_IMAGE_SERVICE_URL,
                    local_path=target_path,
                    context=context,
                    license_or_access_note=LANDSLIDE_LICENSE_NOTE,
                    spatial_resolution_raw="NASA global landslide susceptibility map clip; exported at requested 30 arc-second grid",
                    temporal_resolution="static susceptibility ranking",
                    bbox=bbox,
                    notes="Reused an existing local landslide susceptibility clip.",
                ),
            ]

    image_params = {
        "bbox": ",".join(str(float(value)) for value in bbox),
        "bboxSR": 4326,
        "imageSR": 4326,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "S8",
        "f": "image",
    }
    legacy_params = {
        **image_params,
        "transparent": "false",
    }
    export_urls = [
        f"{LANDSLIDE_IMAGE_SERVICE_URL}/exportImage?{urlencode(image_params)}",
        f"{LANDSLIDE_LEGACY_SERVICE_URL}/export?{urlencode(legacy_params)}",
        f"{LANDSLIDE_FALLBACK_SERVICE_URL}/export?{urlencode(legacy_params)}",
    ]

    selected_export_url = ""
    failure_messages: list[str] = []

    from src.data.utils import download_file

    for export_url in export_urls:
        try:
            download_file(export_url, target_path, context)
            ok, reason = _validate_raster(target_path)
            if not ok:
                target_path.unlink(missing_ok=True)
                raise ValueError(reason)
            selected_export_url = export_url
            break
        except Exception as exc:  # pragma: no cover - runtime/provider dependent
            failure_messages.append(f"{export_url}: {exc}")

    if not selected_export_url:
        failure_summary = "\n".join(failure_messages)
        instructions = f"""# Manual Steps For NASA Global Landslide Susceptibility

Automatic export from the NASA map service did not complete successfully.

Service roots attempted:
- {LANDSLIDE_IMAGE_SERVICE_URL}
- {LANDSLIDE_LEGACY_SERVICE_URL}
- {LANDSLIDE_FALLBACK_SERVICE_URL}

Suggested export URLs:
- {export_urls[0]}
- {export_urls[1]}
- {export_urls[2]}

What to do:
1. Open the service in a browser or GIS client.
2. Export or save a TIFF clip for the study-area bbox.
3. Place it under `data/raw/landslide_susceptibility/global/`.
4. Re-run the fetch and inspect commands.
"""
        return [
            manual_record(
                dataset_name="landslide_susceptibility",
                source_url=LANDSLIDE_IMAGE_SERVICE_URL,
                context=context,
                instruction_text=instructions,
                license_or_access_note=LANDSLIDE_LICENSE_NOTE,
                spatial_resolution_raw="Global susceptibility map; exported clip requested at 30 arc-second grid",
                temporal_resolution="static susceptibility ranking",
                bbox=bbox,
                notes=f"NASA map service export did not complete successfully. Attempt details:\n{failure_summary}",
            ),
        ]

    return [
        downloaded_record(
            dataset_name="landslide_susceptibility",
            source_url=selected_export_url,
            local_path=target_path,
            context=context,
            license_or_access_note=LANDSLIDE_LICENSE_NOTE,
            spatial_resolution_raw="NASA global landslide susceptibility map clip; exported at requested 30 arc-second grid",
            temporal_resolution="static susceptibility ranking",
            bbox=bbox,
            notes=join_notes(
                "Exported a study-area clip from a NASA landslide susceptibility map service.",
                f"Successful service endpoint: {selected_export_url}.",
                "This first pass uses a bbox export at approximately 30 arc-second resolution.",
            ),
        ),
    ]
