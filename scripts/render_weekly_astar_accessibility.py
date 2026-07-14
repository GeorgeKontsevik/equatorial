#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render weekly A* accessibility plots from eq.crop_accessibility_weekly_astar.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default="weekly_sum_penalty_v1")
    parser.add_argument(
        "--origin-scope",
        default="cluster_connected_allclusters_10small_3large_3ports_3airports",
    )
    parser.add_argument("--countries", default="loaded", help="loaded or comma-separated ISO3 list")
    parser.add_argument("--min-weeks", type=int, default=1)
    parser.add_argument("--split-crops", action="store_true", help="Also render one country-level PNG per crop.")
    parser.add_argument("--out-dir", default=None)
    return parser.parse_args()


def loaded_countries(conn: psycopg.Connection, scenario: str, origin_scope: str, min_weeks: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT country_code
            FROM eq.crop_accessibility_weekly_astar
            WHERE scenario = %s AND origin_scope = %s
            GROUP BY country_code
            HAVING count(DISTINCT week_start) >= %s
            ORDER BY min(week_start), country_code
            """,
            (scenario, origin_scope, min_weeks),
        )
        return [row[0] for row in cur.fetchall()]


def fetch_summary(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT country_code, week_start, dest_type,
               count(*) AS n_routes,
               count(*) FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS n_ok,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS q25_h,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS median_h,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS q75_h,
               max(travel_time_h) FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS max_h
        FROM eq.crop_accessibility_weekly_astar
        WHERE country_code = %(iso)s AND scenario = %(scenario)s AND origin_scope = %(origin_scope)s
        GROUP BY country_code, week_start, dest_type
        ORDER BY week_start, dest_type
        """,
        conn,
        params={"iso": iso, "scenario": scenario, "origin_scope": origin_scope},
    )


