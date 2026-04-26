"""Configuration loading and study-area resolution helpers."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pycountry
import yaml


def _as_float_bbox(values: Any) -> list[float] | None:
    if values is None:
        return None
    bbox = [float(value) for value in values]
    if len(bbox) != 4:
        raise ValueError("study_area.bbox must have exactly four values: [minx, miny, maxx, maxy]")
    return bbox


def _study_area_tokens(study_area: dict[str, Any]) -> dict[str, str]:
    bbox = _as_float_bbox(study_area.get("bbox"))
    country_code = str(study_area.get("country_code", "")).strip().upper()
    country_name = str(study_area.get("country_name", "")).strip()
    slug = str(study_area.get("slug", "")).strip()
    geofabrik_id = str(study_area.get("geofabrik_id", "")).strip().strip("/")

    if not slug:
        slug = country_name.lower().replace(" ", "-") if country_name else country_code.lower()

    tokens = {
        "country_code": country_code,
        "country_name": country_name,
        "slug": slug,
        "geofabrik_id": geofabrik_id,
        "bbox": ",".join(str(value) for value in bbox) if bbox else "",
        "minx": str(bbox[0]) if bbox else "",
        "miny": str(bbox[1]) if bbox else "",
        "maxx": str(bbox[2]) if bbox else "",
        "maxy": str(bbox[3]) if bbox else "",
        "north": str(bbox[3]) if bbox else "",
        "west": str(bbox[0]) if bbox else "",
        "south": str(bbox[1]) if bbox else "",
        "east": str(bbox[2]) if bbox else "",
    }
    return tokens


def _render_templates(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**tokens)
    if isinstance(value, list):
        return [_render_templates(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _render_templates(item, tokens) for key, item in value.items()}
    return value


def _ensure_dataset_defaults(config: dict[str, Any], study_area: dict[str, Any]) -> dict[str, Any]:
    datasets = config.setdefault("datasets", {})
    bbox = _as_float_bbox(study_area.get("bbox"))
    country_code = str(study_area.get("country_code", "")).strip().upper()
    geofabrik_id = str(study_area.get("geofabrik_id", "")).strip().strip("/")
    slug = str(study_area.get("slug", "")).strip()
    if not slug:
        country_name = str(study_area.get("country_name", "")).strip()
        slug = country_name.lower().replace(" ", "-") if country_name else country_code.lower()

    if "gadm" in datasets and country_code:
        datasets["gadm"].setdefault("country_codes", [country_code])

    if "osm" in datasets and geofabrik_id:
        datasets["osm"].setdefault("geofabrik_ids", [geofabrik_id])

    for dataset_name in [
        "chirps",
        "era5",
        "flood",
        "flood_depth",
        "coastaldem",
        "soilgrids",
        "road_surface",
        "landslide_susceptibility",
        "worldcover",
        "visibility_noaa_isd",
    ]:
        dataset_cfg = datasets.get(dataset_name)
        if dataset_cfg is not None and bbox is not None:
            dataset_cfg.setdefault("bbox", bbox)
            if country_code:
                dataset_cfg.setdefault("country_code", country_code)

    era5_cfg = datasets.get("era5")
    if era5_cfg is not None and bbox is not None:
        request = era5_cfg.setdefault("request", {})
        request.setdefault("area", [bbox[3], bbox[0], bbox[1], bbox[2]])
        if isinstance(request.get("area"), list):
            request["area"] = [float(value) for value in request["area"]]
        if slug:
            era5_cfg.setdefault("target_filename", f"era5-land-{slug}.nc")

    landslide_cfg = datasets.get("landslide_susceptibility")
    if landslide_cfg is not None and slug:
        landslide_cfg.setdefault("target_slug", slug)

    road_surface_cfg = datasets.get("road_surface")
    if road_surface_cfg is not None and country_code:
        road_surface_cfg.setdefault("country_codes", [country_code])

    return config


def resolve_config(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve study-area defaults and string templates in a config dictionary."""

    resolved = deepcopy(config)
    study_area = resolved.get("study_area") or {}
    tokens = _study_area_tokens(study_area)
    resolved = _render_templates(resolved, tokens)
    return _ensure_dataset_defaults(resolved, study_area)


def apply_study_area_override(config: dict[str, Any], country_code: str) -> dict[str, Any]:
    """Return a config copy with a one-off study-area country override."""

    overridden = deepcopy(config)
    iso3 = str(country_code).strip().upper()
    if not iso3:
        return overridden

    study_area = overridden.setdefault("study_area", {})
    study_area["country_code"] = iso3
    # Do not silently reuse bbox/geofabrik metadata from a different country.
    # One-off ISO3 overrides are safe for country-keyed caches such as GADM and road_surface,
    # but bbox-driven datasets must be reconfigured explicitly for the new territory.
    study_area.pop("bbox", None)
    study_area.pop("geofabrik_id", None)

    country = pycountry.countries.get(alpha_3=iso3)
    if country is not None:
        study_area["country_name"] = str(country.name)
        study_area["slug"] = str(country.name).lower().replace(", ", "-").replace(",", "").replace(" ", "-")

    datasets = overridden.setdefault("datasets", {})
    if "gadm" in datasets:
        datasets["gadm"]["country_codes"] = [iso3]
    if "road_surface" in datasets:
        datasets["road_surface"]["country_codes"] = [iso3]

    return overridden


def load_config(path: Path, *, country_code_override: str = "") -> dict[str, Any]:
    """Load YAML config and resolve any study-area driven defaults."""

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if country_code_override:
        raw = apply_study_area_override(raw, country_code_override)
    return resolve_config(raw)
