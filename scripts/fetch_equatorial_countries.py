#!/usr/bin/env python3
"""Fetch configured equatorial country datasets without event-case naming."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch country-level equatorial datasets.")
    parser.add_argument("--config-dir", type=Path, default=Path("config/generated/full_year_2024_20260430_002106"))
    parser.add_argument("--config-glob", type=str, default="*_datasets_2024_full_year.yaml")
    parser.add_argument("--countries", type=str, default="all", help="Comma-separated ISO3 list, or all.")
    parser.add_argument("--exclude-countries", type=str, default="", help="Comma-separated ISO3 list to skip.")
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/country_fetch_runs"))
    parser.add_argument("--datasets", type=str, default="gadm,road_surface,chirps,era5,flood,visibility_noaa_isd")
    parser.add_argument("--start-date", type=str, required=True)
    parser.add_argument("--end-date", type=str, required=True)
    parser.add_argument("--era5-backend", choices=["arco_zarr", "cds"], default="arco_zarr")
    parser.add_argument("--era5-request-mode", choices=["daily", "weekly", "monthly"], default="monthly")
    parser.add_argument("--max-parallel-countries", type=int, default=1)
    parser.add_argument("--progress-bar", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _month_tokens(start_date: str, end_date: str) -> set[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    tokens: set[str] = set()
    year = start.year
    month = start.month
    while (year, month) <= (end.year, end.month):
        tokens.add(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return tokens


def _load_country_configs(config_dir: Path, config_glob: str) -> list[dict[str, object]]:
    countries: list[dict[str, object]] = []
    for path in sorted(config_dir.glob(config_glob)):
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        study_area = config.get("study_area")
        if not isinstance(study_area, dict):
            continue
        iso3 = str(study_area.get("country_code", "")).strip().upper()
        bbox = study_area.get("bbox")
        if not iso3 or not (isinstance(bbox, list) and len(bbox) == 4):
            continue
        slug = str(study_area.get("slug", "")).strip() or iso3.lower()
        country_name = str(study_area.get("country_name", iso3)).strip() or iso3
        bbox_area = abs((float(bbox[2]) - float(bbox[0])) * (float(bbox[3]) - float(bbox[1])))
        countries.append(
            {
                "iso3": iso3,
                "slug": slug,
                "country_name": country_name,
                "bbox": [float(value) for value in bbox],
                "bbox_area": bbox_area,
                "config_path": path,
            }
        )
    return sorted(countries, key=lambda row: (float(row["bbox_area"]), str(row["iso3"])))


def _selected_countries(countries: list[dict[str, object]], raw: str, exclude_raw: str) -> list[dict[str, object]]:
    exclude = {value.strip().upper() for value in exclude_raw.split(",") if value.strip()}
    if raw.strip().lower() == "all":
        selected = countries
    else:
        wanted = {value.strip().upper() for value in raw.split(",") if value.strip()}
        known = {str(row["iso3"]) for row in countries}
        unknown = sorted(wanted.difference(known))
        if unknown:
            raise ValueError(f"Unknown country code(s): {', '.join(unknown)}")
        selected = [row for row in countries if str(row["iso3"]) in wanted]
    return [row for row in selected if str(row["iso3"]) not in exclude]


def _apply_bbox(config: dict, bbox: list[float]) -> None:
    datasets = config.setdefault("datasets", {})
    for dataset_cfg in datasets.values():
        if isinstance(dataset_cfg, dict):
            dataset_cfg["bbox"] = bbox


def _configure_era5(config: dict, *, start_date: str, end_date: str, mode: str, backend: str) -> None:
    datasets = config.setdefault("datasets", {})
    era5 = datasets.get("era5")
    if not isinstance(era5, dict):
        return
    era5["enabled"] = True
    era5["backend"] = backend
    era5["start_date"] = start_date
    era5["end_date"] = end_date
    if backend == "arco_zarr":
        era5["dataset_id"] = "arco-era5"
        era5["source_url"] = "gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3"
        era5["arco_store_url"] = era5["source_url"]
        era5["spatial_resolution_raw"] = "0.25 degree grid"

    if mode == "monthly":
        months = _month_tokens(start_date, end_date)
        if isinstance(era5.get("source_files"), list):
            era5["source_files"] = [name for name in era5["source_files"] if any(token in str(name) for token in months)]
        if isinstance(era5.get("requests"), list):
            era5["requests"] = [item for item in era5["requests"] if any(token in str(item.get("target_filename", "")) for token in months)]
        return

    request_template = era5.get("request_defaults")
    if not isinstance(request_template, dict) or not request_template:
        raise ValueError("Non-monthly ERA5 request mode requires datasets.era5.request_defaults.")
    bbox = config.get("study_area", {}).get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4):
        raise ValueError("Non-monthly ERA5 request mode requires study_area.bbox.")
    request = dict(request_template)
    request["area"] = [float(bbox[3]), float(bbox[0]), float(bbox[1]), float(bbox[2])]
    slug = str(config.get("study_area", {}).get("slug", "country")).strip() or "country"
    era5["request"] = request
    era5["split_request_by"] = mode
    era5["request_step_days"] = 1 if mode == "daily" else 7
    era5["target_prefix"] = f"era5-land-hourly-{slug}-{mode}"
    era5["source_files"] = []
    era5["requests"] = []
    era5.pop("target_filename", None)


def _country_config(base_path: Path, *, start_date: str, end_date: str, era5_request_mode: str, era5_backend: str) -> dict:
    config = deepcopy(yaml.safe_load(base_path.read_text(encoding="utf-8")) or {})
    study_area = config.get("study_area")
    if not isinstance(study_area, dict):
        raise ValueError(f"Config has no study_area: {base_path}")
    bbox = study_area.get("bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4):
        raise ValueError(f"Config has no valid study_area.bbox: {base_path}")
    bbox = [float(value) for value in bbox]
    study_area["bbox"] = bbox
    _apply_bbox(config, bbox)

    datasets = config.setdefault("datasets", {})
    for key in ["chirps", "flood", "visibility_noaa_isd"]:
        dataset_cfg = datasets.get(key)
        if isinstance(dataset_cfg, dict):
            dataset_cfg["enabled"] = True
            dataset_cfg["start_date"] = start_date
            dataset_cfg["end_date"] = end_date
            if key == "flood":
                dataset_cfg["aggregation_period_days"] = 7
    _configure_era5(config, start_date=start_date, end_date=end_date, mode=era5_request_mode, backend=era5_backend)
    return config


def _run_fetch(
    *,
    py: str,
    project_root: Path,
    cfg: Path,
    iso3: str,
    datasets: str,
    progress_bar: bool,
    dry_run: bool,
) -> dict[str, object]:
    cmd = [py, "-m", "src.data.fetch", "--config", str(cfg), "--country-code", iso3, "--datasets", datasets]
    if progress_bar:
        cmd.append("--progress-bar")
    if dry_run:
        return {"command": cmd, "returncode": 0, "elapsed_seconds": 0.0}
    start = time.time()
    rc = subprocess.run(cmd, cwd=project_root, check=False).returncode
    return {"command": cmd, "returncode": int(rc), "elapsed_seconds": round(time.time() - start, 2)}


def _run_country(country: dict[str, object], *, run_root: Path, project_root: Path, py: str, args: argparse.Namespace) -> dict[str, object]:
    iso3 = str(country["iso3"])
    slug = str(country["slug"])
    country_root = run_root / f"{iso3.lower()}_{slug}"
    config_dir = country_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "datasets_country.yaml"
    config = _country_config(
        Path(country["config_path"]),
        start_date=args.start_date,
        end_date=args.end_date,
        era5_request_mode=args.era5_request_mode,
        era5_backend=args.era5_backend,
    )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    print(f"[country-fetch] country={iso3} name={country['country_name']} period={args.start_date}..{args.end_date} bbox={country['bbox']}", flush=True)
    stage = _run_fetch(
        py=py,
        project_root=project_root,
        cfg=config_path,
        iso3=iso3,
        datasets=args.datasets,
        progress_bar=bool(args.progress_bar),
        dry_run=bool(args.dry_run),
    )
    report = {
        "iso3": iso3,
        "slug": slug,
        "country_name": country["country_name"],
        "bbox": country["bbox"],
        "start_date": args.start_date,
        "end_date": args.end_date,
        "datasets": args.datasets,
        "failed": stage["returncode"] != 0,
        "stage": stage,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    (country_root / "country_fetch_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    config_dir = args.config_dir if args.config_dir.is_absolute() else project_root / args.config_dir
    countries = _selected_countries(_load_country_configs(config_dir, args.config_glob), args.countries, args.exclude_countries)
    if not countries:
        raise ValueError("No countries selected.")

    run_id = args.run_id.strip() or f"equatorial_countries_fetch_{datetime.now():%Y%m%d_%H%M%S}"
    run_root = (args.output_root if args.output_root.is_absolute() else project_root / args.output_root) / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    batch: dict[str, object] = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "countries": [],
        "datasets": args.datasets,
        "era5_backend": args.era5_backend,
        "era5_request_mode": args.era5_request_mode,
    }
    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, int(args.max_parallel_countries))) as pool:
        futures = {pool.submit(_run_country, country, run_root=run_root, project_root=project_root, py=py, args=args): str(country["iso3"]) for country in countries}
        for fut in as_completed(futures):
            iso3 = futures[fut]
            try:
                report = fut.result()
            except Exception as exc:
                report = {"iso3": iso3, "failed": True, "error": str(exc), "finished_at": datetime.now().isoformat(timespec="seconds")}
            if report.get("failed"):
                failures += 1
            batch["countries"].append(report)
            print(f"[country-fetch] done country={iso3} failed={bool(report.get('failed'))}", flush=True)
            if failures and not args.continue_on_error:
                break
    batch["finished_at"] = datetime.now().isoformat(timespec="seconds")
    batch["failed_countries"] = failures
    (run_root / "batch_country_fetch_manifest.json").write_text(json.dumps(batch, indent=2), encoding="utf-8")
    print(json.dumps({"run_root": str(run_root), "countries": len(batch["countries"]), "failed_countries": failures}, indent=2), flush=True)


if __name__ == "__main__":
    main()
