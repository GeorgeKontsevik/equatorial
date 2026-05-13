"""Long-running road-hazard worker for overnight data/accessibility runs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full road-hazard accessibility workflow for a long overnight job.")
    parser.add_argument("--country-code", type=str, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--damage-config", type=Path, required=True)
    parser.add_argument("--thresholds-yaml", type=Path, required=True)
    parser.add_argument("--start-date", type=str, required=True)
    parser.add_argument("--end-date", type=str, required=True)
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--candidate-top-n", type=int, default=100)
    parser.add_argument("--top-n-per-crop", type=int, default=3)
    parser.add_argument("--spam-dir", type=Path, default=Path("spam_tifs"))
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    parser.add_argument("--min-component-nodes", type=int, default=500)
    parser.add_argument("--isolation-minutes", type=float, default=100000.0)
    parser.add_argument("--run-fetch", action="store_true", help="Fetch configured datasets before overlay.")
    parser.add_argument("--fetch-datasets", type=str, default="", help="Optional comma-separated dataset subset for fetch.")
    parser.add_argument("--skip-overlay", action="store_true")
    parser.add_argument("--skip-accessibility", action="store_true")
    parser.add_argument("--skip-plots", action="store_true")
    parser.add_argument("--skip-parquet", action="store_true")
    parser.add_argument("--export-overlay-parquet", action="store_true")
    return parser.parse_args()


def _relpath(path: Path, project_root: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def _period_slug(start_date: str, end_date: str, step_days: int) -> str:
    return f"{start_date}_to_{end_date}_{step_days}d"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_stage(stage: str, cmd: list[str], cwd: Path, manifest: list[dict[str, object]]) -> None:
    start = time.time()
    print(f"[overnight] stage={stage} start command={' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd, check=False)
    elapsed = time.time() - start
    manifest.append(
        {
            "stage": stage,
            "command": cmd,
            "returncode": int(result.returncode),
            "elapsed_seconds": round(elapsed, 2),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    print(f"[overnight] stage={stage} done returncode={result.returncode} elapsed_s={elapsed:.1f}", flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"Stage `{stage}` failed with return code {result.returncode}.")


def _parquet_available() -> tuple[bool, str]:
    try:
        import pyarrow as pa
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, str(pa.__version__)

def _resolve_output_root(args: argparse.Namespace, project_root: Path, iso3: str) -> Path:
    period_slug = _period_slug(args.start_date, args.end_date, args.step_days)
    output_root = args.output_root
    if output_root is None:
        output_root = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"{period_slug}_crop_connected_visibility_speed_dijkstra"
    elif not output_root.is_absolute():
        output_root = project_root / output_root
    return output_root


def build_stage_commands(*, args: argparse.Namespace, project_root: Path, python_bin: str) -> dict[str, object]:
    iso3 = args.country_code.upper()
    period_slug = _period_slug(args.start_date, args.end_date, args.step_days)
    output_root = _resolve_output_root(args, project_root, iso3)
    overlay_gpkg = project_root / "outputs" / "road_multisource_overlay" / iso3 / period_slug / "roads_with_multisource_overlay.gpkg"
    candidates_dir = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"origins_spam_top{args.candidate_top_n}_by_crop_candidates"
    candidate_gpkg = candidates_dir / f"spam_crop_top{args.candidate_top_n}_origins.gpkg"
    connected_origin_dir = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"origins_spam_top{args.top_n_per_crop}_by_crop_baseline_connected"
    origins_gpkg = connected_origin_dir / f"spam_crop_top{args.top_n_per_crop}_baseline_connected_origins.gpkg"

    stages: list[dict[str, object]] = []
    if args.run_fetch:
        command = [python_bin, "-m", "src.data.fetch", "--config", str(args.config), "--country-code", iso3]
        if args.fetch_datasets:
            command.extend(["--datasets", args.fetch_datasets])
        stages.append({"stage": "fetch", "command": command})

    if not args.skip_overlay:
        stages.append(
            {
                "stage": "overlay",
                "command": [
                    python_bin,
                    "-m",
                    "src.data.run_multisource_road_overlay",
                    "--config",
                    str(args.config),
                    "--country-code",
                    iso3,
                    "--damage-config",
                    str(args.damage_config),
                ],
            }
        )

    stages.append(
        {
            "stage": "build_crop_origin_candidates",
            "command": [
                python_bin,
                "-m",
                "src.data.build_spam_crop_top_origins",
                "--country-code",
                iso3,
                "--top-n",
                str(args.candidate_top_n),
                "--spam-dir",
                str(args.spam_dir),
                "--output-dir",
                str(candidates_dir),
            ],
        }
    )
    stages.append(
        {
            "stage": "select_baseline_connected_crop_origins",
            "command": [
                python_bin,
                "-m",
                "src.data.select_baseline_connected_crop_origins",
                "--country-code",
                iso3,
                "--candidate-gpkg",
                str(candidate_gpkg),
                "--overlay-gpkg",
                str(overlay_gpkg),
                "--output-dir",
                str(connected_origin_dir),
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
        }
    )

    if not args.skip_accessibility:
        stages.append(
            {
                "stage": "weekly_accessibility_dijkstra",
                "command": [
                    python_bin,
                    "-m",
                    "src.data.run_weekly_accessibility_dijkstra",
                    "--country-code",
                    iso3,
                    "--start-date",
                    args.start_date,
                    "--end-date",
                    args.end_date,
                    "--step-days",
                    str(args.step_days),
                    "--city-threshold",
                    str(args.city_threshold),
                    "--origins-file",
                    str(origins_gpkg),
                    "--overlay-gpkg",
                    str(overlay_gpkg),
                    "--thresholds-yaml",
                    str(args.thresholds_yaml),
                    "--output-root",
                    str(output_root),
                    "--speed-paved-kmh",
                    str(args.speed_paved_kmh),
                    "--speed-unpaved-kmh",
                    str(args.speed_unpaved_kmh),
                    "--min-component-nodes",
                    str(args.min_component_nodes),
                    "--isolation-minutes",
                    str(args.isolation_minutes),
                ],
            }
        )

    if not args.skip_plots:
        stages.append(
            {
                "stage": "weekly_plots",
                "command": [python_bin, "-m", "src.data.plot_weekly_accessibility_results", "--results-dir", str(output_root)],
            }
        )
        stages.append(
            {
                "stage": "crop_type_plots",
                "command": [
                    python_bin,
                    "-m",
                    "src.data.plot_crop_accessibility_results",
                    "--results-dir",
                    str(output_root),
                    "--country-code",
                    iso3,
                    "--overlay-gpkg",
                    str(overlay_gpkg),
                ],
            }
        )

    return {
        "country_code": iso3,
        "period_slug": period_slug,
        "overlay_gpkg": overlay_gpkg,
        "candidate_gpkg": candidate_gpkg,
        "origins_gpkg": origins_gpkg,
        "output_root": output_root,
        "stages": stages,
    }


def _export_parquet(results_dir: Path, *, overlay_gpkg: Path | None, project_root: Path, include_overlay: bool) -> dict[str, object]:
    available, version_or_error = _parquet_available()
    manifest: dict[str, object] = {
        "pyarrow_available": available,
        "pyarrow": version_or_error,
        "exports": [],
        "skipped": [],
    }
    if not available:
        manifest["skipped"].append("pyarrow is unavailable; keeping CSV/GPKG outputs only")
        return manifest

    exports: list[dict[str, str]] = []
    parquet_root = results_dir / "parquet"
    parquet_root.mkdir(parents=True, exist_ok=True)

    for csv_path in sorted(results_dir.glob("*.csv")) + sorted((results_dir / "crop_type_maps").glob("*.csv")):
        rel = csv_path.relative_to(results_dir)
        out_path = parquet_root / rel.with_suffix(".parquet")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.read_csv(csv_path).to_parquet(out_path, index=False)
        exports.append({"source": _relpath(csv_path, project_root), "parquet": _relpath(out_path, project_root)})

    for gpkg_name in ["origins_used.gpkg", "cities_used.gpkg"]:
        gpkg_path = results_dir / gpkg_name
        if not gpkg_path.exists():
            continue
        out_path = parquet_root / gpkg_path.with_suffix(".parquet").name
        gpd.read_file(gpkg_path).to_parquet(out_path, index=False)
        exports.append({"source": _relpath(gpkg_path, project_root), "parquet": _relpath(out_path, project_root)})

    if include_overlay and overlay_gpkg is not None and overlay_gpkg.exists():
        out_path = parquet_root / "roads_with_multisource_overlay.parquet"
        gpd.read_file(overlay_gpkg).to_parquet(out_path, index=False)
        exports.append({"source": _relpath(overlay_gpkg, project_root), "parquet": _relpath(out_path, project_root)})

    manifest["exports"] = exports
    return manifest


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    plan = build_stage_commands(args=args, project_root=project_root, python_bin=sys.executable)
    iso3 = str(plan["country_code"])
    period_slug = str(plan["period_slug"])
    output_root = Path(plan["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)
    worker_manifest: list[dict[str, object]] = []
    overlay_gpkg = Path(plan["overlay_gpkg"])
    origins_gpkg = Path(plan["origins_gpkg"])

    for stage in plan["stages"]:
        _run_stage(str(stage["stage"]), list(stage["command"]), project_root, worker_manifest)
    if not overlay_gpkg.exists():
        raise FileNotFoundError(f"Missing overlay after overlay stage: {overlay_gpkg}")
    if not origins_gpkg.exists():
        raise FileNotFoundError(f"Missing selected crop origins after worker stages: {origins_gpkg}")

    parquet_manifest = {"skipped": ["disabled by --skip-parquet"]}
    if not args.skip_parquet:
        parquet_manifest = _export_parquet(
            output_root,
            overlay_gpkg=overlay_gpkg,
            project_root=project_root,
            include_overlay=args.export_overlay_parquet,
        )
        (output_root / "parquet_manifest.json").write_text(json.dumps(parquet_manifest, indent=2), encoding="utf-8")

    report = {
        "country_code": iso3,
        "period_slug": period_slug,
        "output_root": _relpath(output_root, project_root),
        "overlay_gpkg": _relpath(overlay_gpkg, project_root),
        "origins_gpkg": _relpath(origins_gpkg, project_root),
        "stages": worker_manifest,
        "parquet": parquet_manifest,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    (output_root / "overnight_worker_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
