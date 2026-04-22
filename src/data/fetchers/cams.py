"""Fetcher for CAMS global reanalysis products via the Atmosphere Data Store API."""

from __future__ import annotations

import os
from pathlib import Path

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, join_notes, manual_record, validate_download


CAMS_DEFAULT_DATASET = "cams-global-reanalysis-eac4"
CAMS_LICENSE_NOTE = (
    "Copernicus Atmosphere Data Store licence applies. Users usually need an ADS account and dataset licence acceptance."
)
CAMS_DEFAULT_ADS_URL = "https://ads.atmosphere.copernicus.eu/api"


def _prepare_ads_environment() -> tuple[str | None, str | None]:
    key = (
        os.getenv("ADSAPI_KEY")
        or os.getenv("ADS_API_KEY")
        or os.getenv("CAMSAPI_KEY")
        or os.getenv("CAMS_API_KEY")
        or os.getenv("CDSAPI_KEY")
        or os.getenv("CDS_API_KEY")
    )
    url = (
        os.getenv("ADSAPI_URL")
        or os.getenv("ADS_API_URL")
        or os.getenv("CAMSAPI_URL")
        or os.getenv("CAMS_API_URL")
        or os.getenv("CDSAPI_URL")
        or os.getenv("CDS_API_URL")
    )

    if key and not url:
        url = CAMS_DEFAULT_ADS_URL
    return key, url


def _has_ads_credentials() -> bool:
    key, url = _prepare_ads_environment()
    if key and url:
        return True
    return Path.home().joinpath(".cdsapirc").exists()


def _build_request_from_config(dataset_cfg: dict) -> dict:
    request = dataset_cfg.get("request")
    if isinstance(request, dict) and request:
        return dict(request)

    generated: dict[str, object] = {}
    passthrough_keys = [
        "date",
        "time",
        "variable",
        "model_level",
        "pressure_level",
        "level",
        "format",
        "data_format",
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
    """Retrieve a CAMS reanalysis product through the ADS API when credentials are available."""

    dataset_id = str(dataset_cfg.get("dataset_id", CAMS_DEFAULT_DATASET))
    source_url = str(dataset_cfg.get("source_url", f"https://ads.atmosphere.copernicus.eu/datasets/{dataset_id}"))
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "0.75 degree global atmospheric composition grid"))
    temporal_resolution = str(dataset_cfg.get("temporal_resolution", "3-hourly"))
    bbox = dataset_cfg.get("bbox")
    request = _build_request_from_config(dataset_cfg)

    if not _has_ads_credentials():
        instructions = f"""# Manual Steps For CAMS / ADS

The ADS API credentials were not found.

Accepted environment variable aliases:
- `ADSAPI_URL` and `ADSAPI_KEY`
- `ADS_API_URL` and `ADS_API_KEY`
- `CAMSAPI_URL` and `CAMSAPI_KEY`
- `CAMS_API_URL` and `CAMS_API_KEY`

Dataset page:
- {source_url}

What to do:
1. Create or sign in to an Atmosphere Data Store account.
2. Accept the dataset licence on the dataset page if prompted.
3. Configure ADS credentials in environment variables or a compatible API config file.
4. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="cams",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=CAMS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes="ADS API credentials were not detected.",
            ),
        ]

    if not request:
        instructions = f"""# Manual Steps For CAMS / ADS

No ADS API request payload was configured for dataset `{dataset_id}`.

Recommended request style:
```yaml
datasets:
  cams:
    request:
      date: ["2024-01-01/2024-01-31"]
      time: ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
      variable:
        - particulate_matter_2.5um
        - particulate_matter_10um
        - dust_aerosol_optical_depth_550nm
      format: netcdf
```

Dataset page:
- {source_url}
"""
        return [
            manual_record(
                dataset_name="cams",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=CAMS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes="No CAMS request payload was configured.",
            ),
        ]

    target_name = str(dataset_cfg.get("target_filename", _default_target_name(dataset_id, request)))
    target_path = ensure_directory(context.raw_root / "cams") / target_name

    if target_path.exists():
        ok, _ = validate_download(target_path)
        if ok:
            return [
                downloaded_record(
                    dataset_name="cams",
                    source_url=source_url,
                    local_path=target_path,
                    context=context,
                    license_or_access_note=CAMS_LICENSE_NOTE,
                    spatial_resolution_raw=spatial_resolution_raw,
                    temporal_resolution=temporal_resolution,
                    bbox=bbox,
                    notes="Reused an existing local CAMS file.",
                ),
            ]

    key, url = _prepare_ads_environment()
    try:
        import cdsapi

        client_kwargs = {"quiet": True, "progress": False}
        if key and url:
            client_kwargs["key"] = key
            client_kwargs["url"] = url
        client = cdsapi.Client(**client_kwargs)
        client.retrieve(dataset_id, request, str(target_path))
    except Exception as exc:  # pragma: no cover - runtime/auth/provider dependent
        instructions = f"""# Manual Steps For CAMS / ADS

The ADS API call did not complete successfully.

Dataset page:
- {source_url}

Configured dataset id:
- `{dataset_id}`

Configured request:
```python
{request}
```

What to check:
1. Your ADS credentials are valid.
2. You accepted the licence for the dataset.
3. The request fields match the selected CAMS dataset.
4. After fixing the issue, re-run the fetch command.
"""
        return [
            manual_record(
                dataset_name="cams",
                source_url=source_url,
                context=context,
                instruction_text=instructions,
                license_or_access_note=CAMS_LICENSE_NOTE,
                spatial_resolution_raw=spatial_resolution_raw,
                temporal_resolution=temporal_resolution,
                bbox=bbox,
                notes=f"ADS API retrieval did not complete successfully: {exc}",
            ),
        ]

    return [
        downloaded_record(
            dataset_name="cams",
            source_url=source_url,
            local_path=target_path,
            context=context,
            license_or_access_note=CAMS_LICENSE_NOTE,
            spatial_resolution_raw=spatial_resolution_raw,
            temporal_resolution=temporal_resolution,
            bbox=bbox,
            notes=join_notes(
                f"Retrieved `{dataset_id}` through the ADS API.",
                "Request parameters were taken from config/datasets.yaml.",
            ),
        ),
    ]
