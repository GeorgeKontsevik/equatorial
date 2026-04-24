"""Fetcher for ERA5 / ERA5-Land products via the CDS API."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
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


def _parse_iso_date(value: object) -> date:
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _week_windows(start: date, end: date, step_days: int) -> list[tuple[date, date]]:
    if step_days <= 0:
        raise ValueError("ERA5 `request_step_days` must be a positive integer.")
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=step_days - 1))
        windows.append((cursor, window_end))
        cursor += timedelta(days=step_days)
    return windows


def _request_for_window(template: dict, start: date, end: date) -> dict:
    request = dict(template)
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    request["year"] = sorted({f"{day.year:04d}" for day in days})
    request["month"] = sorted({f"{day.month:02d}" for day in days})
    request["day"] = [f"{day.day:02d}" for day in days]
    return request


def _subrequests_from_config(dataset_cfg: dict, dataset_id: str) -> list[tuple[str, dict]]:
    split_request_by = str(dataset_cfg.get("split_request_by", "")).strip().lower()
    if split_request_by:
        if split_request_by != "weekly":
            raise ValueError(f"Unsupported ERA5 split_request_by `{split_request_by}`.")
        if not dataset_cfg.get("start_date") or not dataset_cfg.get("end_date"):
            raise ValueError("ERA5 weekly splitting requires `start_date` and `end_date`.")
        request_template = _build_request_from_config(dataset_cfg)
        if not request_template:
            raise ValueError("ERA5 weekly splitting requires a non-empty `request` template.")
        start = _parse_iso_date(dataset_cfg["start_date"])
        end = _parse_iso_date(dataset_cfg["end_date"])
        if end < start:
            raise ValueError(f"ERA5 `end_date` must not be earlier than `start_date`: {start}..{end}")
        step_days = int(dataset_cfg.get("request_step_days", dataset_cfg.get("step_days", 7)))
        target_prefix = str(dataset_cfg.get("target_prefix", dataset_id)).strip() or dataset_id
        suffix = Path(_default_target_name(dataset_id, request_template)).suffix
        return [
            (f"{target_prefix}-{window_start.isoformat()}{suffix}", _request_for_window(request_template, window_start, window_end))
            for window_start, window_end in _week_windows(start, end, step_days)
        ]

    raw = dataset_cfg.get("requests")
    if isinstance(raw, list) and raw:
        out: list[tuple[str, dict]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"ERA5 requests[{idx}] must be a mapping.")
            request = dict(item.get("request") or {})
            if not request:
                raise ValueError(f"ERA5 requests[{idx}] is missing a non-empty `request` mapping.")
            target_name = str(item.get("target_filename") or _default_target_name(dataset_id, request))
            out.append((target_name, request))
        return out

    request = _build_request_from_config(dataset_cfg)
    if not request:
        return []
    target_name = str(dataset_cfg.get("target_filename", _default_target_name(dataset_id, request)))
    return [(target_name, request)]


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Retrieve an ERA5-family product through `cdsapi` when credentials are available."""

    dataset_id = str(dataset_cfg.get("dataset_id", ERA5_DEFAULT_DATASET))
    source_url = str(dataset_cfg.get("source_url", f"https://cds.climate.copernicus.eu/datasets/{dataset_id}"))
    spatial_resolution_raw = str(dataset_cfg.get("spatial_resolution_raw", "See CDS dataset metadata"))
    temporal_resolution = str(dataset_cfg.get("temporal_resolution", "See CDS dataset metadata"))
    bbox = dataset_cfg.get("bbox")
    subrequests = _subrequests_from_config(dataset_cfg, dataset_id)

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

    if not subrequests:
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

    try:
        import cdsapi

        client = cdsapi.Client(quiet=True, progress=False)
        records: list[CatalogRecord] = []
        for target_name, request in subrequests:
            target_path = ensure_directory(context.raw_root / "era5") / target_name

            if target_path.exists():
                ok, _ = validate_download(target_path)
                if ok:
                    records.append(
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
                    )
                    continue

            client.retrieve(dataset_id, request, str(target_path))
            records.append(
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
            )
    except Exception as exc:  # pragma: no cover - runtime/auth/provider dependent
        request_preview = subrequests[0][1] if subrequests else {}
        instructions = f"""# Manual Steps For ERA5 / ERA5-Land

The CDS API call did not complete successfully.

Dataset page:
- {source_url}

Configured dataset id:
- `{dataset_id}`

Configured request:
```python
{request_preview}
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

    return records
