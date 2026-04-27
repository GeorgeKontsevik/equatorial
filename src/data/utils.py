"""Shared helpers for downloading, validating, and inspecting data assets."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import httpx
import rasterio
import xarray as xr
from rasterio.errors import RasterioIOError

from src.data.catalog import CatalogRecord


DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRIES = 3


@dataclass(slots=True)
class FetchContext:
    """Runtime paths and settings shared by all fetchers."""

    project_root: Path
    data_root: Path
    raw_root: Path
    metadata_root: Path
    manual_steps_root: Path
    logs_root: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    user_agent: str = "equatorial-data-fetch/0.1.0"
    logger: logging.Logger | None = None


def utc_now_iso() -> str:
    """Return the current UTC time in ISO-8601 format."""

    return datetime.now(tz=UTC).replace(microsecond=0).isoformat()


def ensure_directory(path: Path) -> Path:
    """Create the directory if necessary and return it."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def load_project_env(project_root: Path) -> None:
    """Load simple KEY=VALUE pairs from a project-local `.env` file if present."""

    env_path = project_root / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def configure_logging(logs_root: Path, logger_name: str = "equatorial.data") -> logging.Logger:
    """Configure a file and stderr logger for the current run."""

    ensure_directory(logs_root)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

    file_handler = logging.FileHandler(logs_root / f"fetch_{stamp}.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def sha256_for_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Compute the SHA-256 checksum for a file."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive_integrity(path: Path) -> bool:
    """Check that a zip or tar archive can be opened without obvious corruption."""

    suffixes = {suffix.lower() for suffix in path.suffixes}
    if ".zip" in suffixes:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    if ".tar" in suffixes or ".tgz" in suffixes or ".gz" in suffixes:
        with tarfile.open(path) as archive:
            archive.getmembers()
            return True
    return True


def validate_download(path: Path) -> tuple[bool, str]:
    """Validate presence, non-empty size, and archive integrity where applicable."""

    if not path.exists():
        return False, "file does not exist"
    if path.stat().st_size <= 0:
        return False, "file has zero size"
    try:
        if not verify_archive_integrity(path):
            return False, "archive integrity check failed"
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        return False, f"archive validation failed: {exc}"
    return True, ""


def download_file(url: str, target_path: Path, context: FetchContext) -> Path:
    """Download a remote file with retry logic and atomic replacement."""

    ensure_directory(target_path.parent)
    headers = {"User-Agent": context.user_agent}
    last_error: Exception | None = None
    temp_path = target_path.with_suffix(target_path.suffix + ".part")

    for attempt in range(1, context.max_retries + 1):
        try:
            if context.logger:
                context.logger.info("Downloading %s -> %s (attempt %s/%s)", url, target_path, attempt, context.max_retries)
            with httpx.stream("GET", url, timeout=context.timeout_seconds, headers=headers, follow_redirects=True) as response:
                response.raise_for_status()
                with temp_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if chunk:
                            handle.write(chunk)
            temp_path.replace(target_path)
            ok, reason = validate_download(target_path)
            if not ok:
                raise ValueError(reason)
            return target_path
        except Exception as exc:  # pragma: no cover - exercised by runtime failures
            last_error = exc
            if context.logger:
                context.logger.warning("Download failed for %s: %s", url, exc)
            if temp_path.exists():
                temp_path.unlink()
            if attempt < context.max_retries:
                time.sleep(min(2 ** attempt, 10))

    raise RuntimeError(f"Failed to download {url}: {last_error}") from last_error


def ensure_local_copy(url: str, target_path: Path, context: FetchContext) -> tuple[Path, bool]:
    """Reuse an existing valid file or download it when missing or invalid."""

    if target_path.exists():
        ok, _ = validate_download(target_path)
        if ok:
            if context.logger:
                context.logger.info("Reusing existing file: %s", target_path)
            return target_path, True

        if context.logger:
            context.logger.warning("Existing file is invalid and will be replaced: %s", target_path)
        target_path.unlink()

    return download_file(url, target_path, context), False


def write_text(path: Path, content: str) -> Path:
    """Write UTF-8 text to disk."""

    ensure_directory(path.parent)
    path.write_text(content, encoding="utf-8")
    return path


def write_manual_instructions(dataset_name: str, content: str, context: FetchContext) -> Path:
    """Write a manual-download instruction file for a dataset."""

    manual_path = context.manual_steps_root / f"{dataset_name}.md"
    return write_text(manual_path, content)


def relative_to_project(path: Path, project_root: Path) -> str:
    """Return a stable project-relative path string when possible."""

    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def bbox_to_string(bbox: list[float] | tuple[float, float, float, float] | None) -> str:
    """Serialize a bounding box as JSON for catalog storage."""

    if bbox is None:
        return ""
    return json.dumps([float(value) for value in bbox])


def join_notes(*parts: str) -> str:
    """Join non-empty note fragments into one readable sentence block."""

    return " ".join(str(part).strip() for part in parts if str(part).strip())


def downloaded_record(
    *,
    dataset_name: str,
    source_url: str,
    local_path: Path,
    context: FetchContext,
    license_or_access_note: str,
    spatial_resolution_raw: str = "",
    temporal_resolution: str = "",
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    notes: str = "",
) -> CatalogRecord:
    """Build a catalog record for a successfully downloaded or reused file."""

    return CatalogRecord(
        dataset_name=dataset_name,
        source_url=source_url,
        download_date_utc=utc_now_iso(),
        local_path=relative_to_project(local_path, context.project_root),
        file_format=detect_file_format(local_path),
        license_or_access_note=license_or_access_note,
        spatial_resolution_raw=spatial_resolution_raw,
        temporal_resolution=temporal_resolution,
        bbox_if_known=bbox_to_string(bbox),
        checksum_sha256=sha256_for_file(local_path),
        status="downloaded",
        notes=notes,
    )


def manual_record(
    *,
    dataset_name: str,
    source_url: str,
    context: FetchContext,
    instruction_text: str,
    license_or_access_note: str,
    spatial_resolution_raw: str = "",
    temporal_resolution: str = "",
    bbox: list[float] | tuple[float, float, float, float] | None = None,
    notes: str = "",
    instruction_name: str | None = None,
) -> CatalogRecord:
    """Write manual instructions and return a catalog record with status=manual."""

    manual_key = instruction_name or dataset_name
    manual_path = write_manual_instructions(manual_key, instruction_text, context)
    return CatalogRecord(
        dataset_name=dataset_name,
        source_url=source_url,
        download_date_utc=utc_now_iso(),
        local_path=relative_to_project(manual_path, context.project_root),
        file_format="md",
        license_or_access_note=license_or_access_note,
        spatial_resolution_raw=spatial_resolution_raw,
        temporal_resolution=temporal_resolution,
        bbox_if_known=bbox_to_string(bbox),
        checksum_sha256=sha256_for_file(manual_path),
        status="manual",
        notes=notes or "Manual download or approval is required.",
    )


def inspect_spatial_file(path: Path) -> dict[str, str]:
    """Extract spatial metadata from raster, vector, or NetCDF-like files when feasible."""

    suffix = path.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return _inspect_raster(path)
    if suffix in {".gpkg", ".geojson", ".json", ".shp"}:
        return _inspect_vector(path)
    if suffix == ".nc":
        return _inspect_netcdf(path)
    return {}


def _inspect_raster(path: Path) -> dict[str, str]:
    with rasterio.open(path) as dataset:
        pixel_width = dataset.transform.a
        pixel_height = abs(dataset.transform.e)
        return {
            "crs": dataset.crs.to_string() if dataset.crs else "",
            "pixel_size": f"{pixel_width} x {pixel_height}",
            "geometry_type": "raster",
            "layer_names": ",".join(dataset.descriptions) if any(dataset.descriptions) else "",
            "raster_shape": f"{dataset.height} x {dataset.width}",
        }


def _inspect_vector(path: Path) -> dict[str, str]:
    layers = []
    geometry_types: set[str] = set()
    crs = ""

    if path.suffix.lower() == ".gpkg":
        import fiona

        layers = fiona.listlayers(path)
        target_layers = layers[:3] if layers else []
        for layer_name in target_layers:
            frame = gpd.read_file(path, layer=layer_name, rows=50)
            crs = crs or (frame.crs.to_string() if frame.crs else "")
            geometry_types.update(frame.geom_type.dropna().astype(str).unique().tolist())
    else:
        frame = gpd.read_file(path, rows=50)
        crs = frame.crs.to_string() if frame.crs else ""
        geometry_types.update(frame.geom_type.dropna().astype(str).unique().tolist())

    return {
        "crs": crs,
        "pixel_size": "",
        "geometry_type": ",".join(sorted(geometry_types)),
        "layer_names": ",".join(layers),
        "raster_shape": "",
    }


def _inspect_netcdf(path: Path) -> dict[str, str]:
    with xr.open_dataset(path) as dataset:
        dims = " x ".join(f"{name}={size}" for name, size in dataset.sizes.items())
        crs = ""
        if "crs" in dataset:
            crs = str(dataset["crs"].attrs.get("spatial_ref", "")) or str(dataset["crs"].attrs.get("grid_mapping_name", ""))

        pixel_size = ""
        if "longitude" in dataset.coords and len(dataset["longitude"]) > 1:
            xres = float(dataset["longitude"][1] - dataset["longitude"][0])
            if "latitude" in dataset.coords and len(dataset["latitude"]) > 1:
                yres = abs(float(dataset["latitude"][1] - dataset["latitude"][0]))
                pixel_size = f"{xres} x {yres}"

        return {
            "crs": crs,
            "pixel_size": pixel_size,
            "geometry_type": "netcdf",
            "layer_names": ",".join(dataset.data_vars.keys()),
            "raster_shape": dims,
        }


def detect_file_format(path: Path) -> str:
    """Return a normalized file format label."""

    suffixes = [suffix.lower() for suffix in path.suffixes]
    if suffixes[-2:] == [".tar", ".gz"]:
        return "tar.gz"
    if suffixes[-2:] == [".tar", ".bz2"]:
        return "tar.bz2"
    if suffixes[-2:] == [".tar", ".xz"]:
        return "tar.xz"
    if suffixes:
        return suffixes[-1].lstrip(".")
    return "unknown"


def extract_bounds(path: Path) -> list[float] | None:
    """Read file bounds when possible."""

    try:
        suffix = path.suffix.lower()
        if suffix in {".tif", ".tiff"}:
            with rasterio.open(path) as dataset:
                bounds = dataset.bounds
                return [bounds.left, bounds.bottom, bounds.right, bounds.top]
        if suffix in {".gpkg", ".geojson", ".json", ".shp"}:
            frame = gpd.read_file(path, rows=500)
            bounds = frame.total_bounds
            return [float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])]
    except (RasterioIOError, OSError, ValueError):
        return None
    return None
