"""Fetcher for ERA5 / ERA5-Land products via the CDS API."""

from __future__ import annotations

import os
from pathlib import Path

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, join_notes, manual_record, validate_download


ERA5_DEFAULT_DATASET = "reanalysis-era5-land-monthly-means"
ERA5_LICENSE_NOTE = "Copernicus Climate Data Store licence applies. Users usually need an account and dataset licence acceptance."
ERA5_DEFAULT_CDS_URL = "https://cds.climate.copernicus.eu/api"


def _prepare_cds_environment() -> bool:
    """Normalize common CDS credential env var aliases used in .env files."""

    key = os.getenv("CDSAPI_KEY") or os.getenv("CDS_API_KEY")
    url = os.getenv("CDSAPI_URL") or os.getenv("CDS_API_URL")

    if key and not os.getenv("CDSAPI_KEY"):
        os.environ["CDSAPI_KEY"] = key
    if key and not url:
        url = ERA5_DEFAULT_CDS_URL
    if url and not os.getenv("CDSAPI_URL"):
        os.environ["CDSAPI_URL"] = url

    return bool(os.getenv("CDSAPI_KEY") and os.getenv("CDSAPI_URL"))


def _has_cds_credentials() -> bool:
    if _prepare_cds_environment():
        return True
    return Path.home().joinpath(".cdsapirc").exists()


def _build_request_from_config(dataset_cfg: dict) -> dict:
    request = dataset_cfg.get("request")
    if isinstance(request, dict) and request:
        return dict(request)

    generated: dict[str, object] = {}
    passthrough_keys = [
        "product_type",
        "variable",
        "year",
        "month",
        "day",
        "time",
        "area",
        "data_format",
        "download_format",
        "format",
    ]
    for key in passthrough_keys:
        if key in dataset_cfg and dataset_cfg[key] not in (None, ""):
            generated[key] = dataset_cfg[key]
    return generated


def _default_target_name(dataset_id: str, request: dict) -> str:
    format_hint = str(request.get("data_format") or request.get("format") or "netcdf").lower()
    suffix = ".grib" if "grib" in format_hint else ".nc"
    return f"{dataset_id}{suffix}"


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Retrieve an ERA5-family product through `cdsapi` when credentials are available."""

    dataset_id = str(dataset_cfg.get("dataset_id", ERA5_DEFAULT_DATASET))
    source_url = str(dataset_cfg.get("source_url", f"https://cds.climate.copernicus.eu/datasets/{dataset_id}"))
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "See CDS dataset metadata"))
    temporal_resolution = str(dataset_cfg.get("temporal_resolution", "See CDS dataset metadata"))
    bbox = dataset_cfg.get("bbox")
    request = _build_request_from_config(dataset_cfg)

    if not _has_cds_credentials():
        instructions = f"""# Manual Steps For ERA5 / ERA5-Land

The CDS API credentials were not found.

Expected credential locations:
- environment variables: `CDSAPI_URL` and `CDSAPI_KEY`
- accepted aliases in `.env`: `CDS_API_URL` and `CDS_API_KEY`
- or `~/.cdsapirc`

Dataset page:
- {source_url}

What to do:
1. Create or sign in to a Copernicus Climate Data Store account.
2. Accept the dataset licence on the dataset page if prompted.
3. Configure `~/.cdsapirc` or the relevant environment variables.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="era5",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=ERA5_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes="CDS API credentials were not detected.",
            ),
        ]

    if not request:
        instructions = f"""# Manual Steps For ERA5 / ERA5-Land

No CDS API request payload was configured for dataset `{dataset_id}`.

The fetcher accepts either:
1. `datasets.era5.request` as a direct CDS request dictionary, or
2. high-level keys such as `variable`, `year`, `month`, `time`, `area`, and `data_format`.

Dataset page:
- {source_url}
"""
        return [
            manual_record(
                dataset_name="era5",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=ERA5_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes="No ERA5 request payload was configured.",
            ),
        ]

    target_name = str(dataset_cfg.get("target_filename", _default_target_name(dataset_id, request)))
    target_path = ensure_directory(context.raw_root / "era5") / target_name

    if target_path.exists():
        ok, _ = validate_download(target_path)
        if ok:
            return [
                downloaded_record(
                    dataset_name="era5",
                    source_url=source_url,
                    local_path=target_path,
                    context=context,
                    license_or_access_note=ERA5_LICENSE_NOTE,
                    spatial_resolution_raw=spatial_resolution_raw,
                    temporal_resolution=temporal_resolution,
                    bbox=bbox,
                    notes="Reused an existing local ERA5-family file.",
                ),
            ]

    try:
        import cdsapi

        client = cdsapi.Client(quiet=True, progress=False)
        client.retrieve(dataset_id, request, str(target_path))
    except Exception as exc:  # pragma: no cover - runtime/auth/provider dependent
        instructions = f"""# Manual Steps For ERA5 / ERA5-Land

The CDS API call did not complete successfully.

Dataset page:
- {source_url}

Configured dataset id:
- `{dataset_id}`

Configured request:
```python
{request}
```

What to check:
1. Your CDS credentials are valid.
2. You accepted the licence for the dataset.
3. The request fields match the selected dataset.
4. After fixing the issue, re-run the fetch command.
"""
        return [
            manual_record(
                dataset_name="era5",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=ERA5_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes=f"CDS API retrieval did not complete successfully: {exc}",
            ),
        ]

    return [
        downloaded_record(
            dataset_name="era5",
            source_url=source_url,
            local_path=target_path,
            context=context,
            license_or_access_note=ERA5_LICENSE_NOTE,
            spatial_resolution_raw=spatial_resolution_raw,
            temporal_resolution=temporal_resolution,
            bbox=bbox,
            notes=join_notes(
                f"Retrieved `{dataset_id}` through the CDS API.",
                "Request parameters were taken from config/datasets.yaml.",
            ),
        ),
    ]