def fetch_crop_summary(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT country_code, crop_code, week_start, dest_type,
               count(*) AS n_routes,
               count(*) FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS n_ok,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS q25_h,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS median_h,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY travel_time_h)
                   FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS q75_h,
               max(travel_time_h) FILTER (WHERE route_status = 'ok' AND travel_time_h IS NOT NULL) AS max_h
        FROM eq.crop_accessibility_weekly_astar
        WHERE country_code = %(iso)s AND scenario = %(scenario)s AND origin_scope = %(origin_scope)s
        GROUP BY country_code, crop_code, week_start, dest_type
        ORDER BY crop_code, week_start, dest_type
        """,
        conn,
        params={"iso": iso, "scenario": scenario, "origin_scope": origin_scope},
    )


DEST_COLORS = {
    "city": "#2364aa",
    "city_5_100k": "#2364aa",
    "city_100k_plus": "#08519c",
    "port": "#d95f02",
    "airport": "#6a3d9a",
}
DEST_LABELS = {
    "city": "Top-10 reachable cities",
    "city_5_100k": "Small cities 5-100k",
    "city_100k_plus": "Large cities 100k+",
    "port": "Ports",
    "airport": "Airports",
}


def plot_country(frame: pd.DataFrame, iso: str, out_path: Path, scenario: str, origin_scope: str) -> None:
    frame = frame.copy()
    frame["week_start"] = pd.to_datetime(frame["week_start"])
    frame["unreachable_pct"] = 100.0 * (1.0 - frame["n_ok"] / frame["n_routes"].clip(lower=1))

    fig, (ax_time, ax_unreach) = plt.subplots(
        2,
        1,
        figsize=(14.0, 7.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    for dest_type, group in frame.groupby("dest_type", sort=True):
        group = group.sort_values("week_start")
        color = DEST_COLORS.get(dest_type, "#333333")
        label = DEST_LABELS.get(dest_type, dest_type)
        x = group["week_start"]
        ax_time.plot(x, group["median_h"], color=color, lw=2.2, label=f"{label} median")
        ax_time.fill_between(x, group["q25_h"], group["q75_h"], color=color, alpha=0.18, linewidth=0)
        ax_unreach.plot(x, group["unreachable_pct"], color=color, lw=1.8, label=label)

    weeks_done = frame["week_start"].nunique()
    row_count = int(frame["n_routes"].sum())
    ax_time.set_title(
        f"{iso} weekly A* accessibility aggregated across all crops, 2024 | {scenario}\n"
        f"{origin_scope} | weeks={weeks_done}/53 | rows={row_count:,}",
        fontsize=10,
    )
    ax_time.set_ylabel("Travel time, hours")
    ax_time.grid(alpha=0.22)
    ax_time.legend(loc="upper left", ncols=2, frameon=False)

    ax_unreach.set_ylabel("Unreachable, %")
    ax_unreach.set_ylim(-1, 101)
    ax_unreach.grid(alpha=0.22)
    ax_unreach.set_xlabel("Week start")
    ax_unreach.xaxis.set_major_locator(mdates.MonthLocator())
    ax_unreach.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))

    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def plot_overview(summary_frames: dict[str, pd.DataFrame], out_path: Path, scenario: str, origin_scope: str) -> None:
    if not summary_frames:
        return
    countries = list(summary_frames)
    n = len(countries)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 6.0, rows * 3.2), sharex=False)
    axes_flat = list(np.asarray(axes, dtype=object).ravel())

    for ax, iso in zip(axes_flat, countries):
        frame = summary_frames[iso].copy()
        frame["week_start"] = pd.to_datetime(frame["week_start"])
        for dest_type, group in frame.groupby("dest_type", sort=True):
            color = {
                **DEST_COLORS,
            }.get(dest_type, "#333333")
            label = {
                **DEST_LABELS,
            }.get(dest_type, dest_type)
            group = group.sort_values("week_start")
            ax.fill_between(
                group["week_start"],
                group["q25_h"],
                group["q75_h"],
                color=color,
                alpha=0.14,
                linewidth=0,
            )
            ax.plot(group["week_start"], group["median_h"], color=color, lw=1.6, label=label)
        ax.set_title(f"{iso} ({frame['week_start'].nunique()}/53 weeks)")
        ax.grid(alpha=0.18)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    for ax in axes_flat[len(countries) :]:
        ax.axis("off")
    legend_items = {}
    for ax in axes_flat[: len(countries)]:
        handles, labels = ax.get_legend_handles_labels()
        for handle, label in zip(handles, labels, strict=True):
            legend_items.setdefault(label, handle)
    fig.legend(list(legend_items.values()), list(legend_items), loc="upper center", bbox_to_anchor=(0.5, 0.965), ncols=3, frameon=False)
    fig.suptitle(
        f"Weekly A* accessibility overview aggregated across all crops | {scenario}\n{origin_scope}",
        y=0.995,
        fontsize=10,
    )
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def plot_crop(frame: pd.DataFrame, iso: str, crop: str, out_path: Path, scenario: str, origin_scope: str) -> None:
    subset = frame[frame["crop_code"].eq(crop)].copy()
    if subset.empty:
        return
    subset["week_start"] = pd.to_datetime(subset["week_start"])
    subset["unreachable_pct"] = 100.0 * (1.0 - subset["n_ok"] / subset["n_routes"].clip(lower=1))

    fig, (ax_time, ax_unreach) = plt.subplots(
        2,
        1,
        figsize=(14.0, 7.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    for dest_type, group in subset.groupby("dest_type", sort=True):
        group = group.sort_values("week_start")
        color = DEST_COLORS.get(dest_type, "#333333")
        label = DEST_LABELS.get(dest_type, dest_type)
        ax_time.plot(group["week_start"], group["median_h"], color=color, lw=2.2, label=f"{label} median")
        ax_time.fill_between(group["week_start"], group["q25_h"], group["q75_h"], color=color, alpha=0.18, linewidth=0)
        ax_unreach.plot(group["week_start"], group["unreachable_pct"], color=color, lw=1.8, label=label)

    weeks_done = subset["week_start"].nunique()
    row_count = int(subset["n_routes"].sum())
    ax_time.set_title(
        f"{iso} {crop} weekly A* accessibility, 2024 | {scenario}\n"
        f"{origin_scope} | weeks={weeks_done}/53 | rows={row_count:,}",
        fontsize=10,
    )
    ax_time.set_ylabel("Travel time, hours")
    ax_time.grid(alpha=0.22)
    ax_time.legend(loc="upper left", ncols=2, frameon=False)
    ax_unreach.set_ylabel("Unreachable, %")
    ax_unreach.set_ylim(-1, 101)
    ax_unreach.grid(alpha=0.22)
    ax_unreach.set_xlabel("Week start")
    ax_unreach.xaxis.set_major_locator(mdates.MonthLocator())
    ax_unreach.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "outputs" / "astar_accessibility_weekly" / f"{args.scenario}_{args.origin_scope}"
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "loaded":
            countries = loaded_countries(conn, args.scenario, args.origin_scope, args.min_weeks)
        else:
            countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()]
        log(f"[render] countries={','.join(countries)} out_dir={out_dir}")
        summary_frames: dict[str, pd.DataFrame] = {}
        manifest = []
        for iso in countries:
            frame = fetch_summary(conn, iso, args.scenario, args.origin_scope)
            if frame.empty:
                log(f"[skip] {iso} no rows")
                continue
            path = out_dir / f"{iso}_weekly_astar_accessibility.png"
            plot_country(frame, iso, path, args.scenario, args.origin_scope)
            crop_pngs: list[str] = []
            if args.split_crops:
                crop_frame = fetch_crop_summary(conn, iso, args.scenario, args.origin_scope)
                for crop in sorted(crop_frame["crop_code"].dropna().unique()):
                    crop_path = out_dir / "by_crop" / iso / f"{iso}_{crop}_weekly_astar_accessibility.png"
                    plot_crop(crop_frame, iso, crop, crop_path, args.scenario, args.origin_scope)
                    crop_pngs.append(str(crop_path))
            summary_frames[iso] = frame
            manifest.append(
                {
                    "country_code": iso,
                    "weeks": int(frame["week_start"].nunique()),
                    "rows": int(frame["n_routes"].sum()),
                    "png": str(path),
                    "crop_pngs": crop_pngs,
                }
            )
            log(f"[done] {iso} weeks={frame['week_start'].nunique()} rows={int(frame['n_routes'].sum()):,} png={path}")
        overview = out_dir / "_overview_weekly_astar_accessibility.png"
        plot_overview(summary_frames, overview, args.scenario, args.origin_scope)
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        log(f"[done] overview={overview}")


if __name__ == "__main__":
    main()
