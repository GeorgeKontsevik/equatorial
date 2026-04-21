"""Inspect downloaded assets and enrich the metadata catalog with spatial details."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.data.catalog import load_catalog, write_catalog, write_inventory_report
from src.data.config import load_config
from src.data.utils import FetchContext, bbox_to_string, configure_logging, extract_bounds, inspect_spatial_file, relative_to_project


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect downloaded data and update the metadata catalog.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.yaml"), help="Path to the dataset configuration YAML file.")
    return parser.parse_args()


def build_context(project_root: Path, config: dict) -> FetchContext:
    global_cfg = config.get("global", {})
    data_root = project_root / global_cfg.get("data_root", "data")
    logger = configure_logging(data_root / "logs", logger_name="equatorial.data.inspect")
    return FetchContext(
        project_root=project_root,
        data_root=data_root,
        raw_root=data_root / "raw",
        metadata_root=data_root / "metadata",
        manual_steps_root=data_root / "metadata" / "manual_steps",
        logs_root=data_root / "logs",
        logger=logger,
    )


def main() -> None:
    args = parse_args()
    project_root = args.config.resolve().parents[1]
    config = load_config(args.config)
    context = build_context(project_root, config)

    catalog_csv = context.metadata_root / "catalog.csv"
    catalog_json = context.metadata_root / "catalog.json"
    report_path = project_root / "reports" / "data_inventory.md"

    catalog = load_catalog(catalog_csv)
    if catalog.empty:
        context.logger.info("Catalog is empty; nothing to inspect.")
        write_inventory_report(catalog, report_path)
        return

    updated = catalog.copy()
    for index, row in updated.iterrows():
        if row["status"] != "downloaded" or not row["local_path"]:
            continue

        local_path = project_root / row["local_path"]
        if not local_path.exists():
            updated.at[index, "notes"] = f"{row['notes']} Missing local path during inspection.".strip()
            continue

        try:
            details = inspect_spatial_file(local_path)
        except Exception as exc:  # pragma: no cover - backend/runtime dependent
            updated.at[index, "notes"] = f"{row['notes']} Inspection skipped: {exc}".strip()
            continue
        for key, value in details.items():
            if key in updated.columns:
                updated.at[index, key] = value

        if not updated.at[index, "bbox_if_known"]:
            bounds = extract_bounds(local_path)
            updated.at[index, "bbox_if_known"] = bbox_to_string(bounds)

        updated.at[index, "local_path"] = relative_to_project(local_path, project_root)

    write_catalog(updated, catalog_csv, catalog_json)
    write_inventory_report(updated, report_path)
    context.logger.info("Inspection complete for %s catalog records.", len(updated))


if __name__ == "__main__":
    main()
