"""Fetcher for ERA5 / ERA5-Land products via the CDS API."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

from src.data.catalog import CatalogRecord
from src.data.utils import (
    downloaded_record,
    ensure_directory,
    join_notes,
    log_reuse_progress,
    manual_record,
    set_progress_total,
    update_progress,
    validate_download,
)


ERA5_DEFAULT_DATASET = "reanalysis-era5-land-monthly-means"
ERA5_LICENSE_NOTE = "Copernicus Climate Data Store licence applies. Users usually need an account and dataset licence acceptance."
ERA5_DEFAULT_CDS_URL = "https://cds.climate.copernicus.eu/api"
ERA5_ARCO_DEFAULT_STORE = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
ERA5_ARCO_DOCS_URL = "https://github.com/google-research/arco-era5"
ERA5_ARCO_LICENSE_NOTE = "Copernicus/ECMWF ERA5 terms apply; data accessed through Google Cloud Public Datasets ARCO ERA5."
ERA5_ARCO_VARIABLE_ALIASES = {
    "2m_temperature": "t2m",
    "skin_temperature": "skt",
    "total_precipitation": "tp",
    "volumetric_soil_water_layer_1": "swvl1",
    "10m_u_component_of_wind": "u10",
    "10m_v_component_of_wind": "v10",
    "instantaneous_10m_wind_gust": "i10fg",
    "maximum_total_precipitation_rate_since_previous_post_processing": "mxtpr",
    "mean_total_precipitation_rate": "mtpr",
}
ERA5_ARCO_DEFAULT_VARIABLES = [
    "2m_temperature",
    "skin_temperature",
    "total_precipitation",
    "volumetric_soil_water_layer_1",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
]


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Retrieve ERA5-family products through the configured backend."""

    backend = str(dataset_cfg.get("backend", dataset_cfg.get("download_backend", "cds"))).strip().lower()
    if backend in {"arco", "arco_zarr", "zarr"}:
        return _fetch_arco_zarr(dataset_cfg, context)
    if backend not in {"", "cds", "cdsapi", "copernicus"}:
        raise ValueError(f"Unsupported ERA5 backend `{backend}`. Use `arco_zarr` or `cds`.")
    return _fetch_cds(dataset_cfg, context)


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
        if split_request_by not in {"daily", "weekly"}:
            raise ValueError(f"Unsupported ERA5 split_request_by `{split_request_by}`.")
        if not dataset_cfg.get("start_date") or not dataset_cfg.get("end_date"):
            raise ValueError(f"ERA5 {split_request_by} splitting requires `start_date` and `end_date`.")
        request_template = _build_request_from_config(dataset_cfg)
        if not request_template:
            raise ValueError(f"ERA5 {split_request_by} splitting requires a non-empty `request` template.")
        start = _parse_iso_date(dataset_cfg["start_date"])
        end = _parse_iso_date(dataset_cfg["end_date"])
        if end < start:
            raise ValueError(f"ERA5 `end_date` must not be earlier than `start_date`: {start}..{end}")
        default_step_days = 1 if split_request_by == "daily" else 7
        step_days = int(dataset_cfg.get("request_step_days", dataset_cfg.get("step_days", default_step_days)))
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


