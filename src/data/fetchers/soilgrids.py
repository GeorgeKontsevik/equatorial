"""Fetcher for SoilGrids subsets using the official WCS service."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable
import subprocess
from urllib.parse import urlencode

import httpx
from pyproj import Transformer
import rasterio
from rasterio.errors import RasterioIOError

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, join_notes, manual_record, validate_download


SOILGRIDS_SERVICE_TEMPLATE = "https://maps.isric.org/mapserv?map=/map/{property}.map"
SOILGRIDS_DOCS_URL = "https://docs.isric.org/globaldata/soilgrids/wcs.html"
SOILGRIDS_LICENSE_NOTE = "SoilGrids data are distributed by ISRIC under CC BY 4.0; confirm current terms before redistribution."
IGH_PROJ4 = "+proj=igh +lat_0=0 +lon_0=0 +datum=WGS84 +units=m +no_defs"


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _project_bbox_to_igh(bbox: list[float]) -> tuple[float, float, float, float]:
    transformer = Transformer.from_crs("EPSG:4326", IGH_PROJ4, always_xy=True)
    corners = [
        transformer.transform(float(bbox[0]), float(bbox[1])),
        transformer.transform(float(bbox[0]), float(bbox[3])),
        transformer.transform(float(bbox[2]), float(bbox[1])),
        transformer.transform(float(bbox[2]), float(bbox[3])),
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _candidate_subset_params(bbox: list[float]) -> Iterable[list[tuple[str, str]]]:
    """Yield conservative WCS request variants accepted by SoilGrids servers.

    In practice, SoilGrids can differ in axis-name handling depending on endpoint/proxy.
    We try only documented-safe combinations and avoid inventing custom endpoints.
    """

    lon_min, lat_min, lon_max, lat_max = [float(value) for value in bbox]
    igh_minx, igh_miny, igh_maxx, igh_maxy = _project_bbox_to_igh(bbox)

    # Primary path follows the documented SoilGrids WCS request pattern.
    yield [
        ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/152160"),
        ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/152160"),
        ("SUBSET", f"X({igh_minx},{igh_maxx})"),
        ("SUBSET", f"Y({igh_miny},{igh_maxy})"),
    ]

    yield [
        ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("SUBSET", f"X({lon_min},{lon_max})"),
        ("SUBSET", f"Y({lat_min},{lat_max})"),
    ]
    yield [
        ("SUBSETTINGCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("OUTPUTCRS", "http://www.opengis.net/def/crs/EPSG/0/4326"),
        ("SUBSET", f"Long({lon_min},{lon_max})"),
        ("SUBSET", f"Lat({lat_min},{lat_max})"),
    ]


def _download_with_curl(service_url: str, params: list[tuple[str, str]], target_path: Path, context) -> bool:
    request_url = f"{service_url}&{urlencode(params, doseq=True, safe='(),:/')}"
    command = [
        "curl",
        "-L",
        "--fail",
        "--retry",
        str(max(1, int(context.max_retries))),
        "--retry-delay",
        "2",
        "-A",
        context.user_agent,
        "-o",
        str(target_path),
        request_url,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        target_path.unlink(missing_ok=True)
        return False
    return True


def _try_wcs_download(property_name: str, coverage_id: str, bbox: list[float], target_path: Path, context) -> str | None:
    service_url = SOILGRIDS_SERVICE_TEMPLATE.format(property=property_name)
    base_params = [
        ("SERVICE", "WCS"),
        ("VERSION", "2.0.1"),
        ("REQUEST", "GetCoverage"),
        ("COVERAGEID", coverage_id),
        ("FORMAT", "GEOTIFF_INT16"),
    ]

    for subset_params in _candidate_subset_params(bbox):
        params = base_params + list(subset_params)
        if context.logger:
            context.logger.info("SoilGrids WCS attempt for %s using subset profile %s", coverage_id, subset_params[0][1])
        httpx_ok = False
        try:
            response = httpx.get(
                service_url,
                params=params,
                headers={"User-Agent": context.user_agent},
                timeout=context.timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
            _write_bytes(target_path, response.content)
            httpx_ok = True
        except (httpx.HTTPError, OSError):
            if not _download_with_curl(service_url, params, target_path, context):
                continue

        try:
            ok, _ = validate_download(target_path)
            if not ok:
                # Some WCS endpoints return XML errors with HTTP 200. If HTTPX returned
                # such content, retry once with curl, which is often more resilient.
                if httpx_ok and _download_with_curl(service_url, params, target_path, context):
                    ok, _ = validate_download(target_path)
                if not ok:
                    target_path.unlink(missing_ok=True)
                    continue
            with rasterio.open(target_path):
                pass
            return service_url
        except (RasterioIOError, OSError, ValueError):
            if httpx_ok and _download_with_curl(service_url, params, target_path, context):
                try:
                    with rasterio.open(target_path):
                        pass
                    return service_url
                except (RasterioIOError, OSError, ValueError):
                    pass
            target_path.unlink(missing_ok=True)
            continue

    return None


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download SoilGrids subsets for configured properties and a WGS84 bbox."""

    bbox = dataset_cfg.get("bbox")
    properties = [str(value).strip() for value in dataset_cfg.get("properties", ["clay", "sand", "silt", "bdod", "soc"]) if str(value).strip()]
    depth_interval = str(dataset_cfg.get("depth_interval", "0-5cm"))
    quantile = str(dataset_cfg.get("quantile", "Q0.5"))
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "250 m"))

    if not bbox or len(bbox) != 4:
        instructions = f"""# Manual Steps For SoilGrids

The SoilGrids fetcher needs a WGS84 bbox to request WCS subsets.

Official WCS documentation:
- {SOILGRIDS_DOCS_URL}

How to proceed:
1. Set `datasets.soilgrids.bbox` to `[minx, miny, maxx, maxy]`.
2. Re-run the fetch command.
3. If you prefer manual download, use either the WCS service or the WebDAV/VRT access methods described in the official docs.
"""
        return [
            manual_record(
                dataset_name="soilgrids",
                source_url=SOILGRIDS_DOCS_URL,
                context=context,
                instruction_text=instructions,
                license_or_access_note=SOILGRIDS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static soil property grids",
                bbox=bbox,
                notes="No SoilGrids bbox was configured.",
            ),
        ]

    records: list[CatalogRecord] = []
    target_dir = ensure_directory(context.raw_root / "soilgrids")

    for property_name in properties:
        coverage_id = f"{property_name}_{depth_interval}_{quantile}"
        target_path = target_dir / f"{coverage_id}.tif"
        if target_path.exists():
            ok, _ = validate_download(target_path)
            if ok:
                records.append(
                    downloaded_record(
                        dataset_name="soilgrids",
                        source_url=SOILGRIDS_SERVICE_TEMPLATE.format(property=property_name),
                        local_path=target_path,
                        context=context,
                        license_or_access_note=SOILGRIDS_LICENSE_NOTE,
                        spatial_resolution_raw=spatial_resolution_raw,
                        temporal_resolution="static soil property grids",
                        bbox=bbox,
                        notes=f"Reused an existing local SoilGrids subset for `{coverage_id}`.",
                    ),
                )
                continue

        service_url = _try_wcs_download(property_name, coverage_id, bbox, target_path, context)
        if service_url is None:
            instructions = f"""# Manual Steps For SoilGrids `{coverage_id}`

Automatic WCS subset retrieval did not succeed for this layer in the current environment.

Official documentation:
- {SOILGRIDS_DOCS_URL}

Requested layer:
- property: `{property_name}`
- coverage id: `{coverage_id}`
- bbox (WGS84): `{bbox}`

What to do:
1. Use the official SoilGrids WCS or WebDAV access method described in the docs.
2. Save the resulting file under `data/raw/soilgrids/{coverage_id}.tif`.
3. Re-run the fetch and inspect commands.
"""
            records.append(
                manual_record(
                    dataset_name="soilgrids",
                    source_url=SOILGRIDS_DOCS_URL,
                    context=context,
                    instruction_text=instructions,
                    license_or_access_note=SOILGRIDS_LICENSE_NOTE,
                    spatial_resolution_raw=spatial_resolution_raw,
                    temporal_resolution="static soil property grids",
                    bbox=bbox,
                    notes=f"Automatic SoilGrids WCS subset retrieval did not succeed for `{coverage_id}`.",
                    instruction_name=f"soilgrids_{coverage_id}",
                ),
            )
            continue

        records.append(
            downloaded_record(
                dataset_name="soilgrids",
                source_url=service_url,
                local_path=target_path,
                context=context,
                license_or_access_note=SOILGRIDS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution="static soil property grids",
                bbox=bbox,
                notes=join_notes(
                    f"SoilGrids WCS subset for `{coverage_id}`.",
                    "Output CRS follows the SoilGrids WCS subset request.",
                ),
            ),
        )

    return records
