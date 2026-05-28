"""CLI entrypoint for reproducible raw-data acquisition."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.data.catalog import CatalogRecord, load_catalog, summarize_status, upsert_records, write_catalog, write_inventory_report
from src.data.config import load_config
from src.data.fetchers import FETCHER_REGISTRY
from src.data.utils import (
    FetchContext,
    bbox_to_string,
    configure_logging,
    ensure_directory,
    load_project_env,
    log_reuse_progress,
    set_progress_total,
    utc_now_iso,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch configured datasets into the local data lake.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the dataset configuration YAML file.")
    parser.add_argument("--country-code", type=str, default="", help="Optional ISO3 override for country-wise dataset caching such as GADM and road_surface.")
    parser.add_argument("--datasets", type=str, default="", help="Optional comma-separated subset of dataset keys to fetch, for example `gadm,road_surface`.")
    parser.add_argument("--progress-bar", action="store_true", help="Render tqdm progress bars instead of verbose INFO logs on stderr.")
    return parser.parse_args()


def build_context(project_root: Path, config: dict, *, progress_bar: bool = False) -> FetchContext:
    global_cfg = config.get("global", {})
    data_root = ensure_directory(project_root / global_cfg.get("data_root", "data"))
    metadata_root = ensure_directory(data_root / "metadata")
    logs_root = ensure_directory(data_root / "logs")
    logger = configure_logging(logs_root, console_level=logging.WARNING if progress_bar else logging.INFO)
    return FetchContext(
        project_root=project_root,
        data_root=data_root,
        raw_root=ensure_directory(data_root / "raw"),
        metadata_root=metadata_root,
        manual_steps_root=ensure_directory(metadata_root / "manual_steps"),
        logs_root=logs_root,
        timeout_seconds=float(global_cfg.get("timeout_seconds", 120)),
        max_retries=int(global_cfg.get("max_retries", 3)),
        user_agent=str(global_cfg.get("user_agent", "equatorial-data-fetch/0.1.0")),
        logger=logger,
    )


def _parse_date(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d")


def _count_days(start_date: object, end_date: object) -> int | None:
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start is None or end is None or end < start:
        return None
    return (end - start).days + 1


def _expected_fetch_items(dataset_name: str, dataset_cfg: dict) -> int | None:
    if dataset_name == "chirps":
        frequency = str(dataset_cfg.get("frequency", dataset_cfg.get("temporal_resolution", "monthly"))).lower()
        if frequency == "daily":
            return _count_days(dataset_cfg.get("start_date"), dataset_cfg.get("end_date"))
        years = dataset_cfg.get("years") or []
        months = dataset_cfg.get("months") or []
        return len(years) * len(months) if years and months else None
    if dataset_name == "era5":
        requests = dataset_cfg.get("requests")
        if isinstance(requests, list) and requests:
            return len(requests)
        split_request_by = str(dataset_cfg.get("split_request_by", "")).strip().lower()
        n_days = _count_days(dataset_cfg.get("start_date"), dataset_cfg.get("end_date"))
        if n_days is None:
            return 1 if dataset_cfg.get("request") else None
        if split_request_by == "daily":
            return n_days
        if split_request_by == "weekly":
            return (n_days + 6) // 7
    if dataset_name in {"gadm", "road_surface"}:
        country_codes = dataset_cfg.get("country_codes") or []
        return len(country_codes) if country_codes else None
    return None


def _project_root_from_config(config_path: Path) -> Path:
    """Find the repository root for configs stored in nested generated dirs."""

    resolved = config_path.resolve()
    for parent in [resolved.parent, *resolved.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "src").exists():
            return parent
    return resolved.parents[1]


def skipped_record(dataset_name: str, dataset_cfg: dict) -> CatalogRecord:
    return CatalogRecord(
        dataset_name=dataset_name,
        source_url=str(dataset_cfg.get("source_url", "")),
        download_date_utc=utc_now_iso(),
        local_path="",
        file_format="",
        license_or_access_note=str(dataset_cfg.get("license_or_access_note", "")),
        spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "")),
        temporal_resolution=str(dataset_cfg.get("temporal_resolution", "")),
        bbox_if_known=bbox_to_string(dataset_cfg.get("bbox")),
        checksum_sha256="",
        status="skipped",
        notes="Dataset disabled in config.",
    )


def main() -> None:
    args = parse_args()
    project_root = _project_root_from_config(args.config)
    load_project_env(project_root)
    config = load_config(args.config, country_code_override=args.country_code)
    context = build_context(project_root, config, progress_bar=bool(args.progress_bar))
    tqdm = None
    if args.progress_bar:
        from tqdm.auto import tqdm as tqdm_factory

        tqdm = tqdm_factory

    catalog_csv = context.metadata_root / "catalog.csv"
    catalog_json = context.metadata_root / "catalog.json"
    report_path = project_root / "reports" / "data_inventory.md"

    catalog = load_catalog(catalog_csv)
    new_records: list[CatalogRecord] = []

    dataset_cfgs = config.get("datasets", {})
    selected_datasets = {
        value.strip()
        for value in str(args.datasets).split(",")
        if value.strip()
    }
    work_items = [
        (dataset_name, dataset_cfg)
        for dataset_name, dataset_cfg in dataset_cfgs.items()
        if not selected_datasets or dataset_name in selected_datasets
    ]
    total = len(work_items)
    if total == 0:
        print("No datasets selected for fetch.")
        return

    dataset_iter = work_items
    dataset_bar = None
    if tqdm is not None:
        dataset_bar = tqdm(total=total, desc="datasets", unit="dataset", dynamic_ncols=True, position=0)

    for idx, (dataset_name, dataset_cfg) in enumerate(dataset_iter, start=1):
        pct = int(round((idx - 1) / total * 100))
        context.logger.info("[fetch-progress] %s/%s (%s%%) starting dataset=%s", idx, total, pct, dataset_name)
        item_bar = None
        if tqdm is not None:
            expected = _expected_fetch_items(dataset_name, dataset_cfg or {})
            item_bar = tqdm(total=expected, desc=dataset_name, unit="file", dynamic_ncols=True, position=1, leave=False)
            context.progress_bar = item_bar
            set_progress_total(context, expected)
        fetcher = FETCHER_REGISTRY.get(dataset_name)
        if fetcher is None:
            context.logger.warning("No fetcher registered for %s; skipping.", dataset_name)
            new_records.append(
                CatalogRecord(
                    dataset_name=dataset_name,
                    source_url="",
                    download_date_utc=utc_now_iso(),
                    local_path="",
                    file_format="",
                    license_or_access_note="",
                    spatial_resolution_raw="",
                    temporal_resolution="",
                    bbox_if_known=bbox_to_string(dataset_cfg.get("bbox")),
                    checksum_sha256="",
                    status="skipped",
                    notes="No fetcher implemented for this dataset key.",
                ),
            )
            context.logger.info("[fetch-progress] %s/%s (%s%%) finished dataset=%s status=skipped", idx, total, int(round(idx / total * 100)), dataset_name)
            if item_bar is not None:
                item_bar.close()
                context.progress_bar = None
            if dataset_bar is not None:
                dataset_bar.update(1)
            continue

        if not bool(dataset_cfg.get("enabled", True)):
            context.logger.info("Skipping disabled dataset: %s", dataset_name)
            new_records.append(skipped_record(dataset_name, dataset_cfg))
            context.logger.info("[fetch-progress] %s/%s (%s%%) finished dataset=%s status=skipped", idx, total, int(round(idx / total * 100)), dataset_name)
            if item_bar is not None:
                item_bar.close()
                context.progress_bar = None
            if dataset_bar is not None:
                dataset_bar.update(1)
            continue

        try:
            context.logger.info("Fetching dataset: %s", dataset_name)
            context.active_dataset = dataset_name
            records = fetcher(dataset_cfg or {}, context)
            new_records.extend(records)
            status_counts: dict[str, int] = {}
            for rec in records:
                status_counts[rec.status] = status_counts.get(rec.status, 0) + 1
            context.logger.info(
                "[fetch-progress] %s/%s (%s%%) finished dataset=%s status_counts=%s",
                idx,
                total,
                int(round(idx / total * 100)),
                dataset_name,
                status_counts,
            )
            log_reuse_progress(context, dataset_name, force=True)
        except Exception as exc:  # pragma: no cover - runtime network/provider failures
            context.logger.exception("Fetcher failed for %s", dataset_name)
            new_records.append(
                CatalogRecord(
                    dataset_name=dataset_name,
                    source_url=str(dataset_cfg.get("source_url", "")),
                    download_date_utc=utc_now_iso(),
                    local_path="",
                    file_format="",
                    license_or_access_note=str(dataset_cfg.get("license_or_access_note", "")),
                    spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "")),
                    temporal_resolution=str(dataset_cfg.get("temporal_resolution", "")),
                    bbox_if_known=bbox_to_string(dataset_cfg.get("bbox")),
                    checksum_sha256="",
                    status="failed",
                    notes=str(exc),
                ),
            )
            context.logger.info("[fetch-progress] %s/%s (%s%%) finished dataset=%s status=failed", idx, total, int(round(idx / total * 100)), dataset_name)
        finally:
            context.active_dataset = ""
            if item_bar is not None:
                item_bar.close()
                context.progress_bar = None
            if dataset_bar is not None:
                dataset_bar.update(1)

    if dataset_bar is not None:
        dataset_bar.close()

    merged = upsert_records(catalog, new_records)
    write_catalog(merged, catalog_csv, catalog_json)
    write_inventory_report(merged, report_path)

    current_summary = summarize_status(pd.DataFrame([record.to_dict() for record in new_records]))
    print("Downloaded datasets:")
    for name in current_summary.get("downloaded", []):
        print(f"  - {name}")
    print("Manual datasets:")
    for name in current_summary.get("manual", []):
        print(f"  - {name}")
    print("Failed datasets:")
    for name in current_summary.get("failed", []):
        print(f"  - {name}")


if __name__ == "__main__":
    main()
