#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
DEFAULT_FACTORS = ("era5_tp_sum_weekly_mm",)
SCENARIO_LABELS = {
    "actual_unpaved": "Actual unpaved roads",
    "unknown_as_paved": "Unknown roads treated as paved",
    "unknown_as_unpaved": "Unknown roads treated as unpaved",
}
FACTOR_LABELS = {
    "era5_tp_sum_weekly_mm": "ERA5 weekly precipitation total",
    "era5_tp_mean_hourly_mm": "ERA5 mean hourly precipitation",
    "era5_tp_median_hourly_mm": "ERA5 median hourly precipitation",
    "era5_tp_1h_max_weekly_mm_per_h": "ERA5 hourly precipitation intensity",
}


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render weekly cell-level boxplots from eq.boxplot_stats_weekly.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--countries", default="loaded", help="Comma-separated ISO3 list or loaded.")
    parser.add_argument("--exclude", default="BRA", help="Comma-separated ISO3 list to skip.")
    parser.add_argument(
        "--factors",
        default=",".join(DEFAULT_FACTORS),
        help="Comma-separated factors to render, or all. Defaults to weekly precipitation sum only.",
    )
    parser.add_argument("--y-min", type=float, default=0.0)
    parser.add_argument("--y-max", type=float, default=500.0)
    parser.add_argument("--force", action="store_true", help="Regenerate even if the expected PNG set already exists.")
    return parser.parse_args()


def parse_factors(raw: str) -> list[str] | None:
    if raw.strip().lower() == "all":
        return None
    factors = [part.strip() for part in raw.split(",") if part.strip()]
    if not factors:
        raise ValueError("--factors must be a comma-separated list or all")
    return factors