def _as_list(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _request_date_bounds(request: dict, dataset_cfg: dict) -> tuple[date, date]:
    years = _as_list(request.get("year"))
    months = _as_list(request.get("month"))
    days = _as_list(request.get("day"))
    parsed_dates: list[date] = []
    for year in years:
        for month in months or ["01"]:
            for day in days or ["01"]:
                try:
                    parsed_dates.append(date(int(year), int(month), int(day)))
                except ValueError:
                    continue
    if parsed_dates:
        return min(parsed_dates), max(parsed_dates)

    if dataset_cfg.get("start_date") and dataset_cfg.get("end_date"):
        return _parse_iso_date(dataset_cfg["start_date"]), _parse_iso_date(dataset_cfg["end_date"])
    raise ValueError("ARCO ERA5 requires request year/month/day or datasets.era5 start_date/end_date.")


def _request_variables(request: dict, dataset_cfg: dict) -> list[str]:
    variables = _as_list(request.get("variable")) or _as_list(dataset_cfg.get("variable")) or _as_list(dataset_cfg.get("variables"))
    return variables or list(ERA5_ARCO_DEFAULT_VARIABLES)


def _resolve_arco_variables(dataset, variables: list[str]) -> tuple[list[str], list[str]]:
    available: list[str] = []
    missing: list[str] = []
    for name in variables:
        if name in dataset and name not in available:
            available.append(name)
            continue
        alias = ERA5_ARCO_VARIABLE_ALIASES.get(name)
        if alias in dataset and alias not in available:
            available.append(alias)
            continue
        missing.append(name)
    return available, missing


def _requested_hours(request: dict) -> set[int] | None:
    raw_times = _as_list(request.get("time"))
    if not raw_times:
        return None
    hours: set[int] = set()
    for raw in raw_times:
        try:
            hours.add(int(str(raw).split(":", 1)[0]))
        except ValueError:
            continue
    return hours if hours and len(hours) < 24 else None


def _select_coord_range(dataset, coord: str, low: float, high: float):
    values = dataset[coord].values
    if len(values) == 0:
        return dataset
    if float(values[0]) <= float(values[-1]):
        return dataset.sel({coord: slice(low, high)})
    return dataset.sel({coord: slice(high, low)})


def _select_arco_bbox(dataset, bbox: list[float] | None):
    if not (isinstance(bbox, list) and len(bbox) == 4):
        return dataset

    west, south, east, north = [float(value) for value in bbox]
    subset = _select_coord_range(dataset, "latitude", south, north)
    lon_values = subset["longitude"].values
    if len(lon_values) == 0:
        return subset

    lon_min = float(lon_values.min())
    lon_max = float(lon_values.max())
    if lon_min >= 0.0 and lon_max > 180.0:
        west_norm = west % 360.0
        east_norm = east % 360.0
        if west_norm <= east_norm:
            subset = subset.sel(longitude=slice(west_norm, east_norm))
        else:
            left = subset.sel(longitude=slice(west_norm, 360.0))
            right = subset.sel(longitude=slice(0.0, east_norm))
            import xarray as xr

            subset = xr.concat([left, right], dim="longitude")
        subset = subset.assign_coords(longitude=(((subset["longitude"] + 180.0) % 360.0) - 180.0)).sortby("longitude")
        return subset

    return _select_coord_range(subset, "longitude", west, east)


def _normalize_arco_dataset(dataset):
    if "time" not in dataset.dims and "valid_time" in dataset.dims:
        return dataset.rename({"valid_time": "time"})
    return dataset


def _open_arco_store(store_url: str):
    if "data.earthdatahub.destine.eu" in store_url:
        storage_options = {"client_kwargs": {"trust_env": True}}
        token = os.getenv("DESTINATION_API_KEY") or os.getenv("EDH_API_KEY") or os.getenv("EARTHDATAHUB_API_KEY")
        if token:
            from aiohttp import BasicAuth

            storage_options["client_kwargs"]["auth"] = BasicAuth("edh", token)
    else:
        storage_options = {"token": "anon"}

    import xarray as xr

    return _normalize_arco_dataset(xr.open_zarr(store_url, chunks=None, storage_options=storage_options))


def _mark_arco_progress(context, *, reused: bool) -> None:
    dataset = (context.active_dataset or "era5").strip() or "era5"
    stats = context.reuse_stats.setdefault(dataset, {"total": 0, "reused": 0, "new": 0})
    stats["total"] += 1
    if reused:
        stats["reused"] += 1
    else:
        stats["new"] += 1
    update_progress(context)
    log_reuse_progress(context, dataset)


def _target_suffix_for_arco(target_name: str) -> str:
    suffix = Path(target_name).suffix.lower()
    return suffix if suffix in {".nc", ".zarr"} else ".nc"


def _write_arco_subset(subset, target_path: Path) -> None:
    ensure_directory(target_path.parent)
    if target_path.suffix.lower() == ".zarr":
        tmp_path = target_path.with_name(target_path.name + ".part")
        if tmp_path.exists():
            import shutil

            shutil.rmtree(tmp_path)
        subset.to_zarr(tmp_path, mode="w")
        if target_path.exists():
            import shutil

            shutil.rmtree(target_path)
        tmp_path.replace(target_path)
        return

    tmp_path = target_path.with_suffix(target_path.suffix + ".part")
    tmp_path.unlink(missing_ok=True)
    try:
        subset.to_netcdf(tmp_path, engine="h5netcdf")
    except ImportError:
        tmp_path.unlink(missing_ok=True)
        subset.to_netcdf(tmp_path, engine="netcdf4")
    tmp_path.replace(target_path)


def _fetch_arco_zarr(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Cut local ERA5 subsets from the public Google ARCO ERA5 Zarr store."""

    store_url = str(dataset_cfg.get("arco_store_url") or dataset_cfg.get("source_url") or ERA5_ARCO_DEFAULT_STORE)
    dataset_id = str(dataset_cfg.get("dataset_id", "arco-era5"))
    bbox = dataset_cfg.get("bbox")
    subrequests = _subrequests_from_config(dataset_cfg, dataset_id)
    if not subrequests:
        request = _build_request_from_config(dataset_cfg)
        if not request:
            request = {"variable": ERA5_ARCO_DEFAULT_VARIABLES}
        target_name = str(dataset_cfg.get("target_filename") or "era5-arco.nc")
        subrequests = [(target_name, request)]

    set_progress_total(context, len(subrequests))
    records: list[CatalogRecord] = []
    ds = _open_arco_store(store_url)
    try:
        valid_start = ds.attrs.get("valid_time_start")
        valid_stop = ds.attrs.get("valid_time_stop") or ds.attrs.get("valid_time_stop_era5t")
        if valid_start and valid_stop:
            ds = ds.sel(time=slice(valid_start, valid_stop))

        for idx, (target_name, request) in enumerate(subrequests, start=1):
            suffix = _target_suffix_for_arco(target_name)
            if Path(target_name).suffix.lower() != suffix:
                target_name = f"{Path(target_name).stem}{suffix}"
            target_path = ensure_directory(context.raw_root / "era5") / target_name
            if target_path.exists():
                ok, _ = validate_download(target_path) if target_path.is_file() else (True, "")
                if ok:
                    _mark_arco_progress(context, reused=True)
                    records.append(
                        downloaded_record(
                            dataset_name="era5",
                            source_url=store_url,
                            local_path=target_path,
                            context=context,
                            license_or_access_note=ERA5_ARCO_LICENSE_NOTE,
                            spatial_resolution_raw=str(dataset_cfg.get("arco_spatial_resolution_raw", "0.25 degree grid")),
                            temporal_resolution=str(dataset_cfg.get("temporal_resolution", "hourly")),
                            bbox=bbox,
                            notes="Reused an existing local ERA5 ARCO subset.",
                        ),
                    )
                    continue

            start, end = _request_date_bounds(request, dataset_cfg)
            variables = _request_variables(request, dataset_cfg)
            available, missing = _resolve_arco_variables(ds, variables)
            if not available:
                raise ValueError(f"None of the requested ERA5 ARCO variables exist in the store: {variables}")

            subset = ds[available].sel(time=slice(datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.max.time())))
            hours = _requested_hours(request)
            if hours is not None:
                subset = subset.sel(time=subset["time"].dt.hour.isin(sorted(hours)))
            subset = _select_arco_bbox(subset, bbox)
            rename = {name: ERA5_ARCO_VARIABLE_ALIASES[name] for name in subset.data_vars if name in ERA5_ARCO_VARIABLE_ALIASES}
            if rename:
                subset = subset.rename(rename)
            subset.attrs["source_store"] = store_url
            subset.attrs["source_backend"] = "arco_zarr"
            subset.attrs["requested_start_date"] = start.isoformat()
            subset.attrs["requested_end_date"] = end.isoformat()
            if missing:
                subset.attrs["missing_requested_variables"] = ",".join(missing)

            if context.logger:
                context.logger.info("[era5-arco] %s/%s writing %s vars=%s", idx, len(subrequests), target_name, ",".join(subset.data_vars))
            _write_arco_subset(subset, target_path)
            _mark_arco_progress(context, reused=False)
            records.append(
                downloaded_record(
                    dataset_name="era5",
                    source_url=store_url,
                    local_path=target_path,
                    context=context,
                    license_or_access_note=ERA5_ARCO_LICENSE_NOTE,
                    spatial_resolution_raw=str(dataset_cfg.get("arco_spatial_resolution_raw", "0.25 degree grid")),
                    temporal_resolution=str(dataset_cfg.get("temporal_resolution", "hourly")),
                    bbox=bbox,
                    notes=join_notes(
                        "Cut from Google Cloud Public Datasets ARCO ERA5 Zarr.",
                        f"ARCO docs: {ERA5_ARCO_DOCS_URL}",
                        f"Renamed variables to CDS short names where applicable: {rename}" if rename else "",
                        f"Missing requested variables: {missing}" if missing else "",
                    ),
                ),
            )
    finally:
        ds.close()

    return records


def _fetch_cds(dataset_cfg: dict, context) -> list[CatalogRecord]:
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
        total_requests = len(subrequests)
        set_progress_total(context, total_requests)
        reused_count = 0
        new_count = 0
        for request_idx, (target_name, request) in enumerate(subrequests, start=1):
            target_path = ensure_directory(context.raw_root / "era5") / target_name

            if target_path.exists():
                ok, _ = validate_download(target_path)
                if ok:
                    reused_count += 1
                    if context.logger:
                        pct = int(round(request_idx / total_requests * 100))
                        context.logger.info(
                            "[era5-progress] %s/%s (%s%%) file=%s status=reused reused=%s new=%s",
                            request_idx,
                            total_requests,
                            pct,
                            target_name,
                            reused_count,
                            new_count,
                        )
                    update_progress(context)
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

            if context.logger:
                pct_before = int(round((request_idx - 1) / total_requests * 100))
                context.logger.info(
                    "[era5-progress] %s/%s (%s%%) file=%s status=downloading reused=%s new=%s",
                    request_idx,
                    total_requests,
                    pct_before,
                    target_name,
                    reused_count,
                    new_count,
                )
            client.retrieve(dataset_id, request, str(target_path))
            new_count += 1
            if context.logger:
                pct = int(round(request_idx / total_requests * 100))
                context.logger.info(
                    "[era5-progress] %s/%s (%s%%) file=%s status=downloaded reused=%s new=%s",
                    request_idx,
                    total_requests,
                    pct,
                    target_name,
                    reused_count,
                    new_count,
                )
            update_progress(context)
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
