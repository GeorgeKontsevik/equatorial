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
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt

from src.data.run_road_monthly_scenarios import _country_layers
from src.data.run_weekly_accessibility_dijkstra import _build_edges, _compute_accessibility_dijkstra, _filter_small_components
from src.data.run_weekly_accessibility_pandana import _project_root, _resolve_cities, _round_output_frame


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


def _select_baseline_connected_origins(
    *,
    project_root: Path,
    iso3: str,
    candidate_gpkg: Path,
    overlay_gpkg: Path,
    out_dir: Path,
    city_threshold: int,
    top_n_per_crop: int,
    speed_paved_kmh: float,
    speed_unpaved_kmh: float,
    min_component_nodes: int,
    isolation_minutes: float,
) -> Path:
    print("[overnight] selecting baseline-connected crop origins", flush=True)
    candidates = gpd.read_file(candidate_gpkg)
    roads = gpd.read_file(overlay_gpkg)
    cities = gpd.read_file(_resolve_cities(project_root, iso3, city_threshold, None))
    target_crs = roads.estimate_utm_crs()
    if target_crs is None:
        raise RuntimeError("Unable to estimate projected CRS for road network.")

    roads_proj = roads.to_crs(target_crs)
    candidates_proj = candidates.to_crs(target_crs)
    cities_proj = cities.to_crs(target_crs)
    nodes, edges = _build_edges(roads_proj, [])
    nodes, edges, component_stats = _filter_small_components(nodes, edges, min_component_nodes)

    road_surface = roads_proj.set_index("road_row_id")["surface_group"]
    road_ids = edges["road_row_id"].to_numpy(dtype=int)
    edge_surface = pd.Series(np.asarray([road_surface.loc[rid] for rid in road_ids], dtype="object"), index=edges.index, dtype="object")
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unpaved", speed_unpaved_kmh, speed_paved_kmh)
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unknown", speed_unpaved_kmh, base_speed)
    baseline_edges = edges[["u", "v"]].copy()
    baseline_edges["travel_minutes"] = edges["length_m"].to_numpy(dtype=float) / 1000.0 / np.maximum(base_speed, 1.0) * 60.0

    access = _compute_accessibility_dijkstra(nodes, baseline_edges, candidates_proj, cities_proj, isolation_minutes)
    candidates_access = candidates.copy().merge(access[["origin_id", "connected", "access_minutes"]], on="origin_id", how="left")

    selected_parts: list[gpd.GeoDataFrame] = []
    warnings: list[dict[str, object]] = []
    for crop_code, part in candidates_access.sort_values(["crop_code", "crop_rank"]).groupby("crop_code", sort=True):
        connected = part.loc[part["connected"].astype(bool)].head(top_n_per_crop).copy()
        if len(connected) < top_n_per_crop:
            warnings.append({"crop_code": crop_code, "connected_candidates": int(len(connected)), "target": int(top_n_per_crop)})
            print(f"[overnight] warning crop={crop_code} connected={len(connected)} target={top_n_per_crop}", flush=True)
        selected_parts.append(connected)

    selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else candidates_access.iloc[0:0].copy()
    if selected.empty:
        raise RuntimeError("No baseline-connected crop origins were selected.")
    selected["source_crop_rank"] = selected["crop_rank"].astype(int)
    selected["crop_rank"] = selected.groupby("crop_code").cumcount() + 1
    selected["origin_id"] = np.arange(len(selected), dtype=int)
    selected = gpd.GeoDataFrame(selected, geometry="geometry", crs=candidates.crs)

    out_dir.mkdir(parents=True, exist_ok=True)
    gpkg_path = out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins.gpkg"
    csv_path = out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins.csv"
    candidates_csv = out_dir / f"spam_crop_top{len(candidates)}_candidate_baseline_access.csv"
    selected.to_file(gpkg_path, driver="GPKG")
    _round_output_frame(pd.DataFrame(selected.drop(columns="geometry"))).to_csv(csv_path, index=False)
    _round_output_frame(pd.DataFrame(candidates_access.drop(columns="geometry"))).to_csv(candidates_csv, index=False)

    country, _ = _country_layers(project_root, iso3)
    fig, ax = plt.subplots(figsize=(9.0, 9.0))
    country.to_crs("EPSG:4326").boundary.plot(ax=ax, color="black", linewidth=1.2)
    crops = sorted(selected["crop_code"].unique())
    cmap = plt.get_cmap("tab10", len(crops))
    for idx, crop_code in enumerate(crops):
        subset = selected.loc[selected["crop_code"].eq(crop_code)]
        subset.plot(ax=ax, color=cmap(idx), markersize=55, edgecolor="white", linewidth=0.8, label=crop_code)
        for row in subset.itertuples():
            ax.text(row.geometry.x, row.geometry.y, f"{row.crop_code}{int(row.crop_rank)}", fontsize=7, ha="left", va="bottom")
    ax.set_title(f"{iso3} SPAM Top-{top_n_per_crop} Baseline-Connected Origins Per Crop")
    ax.set_axis_off()
    ax.legend(loc="lower left", ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(out_dir / f"spam_crop_top{top_n_per_crop}_baseline_connected_origins_map.png", dpi=180)
    plt.close(fig)

    summary = {
        "country_code": iso3,
        "candidate_gpkg": _relpath(candidate_gpkg, project_root),
        "selected_gpkg": _relpath(gpkg_path, project_root),
        "selected_csv": _relpath(csv_path, project_root),
        "n_candidates": int(len(candidates_access)),
        "n_selected": int(len(selected)),
        "n_crops": int(selected["crop_code"].nunique()),
        "component_filter": component_stats,
        "warnings": warnings,
    }
    (out_dir / "baseline_connected_origin_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return gpkg_path


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
    iso3 = args.country_code.upper()
    period_slug = _period_slug(args.start_date, args.end_date, args.step_days)
    output_root = args.output_root
    if output_root is None:
        output_root = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"{period_slug}_crop_connected_visibility_speed_dijkstra"
    elif not output_root.is_absolute():
        output_root = project_root / output_root
    output_root.mkdir(parents=True, exist_ok=True)
    worker_manifest: list[dict[str, object]] = []

    overlay_gpkg = project_root / "outputs" / "road_multisource_overlay" / iso3 / period_slug / "roads_with_multisource_overlay.gpkg"

    python = sys.executable
    if args.run_fetch:
        cmd = [python, "-m", "src.data.fetch", "--config", str(args.config), "--country-code", iso3]
        if args.fetch_datasets:
            cmd.extend(["--datasets", args.fetch_datasets])
        _run_stage("fetch", cmd, project_root, worker_manifest)

    if not args.skip_overlay:
        _run_stage(
            "overlay",
            [
                python,
                "-m",
                "src.data.run_multisource_road_overlay",
                "--config",
                str(args.config),
                "--country-code",
                iso3,
                "--damage-config",
                str(args.damage_config),
            ],
            project_root,
            worker_manifest,
        )
    if not overlay_gpkg.exists():
        raise FileNotFoundError(f"Missing overlay after overlay stage: {overlay_gpkg}")

    candidates_dir = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"origins_spam_top{args.candidate_top_n}_by_crop_candidates"
    _run_stage(
        "build_crop_origin_candidates",
        [
            python,
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
        project_root,
        worker_manifest,
    )
    candidate_gpkg = candidates_dir / f"spam_crop_top{args.candidate_top_n}_origins.gpkg"
    connected_origin_dir = project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"origins_spam_top{args.top_n_per_crop}_by_crop_baseline_connected"
    origins_gpkg = _select_baseline_connected_origins(
        project_root=project_root,
        iso3=iso3,
        candidate_gpkg=candidate_gpkg,
        overlay_gpkg=overlay_gpkg,
        out_dir=connected_origin_dir,
        city_threshold=args.city_threshold,
        top_n_per_crop=args.top_n_per_crop,
        speed_paved_kmh=args.speed_paved_kmh,
        speed_unpaved_kmh=args.speed_unpaved_kmh,
        min_component_nodes=args.min_component_nodes,
        isolation_minutes=args.isolation_minutes,
    )

    if not args.skip_accessibility:
        _run_stage(
            "weekly_accessibility_dijkstra",
            [
                python,
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
            project_root,
            worker_manifest,
        )

    if not args.skip_plots:
        _run_stage(
            "weekly_plots",
            [python, "-m", "src.data.plot_weekly_accessibility_results", "--results-dir", str(output_root)],
            project_root,
            worker_manifest,
        )
        _run_stage(
            "crop_type_plots",
            [
                python,
                "-m",
                "src.data.plot_crop_accessibility_results",
                "--results-dir",
                str(output_root),
                "--country-code",
                iso3,
                "--overlay-gpkg",
                str(overlay_gpkg),
            ],
            project_root,
            worker_manifest,
        )

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