def loaded_countries(conn: psycopg.Connection, factors: list[str] | None) -> list[str]:
    factor_filter = "" if factors is None else "WHERE factor = ANY(%s)"
    params = () if factors is None else (factors,)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT country_code
            FROM eq.boxplot_stats_weekly
            {factor_filter}
            GROUP BY country_code
            HAVING count(DISTINCT week_start) = 53
               AND count(DISTINCT scenario) = 3
               AND count(DISTINCT surface_scope) = 3
               AND count(DISTINCT factor) = %s
               AND count(*) = 53 * 3 * 3 * %s
            ORDER BY country_code
            """,
            params + ((4 if factors is None else len(factors)), (4 if factors is None else len(factors))),
        )
        return [row[0] for row in cur.fetchall()]


def fetch_country(conn: psycopg.Connection, iso: str, factors: list[str] | None) -> pd.DataFrame:
    factor_sql = "" if factors is None else "AND factor = ANY(%(factors)s)"
    params: dict[str, object] = {"iso": iso}
    if factors is not None:
        params["factors"] = factors
    return pd.read_sql_query(
        f"""
        SELECT country_code, week_start::text AS week_start, scenario, surface_scope, factor,
               n_values, min_value, q25, median, q75, max_value
        FROM eq.boxplot_stats_weekly
        WHERE country_code = %(iso)s
        {factor_sql}
        ORDER BY scenario, surface_scope, factor, week_start
        """,
        conn,
        params=params,
    )


def plot_group(
    frame: pd.DataFrame,
    iso: str,
    scenario: str,
    scope: str,
    factor: str,
    out_path: Path,
    *,
    y_min: float | None,
    y_max: float | None,
) -> None:
    bxp_stats = []
    for row in frame.sort_values("week_start").itertuples(index=False):
        if int(row.n_values or 0) <= 0:
            continue
        week_label = pd.to_datetime(row.week_start).strftime("%b %d")
        bxp_stats.append(
            {
                "label": week_label,
                "med": row.median,
                "q1": row.q25,
                "q3": row.q75,
                "whislo": row.min_value,
                "whishi": row.max_value,
                "fliers": [],
            }
        )
    fig, ax = plt.subplots(figsize=(14.0, 6.2))
    if bxp_stats:
        ax.bxp(bxp_stats, showfliers=False)
    else:
        ax.text(0.5, 0.5, "No finite cell values", transform=ax.transAxes, ha="center", va="center")
    title_parts = [iso, "2024"]
    if scenario != "actual_unpaved":
        title_parts.append(SCENARIO_LABELS.get(scenario, scenario))
    title_parts.extend([scope, FACTOR_LABELS.get(factor, factor)])
    ax.set_title(" | ".join(title_parts))
    ax.set_xlabel("Week start")
    ax.set_ylabel("Factor value per cell")
    if y_min is not None or y_max is not None:
        ax.set_ylim(bottom=y_min, top=y_max)
    ax.grid(alpha=0.22)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170)
    plt.close(fig)


def render_country(
    conn: psycopg.Connection,
    iso: str,
    force: bool,
    factors: list[str] | None,
    *,
    y_min: float | None,
    y_max: float | None,
) -> dict[str, object]:
    out_dir = ROOT / "outputs" / "road_weekly_scenarios" / iso / f"2024_full_year_db_cell_overlay_{iso.lower()}" / "factor_boxplots_cell"
    png_dir = out_dir / "weekly_factor_value_boxplots"
    existing_pngs = sorted(png_dir.glob("*.png")) if png_dir.exists() else []
    expected_pngs = 36 if factors is None else 3 * 3 * len(factors)
    if len(existing_pngs) == expected_pngs and not force:
        log(f"[skip] {iso} PNGs already exist n={expected_pngs} dir={png_dir}")
        return {"country_code": iso, "skipped": True, "n_pngs": expected_pngs, "png_dir": str(png_dir)}
    if existing_pngs:
        for path in existing_pngs:
            path.unlink()

    frame = fetch_country(conn, iso, factors)
    if frame.empty:
        raise RuntimeError(f"{iso}: no rows in eq.boxplot_stats_weekly")
    log(f"[start] {iso} render rows={len(frame):,}")

    diagnostics_path = out_dir / "weekly_factor_value_diagnostics.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(diagnostics_path, index=False)

    n_pngs = 0
    for (scenario, scope, factor), group in frame.groupby(["scenario", "surface_scope", "factor"], sort=True):
        out_path = png_dir / f"{scenario}__{scope}__{factor}.png"
        plot_group(group, iso, scenario, scope, factor, out_path, y_min=y_min, y_max=y_max)
        n_pngs += 1

    summary = {
        "country_code": iso,
        "aggregation_unit": "cell",
        "source_table": "eq.boxplot_stats_weekly",
        "overlay_table": "eq.era5_precip_cell_overlay",
        "n_stats_rows": int(len(frame)),
        "n_pngs": int(n_pngs),
        "weeks": sorted(frame["week_start"].unique().tolist()),
        "factors": sorted(frame["factor"].unique().tolist()),
        "scenarios": sorted(frame["scenario"].unique().tolist()),
        "surface_scopes": sorted(frame["surface_scope"].unique().tolist()),
        "y_axis": {"min": y_min, "max": y_max},
        "diagnostics_csv": str(diagnostics_path.relative_to(ROOT.parent)),
        "png_dir": str(png_dir.relative_to(ROOT.parent)),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"[done] {iso} pngs={n_pngs} dir={png_dir}")
    return summary


def main() -> None:
    args = parse_args()
    exclude = {part.strip().upper() for part in args.exclude.split(",") if part.strip()}
    factors = parse_factors(args.factors)
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "loaded":
            countries = loaded_countries(conn, factors)
        else:
            countries = [part.strip().upper() for part in args.countries.split(",") if part.strip()]
        countries = [iso for iso in countries if iso not in exclude]
        factor_label = "all" if factors is None else ",".join(factors)
        log(f"[render] countries={','.join(countries)} exclude={','.join(sorted(exclude))} factors={factor_label} force={args.force}")
        summaries = [
            render_country(conn, iso, args.force, factors, y_min=args.y_min, y_max=args.y_max)
            for iso in countries
        ]
    log(json.dumps({"countries": [s["country_code"] for s in summaries], "n_countries": len(summaries)}, indent=2))


if __name__ == "__main__":
    main()
