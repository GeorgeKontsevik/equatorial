"""Fetcher for paved/unpaved road-surface data placeholders."""

from __future__ import annotations

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record, validate_download


ROAD_SURFACE_HDX_URL = (
    "https://data.humdata.org/organization/"
    "heidelberg-institute-for-geoinformation-technology"
    "?dataseries_name=Heidelberg%20Institute%20for%20Geoinformation%20Technology%20-%20Road%20Surface%20Data"
    "&q=&ext_page_size=25"
)
ROAD_SURFACE_DOI = "https://doi.org/10.1016/j.isprsjprs.2025.02.020"
ROAD_SURFACE_URL_TEMPLATE = "https://downloads.ohsome.org/hdx/mapillary_road_surface/heigit_{country_code_lower}_roadsurface_lines.gpkg"
ROAD_SURFACE_LICENSE_NOTE = (
    "Open HDX dataset (HeiGIT road-surface data, paved/unpaved). "
    "Review HDX terms and keep citation/attribution for OSM and Mapillary-derived inputs."
)


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download or catalog country-level road-surface files."""

    raw_dir = context.raw_root / "road_surface"
    country_codes = [str(value).strip().upper() for value in dataset_cfg.get("country_codes", []) if str(value).strip()]
    records: list[CatalogRecord] = []

    if country_codes:
        url_template = str(dataset_cfg.get("source_url_template", ROAD_SURFACE_URL_TEMPLATE))
        for country_code in country_codes:
            target_dir = ensure_directory(raw_dir / country_code)
            source_url = url_template.format(
                country_code=country_code,
                country_code_lower=country_code.lower(),
            )
            filename = source_url.rsplit("/", 1)[-1] or f"heigit_{country_code.lower()}_roadsurface_lines.gpkg"
            try:
                local_path, reused = ensure_local_copy(source_url, target_dir / filename, context)
                records.append(
                    downloaded_record(
                        dataset_name="road_surface",
                        source_url=source_url,
                        local_path=local_path,
                        context=context,
                        license_or_access_note=str(dataset_cfg.get("license_or_access_note", ROAD_SURFACE_LICENSE_NOTE)),
                        spatial_resolution_raw=str(
                            dataset_cfg.get(
                                "spatial_resolution_raw",
                                "Vector road segments with binary paved/unpaved attribution (country releases)",
                            ),
                        ),
                        temporal_resolution="snapshot",
                        bbox=dataset_cfg.get("bbox"),
                        notes=join_notes(
                            f"HeiGIT road-surface country release for {country_code}.",
                            "Reused an existing local copy." if reused else "Downloaded from the direct ohsome/HDX country file URL.",
                        ),
                    ),
                )
            except Exception as exc:  # pragma: no cover - runtime/provider dependent
                instructions = f"""# Manual Steps For Road Surface `{country_code}` (`paved / unpaved`)

Automatic download from the direct country file URL did not complete successfully.

Direct file URL:
- {source_url}

Dataset page:
- {ROAD_SURFACE_HDX_URL}

Reference paper:
- {ROAD_SURFACE_DOI}

What to do:
1. Download the file manually from the direct URL above.
2. Place it under `data/raw/road_surface/{country_code}/{filename}`.
3. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
"""
                records.append(
                    manual_record(
                        dataset_name="road_surface",
                        source_url=source_url,
                        context=context,
                        instruction_text=instructions,
                        license_or_access_note=str(dataset_cfg.get("license_or_access_note", ROAD_SURFACE_LICENSE_NOTE)),
                        spatial_resolution_raw=str(
                            dataset_cfg.get(
                                "spatial_resolution_raw",
                                "Vector road segments with binary paved/unpaved attribution (country releases)",
                            ),
                        ),
                        temporal_resolution="snapshot",
                        bbox=dataset_cfg.get("bbox"),
                        notes=f"Automatic road-surface download did not succeed for {country_code}: {exc}",
                        instruction_name=f"road_surface_{country_code.lower()}",
                    ),
                )
        return records

    existing_files = [path for path in raw_dir.rglob("*") if path.is_file()]
    for path in sorted(existing_files):
        ok, _ = validate_download(path)
        if not ok:
            continue
        records.append(
            downloaded_record(
                dataset_name="road_surface",
                source_url=str(dataset_cfg.get("source_url", ROAD_SURFACE_HDX_URL)),
                local_path=path,
                context=context,
                license_or_access_note=str(dataset_cfg.get("license_or_access_note", ROAD_SURFACE_LICENSE_NOTE)),
                spatial_resolution_raw=str(
                    dataset_cfg.get(
                        "spatial_resolution_raw",
                        "Vector road segments with binary paved/unpaved attribution (country releases)",
                    ),
                ),
                temporal_resolution="snapshot",
                bbox=dataset_cfg.get("bbox"),
                notes="Existing manually acquired road-surface file was cataloged.",
            ),
        )

    if records:
        return records

    requested_countries = ", ".join(country_codes) if country_codes else "the configured study area"
    instructions = f"""# Manual Steps For Road Surface (`paved / unpaved`)

Automatic country-level download was not attempted because no `country_codes` were configured.

Primary dataset page:
- {ROAD_SURFACE_HDX_URL}

Reference paper:
- {ROAD_SURFACE_DOI}

What to do:
1. Open the HDX page and download the country files you need for {requested_countries}.
2. Keep the original metadata and citation files from the download package.
3. Place the downloaded files under `data/raw/road_surface/<ISO3>/`.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml` and `python -m src.data.inspect --config config/datasets.yaml`.
"""
    return [
        manual_record(
            dataset_name="road_surface",
            source_url=str(dataset_cfg.get("source_url", ROAD_SURFACE_HDX_URL)),
            context=context,
            instruction_text=instructions,
            license_or_access_note=str(dataset_cfg.get("license_or_access_note", ROAD_SURFACE_LICENSE_NOTE)),
            spatial_resolution_raw=str(
                dataset_cfg.get(
                    "spatial_resolution_raw",
                    "Vector road segments with binary paved/unpaved attribution (country releases)",
                ),
            ),
            temporal_resolution="snapshot",
            bbox=dataset_cfg.get("bbox"),
            notes="Road-surface data is configured as a manual acquisition step in this first pass.",
        ),
    ]
