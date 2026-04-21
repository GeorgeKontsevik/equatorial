"""CLI entrypoint for reproducible raw-data acquisition."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.data.catalog import CatalogRecord, load_catalog, summarize_status, upsert_records, write_catalog, write_inventory_report
from src.data.config import load_config
from src.data.fetchers import FETCHER_REGISTRY
from src.data.utils import FetchContext, bbox_to_string, configure_logging, ensure_directory, utc_now_iso


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch configured datasets into the local data lake.")
    parser.add_argument("--config", type=Path, required=True, help="Path to the dataset configuration YAML file.")
    parser.add_argument("--country-code", type=str, default="", help="Optional ISO3 override for country-wise dataset caching such as GADM and road_surface.")
    parser.add_argument("--datasets", type=str, default="", help="Optional comma-separated subset of dataset keys to fetch, for example `gadm,road_surface`.")
    return parser.parse_args()


def build_context(project_root: Path, config: dict) -> FetchContext:
    global_cfg = config.get("global", {})
    data_root = ensure_directory(project_root / global_cfg.get("data_root", "data"))
    metadata_root = ensure_directory(data_root / "metadata")
    logs_root = ensure_directory(data_root / "logs")
    logger = configure_logging(logs_root)
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
    config = load_config(args.config, country_code_override=args.country_code)
    project_root = args.config.resolve().parents[1]
    context = build_context(project_root, config)

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
    for dataset_name, dataset_cfg in dataset_cfgs.items():
        if selected_datasets and dataset_name not in selected_datasets:
            continue
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
            continue

        if not bool(dataset_cfg.get("enabled", True)):
            context.logger.info("Skipping disabled dataset: %s", dataset_name)
            new_records.append(skipped_record(dataset_name, dataset_cfg))
            continue

        try:
            context.logger.info("Fetching dataset: %s", dataset_name)
            records = fetcher(dataset_cfg or {}, context)
            new_records.extend(records)
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

    merged = upsert_records(catalog, new_records)
    write_catalog(merged, catalog_csv, catalog_json)
    write_inventory_report(merged, report_path)

    summary = summarize_status(merged)
    print("Downloaded datasets:")
    for name in summary.get("downloaded", []):
        print(f"  - {name}")
    print("Manual datasets:")
    for name in summary.get("manual", []):
        print(f"  - {name}")
    print("Failed datasets:")
    for name in summary.get("failed", []):
        print(f"  - {name}")


if __name__ == "__main__":
    main()
