#!/usr/bin/env python3
from __future__ import annotations

import argparse
import selectors
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PY = ROOT / ".venv" / "bin" / "python"
ORIGIN_SCOPE = "cluster_connected_allclusters_10small_3large_3ports_3airports"
PLOTS_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / f"{ORIGIN_SCOPE}_plots"
HEATMAP_DIR = ROOT / "outputs" / "astar_accessibility_weekly" / f"{ORIGIN_SCOPE}_delta_minutes_heatmaps"

DEFAULT_COUNTRIES = [
    "CIV",
    "CMR",
    "AGO",
    "SOM",
    "LKA",
    "ETH",
    "UGA",
    "ECU",
    "VEN",
    "COD",
    "KEN",
    "PER",
    "COL",
    "TZA",
    "MYS",
    "NGA",
]


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def run_cmd(label: str, cmd: list[str], heartbeat_s: int) -> None:
    log(f"START {label}: {' '.join(cmd)}")
    process = subprocess.Popen(
        cmd,
        cwd=SCRIPTS,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    start = time.monotonic()
    last_heartbeat = start
    while process.poll() is None:
        events = selector.select(timeout=1.0)
        if events:
            line = process.stdout.readline()
            if line:
                print(line, end="", flush=True)
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_s:
            log(f"HEARTBEAT {label} still running elapsed_s={now - start:.0f}")
            last_heartbeat = now
    for line in process.stdout:
        print(line, end="", flush=True)
    code = process.wait()
    elapsed = time.monotonic() - start
    if code != 0:
        raise subprocess.CalledProcessError(code, cmd)
    log(f"DONE {label} elapsed_s={elapsed:.0f}")


def run_country(iso: str, heartbeat_s: int) -> None:
    run_cmd(
        f"{iso} build cluster_connected",
        [str(PY), "build_cluster_connected_graphs.py", "--countries", iso],
        heartbeat_s,
    )
    run_cmd(
        f"{iso} weekly astar",
        [
            str(PY),
            "run_weekly_astar_accessibility.py",
            "--countries",
            iso,
            "--graph-prefix",
            "cluster_connected",
            "--top-per-crop",
            "0",
            "--small-city-limit",
            "10",
            "--port-limit",
            "3",
            "--large-city-limit",
            "3",
            "--airport-limit",
            "3",
            "--force-snap",
            "--force-od",
            "--replace",
            "--heartbeat-s",
            str(heartbeat_s),
        ],
        heartbeat_s,
    )
    run_cmd(
        f"{iso} render plots",
        [
            str(PY),
            "render_weekly_astar_accessibility.py",
            "--origin-scope",
            ORIGIN_SCOPE,
            "--countries",
            iso,
            "--min-weeks",
            "53",
            "--split-crops",
            "--out-dir",
            str(PLOTS_DIR),
        ],
        heartbeat_s,
    )
    run_cmd(
        f"{iso} render heatmap",
        [
            str(PY),
            "render_weekly_astar_accessibility_heatmaps.py",
            "--origin-scope",
            ORIGIN_SCOPE,
            "--countries",
            iso,
            "--min-weeks",
            "53",
            "--metric",
            "delta_minutes",
            "--agg",
            "median",
            "--out-dir",
            str(HEATMAP_DIR),
        ],
        heartbeat_s,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--countries", default=",".join(DEFAULT_COUNTRIES))
    parser.add_argument("--heartbeat-s", type=int, default=300)
    args = parser.parse_args()
    countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()]
    log(f"batch countries={','.join(countries)} heartbeat_s={args.heartbeat_s}")
    for iso in countries:
        run_country(iso, args.heartbeat_s)
    run_cmd(
        "final render all completed plots",
        [
            str(PY),
            "render_weekly_astar_accessibility.py",
            "--origin-scope",
            ORIGIN_SCOPE,
            "--countries",
            "loaded",
            "--min-weeks",
            "53",
            "--split-crops",
            "--out-dir",
            str(PLOTS_DIR),
        ],
        args.heartbeat_s,
    )
    run_cmd(
        "final render all completed heatmaps",
        [
            str(PY),
            "render_weekly_astar_accessibility_heatmaps.py",
            "--origin-scope",
            ORIGIN_SCOPE,
            "--countries",
            "loaded",
            "--min-weeks",
            "53",
            "--metric",
            "delta_minutes",
            "--agg",
            "median",
            "--out-dir",
            str(HEATMAP_DIR),
        ],
        args.heartbeat_s,
    )
    log("batch complete")


if __name__ == "__main__":
    main()
