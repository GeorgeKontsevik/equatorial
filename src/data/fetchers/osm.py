"""Fetcher for OpenStreetMap raw extracts via Geofabrik."""

from __future__ import annotations

from pathlib import Path

import httpx

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record


GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
OSM_LICENSE_NOTE = "OpenStreetMap data via Geofabrik extracts. ODbL 1.0 applies to the underlying OSM data."


def _load_geofabrik_index(index_url: str, context) -> dict:
    headers = {"User-Agent": context.user_agent}
    response = httpx.get(index_url, headers=headers, timeout=context.timeout_seconds, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def _slugify_geofabrik_id(geofabrik_id: str) -> str:
    return str(geofabrik_id).strip().strip("/").replace("/", "__")


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download country or regional OSM extracts from Geofabrik."""

    geofabrik_ids = [str(value).strip().strip("/") for value in dataset_cfg.get("geofabrik_ids", []) if str(value).strip()]
    extract_format = str(dataset_cfg.get("extract_format", "pbf")).lower()
    index_url = str(dataset_cfg.get("index_url", GEOFABRIK_INDEX_URL))
    bbox = dataset_cfg.get("bbox")

    if bool(dataset_cfg.get("planet", False)):
        instructions = f"""# Manual Steps For OSM Planet Data

This first-pass pipeline only automates Geofabrik regional extracts. Full-planet workflows are intentionally left manual.

Official Geofabrik technical documentation:
- https://download.geofabrik.de/technical.html

What to do:
1. Download the planet or a suitable regional extract from an official OSM/Geofabrik source.
2. Place the file under `data/raw/osm/planet/`.
3. Record the exact file name and provenance.
4. Re-run `python -m src.data.inspect --config config/datasets.yaml` to catalog it.
"""
        return [
            manual_record(
                dataset_name="osm",
                source_url=index_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=OSM_LICENSE_NOTE,
                spatial_resolution_raw="Vector OSM extract",
                temporal_resolution="snapshot",
                bbox=bbox,
                notes="Planet-scale OSM download requires a manual workflow in this conservative first pass.",
                instruction_name="osm_planet",
            ),
        ]

    if not geofabrik_ids:
        instructions = f"""# Manual Steps For OSM / Geofabrik

No `geofabrik_ids` were configured, so the fetcher does not know which regional extract to download.

Official machine-readable index:
- {index_url}

How to configure:
1. Open the Geofabrik index or browse https://download.geofabrik.de/.
2. Identify the `id` for each desired extract, for example `south-america/bolivia`.
3. Set `datasets.osm.geofabrik_ids` in `config/datasets.yaml`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="osm",
                source_url=index_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=OSM_LICENSE_NOTE,
                spatial_resolution_raw="Vector OSM extract",
                temporal_resolution="snapshot",
                bbox=bbox,
                notes="No Geofabrik extract identifiers were configured.",
            ),
        ]

    index_json = _load_geofabrik_index(index_url, context)
    features = index_json.get("features", [])
    features_by_id = {str(feature.get("properties", {}).get("id", "")).strip("/"): feature for feature in features}

    records: list[CatalogRecord] = []
    for geofabrik_id in geofabrik_ids:
        feature = features_by_id.get(geofabrik_id)
        if feature is None:
            instructions = f"""# Manual Steps For OSM Extract `{geofabrik_id}`

The configured Geofabrik id `{geofabrik_id}` was not found in the current index.

Index URL:
- {index_url}

What to do:
1. Confirm the desired extract exists in the Geofabrik hierarchy.
2. Update `datasets.osm.geofabrik_ids` with a valid id.
3. Re-run the fetch command.
"""
            records.append(
                manual_record(
                    dataset_name="osm",
                    source_url=index_url,
                    context=context,
                    instruction_text=instructions,
                    license_or_access_note=OSM_LICENSE_NOTE,
                    spatial_resolution_raw="Vector OSM extract",
                    temporal_resolution="snapshot",
                    bbox=bbox,
                    notes=f"Configured Geofabrik id `{geofabrik_id}` was not found in the index.",
                    instruction_name=f"osm_{_slugify_geofabrik_id(geofabrik_id)}",
                ),
            )
            continue

        properties = feature.get("properties", {})
        urls = properties.get("urls", {})
        source_url = str(urls.get(extract_format, "")).strip()
        if not source_url:
            instructions = f"""# Manual Steps For OSM Extract `{geofabrik_id}`

The Geofabrik index entry exists, but it does not advertise a `{extract_format}` URL in the `urls` field.

Index URL:
- {index_url}

What to do:
1. Inspect the Geofabrik page for `{geofabrik_id}` manually.
2. Download the desired file format yourself.
3. Place it under `data/raw/osm/{_slugify_geofabrik_id(geofabrik_id)}/`.
4. Re-run the fetch and inspect commands.
"""
            records.append(
                manual_record(
                    dataset_name="osm",
                    source_url=index_url,
                    context=context,
                    instruction_text=instructions,
                    license_or_access_note=OSM_LICENSE_NOTE,
                    spatial_resolution_raw="Vector OSM extract",
                    temporal_resolution="snapshot",
                    bbox=bbox,
                    notes=f"The Geofabrik index entry for `{geofabrik_id}` has no `{extract_format}` URL.",
                    instruction_name=f"osm_{_slugify_geofabrik_id(geofabrik_id)}",
                ),
            )
            continue

        target_dir = ensure_directory(context.raw_root / "osm" / _slugify_geofabrik_id(geofabrik_id))
        filename = Path(source_url).name or f"{_slugify_geofabrik_id(geofabrik_id)}.{extract_format}"
        local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
        records.append(
            downloaded_record(
                dataset_name="osm",
                source_url=source_url,
                local_path=local_path,
                context=context,
                license_or_access_note=OSM_LICENSE_NOTE,
                spatial_resolution_raw="Vector OSM extract snapshot",
                temporal_resolution="snapshot",
                bbox=bbox,
                notes=join_notes(
                    f"Geofabrik extract id: {geofabrik_id}.",
                    f"Requested format: {extract_format}.",
                    "Reused an existing local copy." if reused else "Downloaded from the official Geofabrik extract URL.",
                ),
            ),
        )

    return records
