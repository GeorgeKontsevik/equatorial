"""Run full road-hazard data and analysis pipelines for named event cases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class EventCase:
    slug: str
    country_code: str
    country_name: str
    event_date: str
    start_date: str
    end_date: str
    region: str
    corridor: str
    hazard: str
    base_config: str


CASES: dict[str, EventCase] = {
    "kenya_nairobi_kajiado_narok_2024_05": EventCase(
        slug="kenya_nairobi_kajiado_narok_2024_05",
        country_code="KEN",
        country_name="Kenya",
        event_date="2024-05-01",
        start_date="2024-04-29",
        end_date="2024-05-19",
        region="Nairobi / Kajiado-Narok corridor",
        corridor="highway access into Nairobi + at least 3 other roads",
        hazard="floods",
        base_config="config/generated/full_year_2024_20260430_002106/equator_500km_full_year_20260430_002106_KEN_datasets_2024_full_year.yaml",
    ),
    "uganda_muyembe_nakapiripirit_2024_04": EventCase(
        slug="uganda_muyembe_nakapiripirit_2024_04",
        country_code="UGA",
        country_name="Uganda",
        event_date="2024-04-30",
        start_date="2024-04-29",
        end_date="2024-05-19",
        region="Eastern Uganda",
        corridor="Muyembe-Nakapiripirit Road, Chepsukunya Bridge",
        hazard="flash floods",
        base_config="config/generated/full_year_2024_20260430_002106/equator_500km_full_year_20260430_002106_UGA_datasets_2024_full_year.yaml",
    ),
    "ecuador_banos_tungurahua_2024_06": EventCase(
        slug="ecuador_banos_tungurahua_2024_06",
        country_code="ECU",
        country_name="Ecuador",
        event_date="2024-06-16",
        start_date="2024-06-10",
        end_date="2024-06-30",
        region="Tungurahua / Banos",
        corridor="Banos road corridor; highlands-Amazon connection",
        hazard="heavy rain / landslide",
        base_config="config/generated/full_year_2024_20260430_002106/equator_500km_full_year_20260430_002106_ECU_datasets_2024_full_year.yaml",
    ),
    "indonesia_west_sumatra_2024_05": EventCase(
        slug="indonesia_west_sumatra_2024_05",
        country_code="IDN",
        country_name="Indonesia",
        event_date="2024-05-11",
        start_date="2024-05-06",
        end_date="2024-05-26",
        region="West Sumatra / Padang Pariaman area",
        corridor="local road access in affected districts",
        hazard="floods / landslides / lahar",
        base_config="config/generated/full_year_2024_20260430_002106/equator_500km_full_year_20260430_002106_IDN_datasets_2024_full_year.yaml",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run road-hazard event case data and analysis batches.")
    parser.add_argument("--cases", type=str, default="all", help="Comma-separated case slugs, or `all`.")
    parser.add_argument("--run-id", type=str, default="", help="Optional run id. Defaults to timestamped id.")
    parser.add_argument("--output-root", type=Path, default=Path("outputs/event_case_runs"))
    parser.add_argument("--thresholds-yaml", type=Path, default=Path("config/road_hazard_thresholds_24h_road_failure_50_75_100_mar_may.yaml"))
    parser.add_argument("--fetch-datasets", type=str, default="gadm,road_surface,chirps,era5,flood,visibility_noaa_isd")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-factor-boxplots", action="store_true")
    parser.add_argument("--road-geometry-mode", choices=("line", "probe_point"), default="probe_point")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--point-batch-size", type=int, default=250000)
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--candidate-top-n", type=int, default=100)
    parser.add_argument("--top-n-per-crop", type=int, default=3)
    parser.add_argument("--spam-dir", type=Path, default=Path("spam_tifs"))
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    parser.add_argument("--min-component-nodes", type=int, default=500)
    parser.add_argument("--isolation-minutes", type=float, default=100000.0)
    parser.add_argument("--continue-on-error", action="store_true")
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _relpath(path: Path, project_root: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _month_tokens(start_date: str, end_date: str) -> set[str]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    tokens: set[str] = set()
    year = start.year
    month = start.month
    while (year, month) <= (end.year, end.month):
        tokens.add(f"{year:04d}-{month:02d}")
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tokens


def _event_config(base_config: Path, case: EventCase) -> dict[str, object]:
    config = yaml.safe_load(base_config.read_text(encoding="utf-8")) or {}
    config = deepcopy(config)
    config["event_case"] = asdict(case)
    datasets = config.setdefault("datasets", {})

    for key in ["chirps", "flood", "visibility_noaa_isd"]:
        cfg = datasets.get(key)
        if isinstance(cfg, dict):
            cfg["enabled"] = True
            cfg["start_date"] = case.start_date
            cfg["end_date"] = case.end_date
            if key == "flood":
                cfg["aggregation_period_days"] = 7

    era5 = datasets.get("era5")
    if isinstance(era5, dict):
        era5["enabled"] = True
        era5["start_date"] = case.start_date
        era5["end_date"] = case.end_date
        months = _month_tokens(case.start_date, case.end_date)
        if isinstance(era5.get("source_files"), list):
            era5["source_files"] = [name for name in era5["source_files"] if any(token in str(name) for token in months)]
        if isinstance(era5.get("requests"), list):
            era5["requests"] = [
                item
                for item in era5["requests"]
                if any(token in str(item.get("target_filename", "")) for token in months)
            ]
    return config


def _damage_config(case: EventCase) -> dict[str, object]:
    return {
        "road_climate_damage": {
            "analysis_period": {
                "start_date": case.start_date,
                "end_date": case.end_date,
                "aggregation_period_days": 7,
            }
        }
    }


def _run_stage(stage: str, command: list[str], cwd: Path) -> dict[str, object]:
    start = time.time()
    print(f"[case-batch] stage={stage} start", flush=True)
    result = subprocess.run(command, cwd=cwd, check=False)
    elapsed = round(time.time() - start, 2)
    print(f"[case-batch] stage={stage} done rc={result.returncode} elapsed_s={elapsed}", flush=True)
    return {
        "stage": stage,
        "command": command,
        "returncode": int(result.returncode),
        "elapsed_seconds": elapsed,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def _case_commands(args: argparse.Namespace, project_root: Path, case_root: Path, case: EventCase, config_path: Path, damage_path: Path) -> list[tuple[str, list[str]]]:
    py = sys.executable
    overlay_dir = case_root / "overlay"
    candidates_dir = case_root / "crop_candidates"
    connected_dir = case_root / "crop_connected"
    access_dir = case_root / "access"
    factor_dir = case_root / "factor_boxplots"
    candidate_gpkg = candidates_dir / f"spam_crop_top{args.candidate_top_n}_origins.gpkg"
    origins_gpkg = connected_dir / f"spam_crop_top{args.top_n_per_crop}_baseline_connected_origins.gpkg"

    commands: list[tuple[str, list[str]]] = []
    if not args.skip_fetch:
        commands.append(
            (
                "fetch",
                [
                    py,
                    "-m",
                    "src.data.fetch",
                    "--config",
                    str(config_path),
                    "--country-code",
                    case.country_code,
                    "--datasets",
                    args.fetch_datasets,
                ],
            )
        )
    commands.append(
        (
            "overlay",
            [
                py,
                "-m",
                "src.data.run_multisource_road_overlay",
                "--config",
                str(config_path),
                "--country-code",
                case.country_code,
                "--damage-config",
                str(damage_path),
                "--road-geometry-mode",
                args.road_geometry_mode,
                "--max-workers",
                str(args.max_workers),
                "--point-batch-size",
                str(args.point_batch_size),
                "--output-root",
                str(overlay_dir),
            ],
        )
    )
    if not args.skip_factor_boxplots:
        commands.append(
            (
                "factor_boxplots",
                [
                    py,
                    "-m",
                    "src.data.run_weekly_factor_boxplots_streaming",
                    "--config",
                    str(config_path),
                    "--country-code",
                    case.country_code,
                    "--damage-config",
                    str(damage_path),
                    "--thresholds-yaml",
                    str(args.thresholds_yaml),
                    "--output-root",
                    str(factor_dir),
                ],
            )
        )
    commands.extend(
        [
            (
                "build_crop_origin_candidates",
                [
                    py,
                    "-m",
                    "src.data.build_spam_crop_top_origins",
                    "--country-code",
                    case.country_code,
                    "--top-n",
                    str(args.candidate_top_n),
                    "--spam-dir",
                    str(args.spam_dir),
                    "--output-dir",
                    str(candidates_dir),
                ],
            ),
            (
                "select_baseline_connected_crop_origins",
                [
                    py,
                    "-m",
                    "src.data.select_baseline_connected_crop_origins",
                    "--country-code",
                    case.country_code,
                    "--candidate-gpkg",
                    str(candidate_gpkg),
                    "--overlay-gpkg",
                    str(overlay_dir),
                    "--output-dir",
                    str(connected_dir),
                    "--city-threshold",
                    str(args.city_threshold),
                    "--top-n-per-crop",
                    str(args.top_n_per_crop),
                    "--speed-paved-kmh",
                    str(args.speed_paved_kmh),
                    "--speed-unpaved-kmh",
                    str(args.speed_unpaved_kmh),
                    "--min-component-nodes",
                    str(args.min_component_nodes),
                    "--isolation-minutes",
                    str(args.isolation_minutes),
                ],
            ),
            (
                "weekly_accessibility_dijkstra",
                [
                    py,
                    "-m",
                    "src.data.run_weekly_accessibility_dijkstra",
                    "--country-code",
                    case.country_code,
                    "--start-date",
                    case.start_date,
                    "--end-date",
                    case.end_date,
                    "--step-days",
                    "7",
                    "--city-threshold",
                    str(args.city_threshold),
                    "--origins-file",
                    str(origins_gpkg),
                    "--overlay-gpkg",
                    str(overlay_dir),
                    "--thresholds-yaml",
                    str(args.thresholds_yaml),
                    "--output-root",
                    str(access_dir),
                    "--speed-paved-kmh",
                    str(args.speed_paved_kmh),
                    "--speed-unpaved-kmh",
                    str(args.speed_unpaved_kmh),
                    "--min-component-nodes",
                    str(args.min_component_nodes),
                    "--isolation-minutes",
                    str(args.isolation_minutes),
                ],
            ),
            (
                "weekly_plots",
                [py, "-m", "src.data.plot_weekly_accessibility_results", "--results-dir", str(access_dir)],
            ),
            (
                "crop_type_plots",
                [
                    py,
                    "-m",
                    "src.data.plot_crop_accessibility_results",
                    "--results-dir",
                    str(access_dir),
                    "--country-code",
                    case.country_code,
                    "--overlay-gpkg",
                    str(overlay_dir),
                    "--out-dir",
                    str(access_dir / "crop_type_maps"),
                ],
            ),
        ]
    )
    return commands


def _selected_cases(raw: str) -> list[EventCase]:
    if raw.strip().lower() == "all":
        return list(CASES.values())
    slugs = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(slugs).difference(CASES))
    if unknown:
        raise ValueError(f"Unknown case slug(s): {', '.join(unknown)}")
    return [CASES[slug] for slug in slugs]


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    run_id = args.run_id.strip() or f"event_cases_{datetime.now():%Y%m%d_%H%M%S}"
    batch_root = args.output_root if args.output_root.is_absolute() else project_root / args.output_root
    run_root = batch_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    cases = _selected_cases(args.cases)
    batch_manifest: dict[str, object] = {"run_id": run_id, "cases": [], "started_at": datetime.now().isoformat(timespec="seconds")}

    for case in cases:
        print(f"[case-batch] case={case.slug} country={case.country_code} start", flush=True)
        case_root = run_root / case.slug
        config_dir = case_root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        base_config = project_root / case.base_config
        config_path = config_dir / "datasets_event.yaml"
        damage_path = config_dir / "road_climate_damage_event.yaml"
        config_path.write_text(yaml.safe_dump(_event_config(base_config, case), sort_keys=False), encoding="utf-8")
        damage_path.write_text(yaml.safe_dump(_damage_config(case), sort_keys=False), encoding="utf-8")

        stages: list[dict[str, object]] = []
        case_failed = False
        for stage, command in _case_commands(args, project_root, case_root, case, config_path, damage_path):
            result = _run_stage(stage, command, project_root)
            stages.append(result)
            if result["returncode"] != 0:
                case_failed = True
                break
        case_report = {
            "case": asdict(case),
            "case_root": _relpath(case_root, project_root),
            "config": _relpath(config_path, project_root),
            "damage_config": _relpath(damage_path, project_root),
            "failed": case_failed,
            "stages": stages,
        }
        (case_root / "case_run_manifest.json").write_text(json.dumps(case_report, indent=2), encoding="utf-8")
        batch_manifest["cases"].append(case_report)
        if case_failed and not args.continue_on_error:
            break

    batch_manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (run_root / "batch_manifest.json").write_text(json.dumps(batch_manifest, indent=2), encoding="utf-8")
    print(json.dumps({"run_root": _relpath(run_root, project_root), "cases": len(batch_manifest["cases"])}, indent=2), flush=True)


if __name__ == "__main__":
    main()
