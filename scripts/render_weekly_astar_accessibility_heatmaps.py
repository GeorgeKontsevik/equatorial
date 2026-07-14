#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.colors import ListedColormap
from matplotlib.gridspec import GridSpec
import numpy as np
import pandas as pd
import psycopg

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql://gk@127.0.0.1:5432/equatorial"
REPO_ROOT = ROOT.parent
LBR_SIDE_PANEL = REPO_ROOT / "itmo-phd-thesis-template-en" / "images" / "ch4" / "lbr_precip_grid_week_2024_08_19.png"
CROP_ORDER = ["avocado", "banana", "mango", "pineapple", "plantain", "bean", "cott", "maiz", "pota", "rice", "sorg", "soyb", "sugc", "sunf", "whea"]
DEST_TYPE_ORDER = ["city_5_100k", "city_100k_plus", "port", "airport"]
MONTH_LABELS_RU = {
    1: "янв",
    2: "фев",
    3: "мар",
    4: "апр",
    5: "май",
    6: "июн",
    7: "июл",
    8: "авг",
    9: "сен",
    10: "окт",
    11: "ноя",
    12: "дек",
}
CROP_LABELS = {
    "avocado": "авокадо",
    "banana": "банан",
    "mango": "манго",
    "pineapple": "ананас",
    "plantain": "плантан",
    "bean": "фасоль",
    "cott": "хлопок",
    "maiz": "кукуруза",
    "pota": "картофель",
    "rice": "рис",
    "sorg": "сорго",
    "soyb": "соя",
    "sugc": "сахарный тростник",
    "sunf": "подсолнечник",
    "whea": "пшеница",
}
DEST_TYPE_LABELS = {
    "city_5_100k": "малый город 5-100 тыс.",
    "city_100k_plus": "крупный город 100 тыс.+",
    "port": "порт",
    "airport": "аэропорт",
}
DAMAGE_CLASS_BINS_MIN = [180.0, 360.0, 540.0, 720.0, 1440.0]
DAMAGE_CLASS_LABELS = [
    "<3ч",
    "3-6ч",
    "6-9ч",
    "9-12ч",
    "12-24ч",
    ">24ч",
]
DAMAGE_CLASS_COLORS = ["#fffdf2", "#fee08b", "#fdae61", "#f46d43", "#d73027", "#7f0000"]


def log(message: str) -> None:
    print(message, flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render clearer crop x week A* accessibility heatmaps.")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--scenario", default="weekly_sum_penalty_v1")
    parser.add_argument(
        "--origin-scope",
        default="cluster_connected_allclusters_10small_3large_3ports_3airports",
    )
    parser.add_argument("--countries", default="loaded", help="loaded or comma-separated ISO3 list")
    parser.add_argument("--min-weeks", type=int, default=1)
    parser.add_argument("--metric", choices=["delta_minutes", "ratio"], default="delta_minutes")
    parser.add_argument("--agg", choices=["median", "p90", "p95", "max"], default="median")
    parser.add_argument("--cap-minutes", type=float, default=240.0)
    parser.add_argument("--cap-ratio", type=float, default=5.0)
    parser.add_argument("--precip-scenario", default="unknown_as_unpaved")
    parser.add_argument("--precip-scope", default="all")
    parser.add_argument("--precip-factor", default="era5_tp_sum_weekly_mm")
    parser.add_argument("--precip-y-max", type=float, default=500.0)
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
            ORDER BY min(run_at), country_code
            """,
            (scenario, origin_scope, min_weeks),
        )
        return [row[0] for row in cur.fetchall()]


def fetch_country(conn: psycopg.Connection, iso: str, scenario: str, origin_scope: str) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT country_code, week_start, crop_code, candidate_rank, crop_rank,
               dest_type, dest_rank, dest_id, route_status, travel_time_h
        FROM eq.crop_accessibility_weekly_astar
        WHERE country_code = %(iso)s
          AND scenario = %(scenario)s
          AND origin_scope = %(origin_scope)s
        ORDER BY week_start, crop_code, candidate_rank, dest_type, dest_rank, dest_id
        """,
        conn,
        params={"iso": iso, "scenario": scenario, "origin_scope": origin_scope},
    )


def fetch_precip(
    conn: psycopg.Connection,
    iso: str,
    precip_scenario: str,
    precip_scope: str,
    precip_factor: str,
) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT country_code, week_start, scenario, surface_scope, factor,
               n_values, min_value, q25, median, q75, max_value
        FROM eq.boxplot_stats_weekly
        WHERE country_code = %(iso)s
          AND scenario = %(scenario)s
          AND surface_scope = %(surface_scope)s
          AND factor = %(factor)s
        ORDER BY week_start
        """,
        conn,
        params={
            "iso": iso,
            "scenario": precip_scenario,
            "surface_scope": precip_scope,
            "factor": precip_factor,
        },
    )


def fetch_penalty_rules(conn: psycopg.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT road_type, min_weekly_mm, max_weekly_mm, speed_multiplier, effect_label
        FROM eq.weekly_rain_speed_penalty_rules
        WHERE min_weekly_mm > 0
        ORDER BY road_type, min_weekly_mm
        """,
        conn,
    )


def add_ratios(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["week_start"] = pd.to_datetime(frame["week_start"])
    frame["od_key"] = (
        frame["crop_code"].astype(str)
        + "|"
        + frame["candidate_rank"].astype(str)
        + "|"
        + frame["dest_type"].astype(str)
        + "|"
        + frame["dest_rank"].astype(str)
        + "|"
        + frame["dest_id"].astype(str)
    )
    ok = frame["route_status"].eq("ok") & frame["travel_time_h"].notna() & (frame["travel_time_h"] > 0)
    baseline = frame.loc[ok].groupby("od_key")["travel_time_h"].min()
    frame["baseline_h"] = frame["od_key"].map(baseline)
    frame["travel_ratio"] = np.where(ok & frame["baseline_h"].gt(0), frame["travel_time_h"] / frame["baseline_h"], np.nan)
    frame["delta_minutes"] = np.where(ok & frame["baseline_h"].notna(), (frame["travel_time_h"] - frame["baseline_h"]) * 60.0, np.nan)
    return frame


def week_labels(weeks: list[pd.Timestamp]) -> list[str]:
    labels: list[str] = []
    last_month = None
    for week in weeks:
        if week.month != last_month:
            labels.append(f"{MONTH_LABELS_RU.get(week.month, week.strftime('%b').lower())} {week.day:02d}")
            last_month = week.month
        else:
            labels.append("")
    return labels


def crop_label(code: str) -> str:
    return CROP_LABELS.get(code, code)


def agg_label_ru(value: str) -> str:
    return {"median": "медианная", "p90": "p90", "p95": "p95", "max": "максимальная"}.get(value, value)


def precip_scope_label(value: str) -> str:
    return {"all": "все", "paved": "асфальт", "unpaved": "грунт"}.get(value, value)


def precip_scenario_label(value: str) -> str:
    return {
        "unknown_as_unpaved": "дороги с неизвестным покрытием считаются грунтовыми",
        "unknown_as_paved": "дороги с неизвестным покрытием считаются асфальтированными",
        "actual_unpaved": "только фактически грунтовые",
    }.get(value, value.replace("_", " "))


def routing_scenario_label(value: str) -> str:
    return {
        "weekly_sum_penalty_v1": "недельный штраф по осадкам, версия 1",
    }.get(value, value)


def origin_scope_label(value: str) -> str:
    return {
        "cluster_connected_allclusters_10small_3large_3ports_3airports": "все кластеры культур, 10 малых городов, 3 крупных, 3 порта, 3 аэропорта",
        "top5_per_crop": "топ-5 источников по каждой культуре",
    }.get(value, value)


def crop_order(crops: list[str]) -> list[str]:
    known = [crop for crop in CROP_ORDER if crop in crops]
    rest = sorted(crop for crop in crops if crop not in known)
    return known + rest


def damage_class_values(values: np.ndarray) -> np.ma.MaskedArray:
    masked = np.ma.masked_invalid(values.astype(float))
    classes = np.digitize(masked.filled(np.nan), DAMAGE_CLASS_BINS_MIN, right=False).astype(float)
    classes[masked.mask] = np.nan
    return np.ma.masked_invalid(classes)


def plot_heatmap(
    ax: plt.Axes,
    frame: pd.DataFrame,
    dest_type: str,
    weeks: list[pd.Timestamp],
    crops: list[str],
    metric: str,
    agg: str,
    cap_value: float,
) -> object | None:
    subset = frame[frame["dest_type"].eq(dest_type)]
    label_name = DEST_TYPE_LABELS.get(dest_type, dest_type)
    if subset.empty:
        ax.text(0.5, 0.5, f"Нет маршрутов: {label_name}", ha="center", va="center", transform=ax.transAxes)
        ax.set_axis_off()
        return None

    def aggregate_metric(values: pd.Series) -> float:
        clean = values.dropna()
        if clean.empty:
            return np.nan
        if agg == "p90":
            return float(np.nanpercentile(clean, 90))
        if agg == "p95":
            return float(np.nanpercentile(clean, 95))
        if agg == "max":
            return float(clean.max())
        return float(clean.median())

    summary = (
        subset.groupby(["crop_code", "week_start"], dropna=False)
        .agg(
            metric_value=(metric, aggregate_metric),
            n_routes=("od_key", "count"),
            n_ok=("travel_ratio", lambda s: int(s.notna().sum())),
        )
        .reset_index()
    )
    matrix = summary.pivot(index="crop_code", columns="week_start", values="metric_value").reindex(index=crops, columns=weeks)
    raw_values = matrix.to_numpy(dtype=float)
    if metric == "delta_minutes":
        values = damage_class_values(raw_values)
        cmap = ListedColormap(DAMAGE_CLASS_COLORS)
        cmap.set_bad("#d9d9d9")
        image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap=cmap, vmin=-0.5, vmax=len(DAMAGE_CLASS_LABELS) - 0.5)
    else:
        values = np.ma.masked_invalid(np.minimum(raw_values, cap_value))
        cmap = plt.get_cmap("YlOrRd").copy()
        cmap.set_bad("#d9d9d9")
        image = ax.imshow(values, aspect="auto", interpolation="nearest", cmap=cmap, vmin=1.0, vmax=cap_value)
    ax.set_yticks(np.arange(len(crops)))
    ax.set_yticklabels([crop_label(crop) for crop in crops])
    ax.set_xticks(np.arange(len(weeks)))
    ax.set_xticklabels([])
    ax.tick_params(axis="x", labelbottom=False)
    agg_label = agg_label_ru(agg)
    label = (
        f"{agg_label} класс деградации доступности по дополнительным минутам"
        if metric == "delta_minutes"
        else f"{agg_label} множитель времени пути"
    )
    ax.set_title(f"{label_name}: {label}")
    ax.set_ylabel("культура")
    ax.grid(False)

    # Mark weeks where a crop has no reachable route for that destination type.
    missing = matrix.isna().to_numpy()
    if missing.any():
        y_idx, x_idx = np.where(missing)
        ax.scatter(x_idx, y_idx, marker="x", s=10, c="#555555", linewidths=0.6)
    return image


def plot_precip_boxplot(
    ax: plt.Axes,
    precip_frame: pd.DataFrame,
    penalty_rules: pd.DataFrame,
    weeks: list[pd.Timestamp],
    precip_scenario: str,
    precip_scope: str,
    precip_y_max: float | None,
) -> None:
    precip_frame = precip_frame.copy()
    if precip_frame.empty:
        ax.text(0.5, 0.5, "Нет данных по недельным осадкам", ha="center", va="center", transform=ax.transAxes)
        return
    precip_frame["week_start"] = pd.to_datetime(precip_frame["week_start"])
    by_week = {pd.Timestamp(row.week_start): row for row in precip_frame.itertuples(index=False)}
    stats = []
    positions = []
    for idx, week in enumerate(weeks):
        row = by_week.get(week)
        if row is None or int(row.n_values or 0) <= 0:
            continue
        stats.append(
            {
                "label": "",
                "med": float(row.median),
                "q1": float(row.q25),
                "q3": float(row.q75),
                "whislo": float(row.min_value),
                "whishi": float(row.max_value),
                "fliers": [],
            }
        )
        positions.append(idx)
    if stats:
        ax.bxp(stats, positions=positions, widths=0.55, showfliers=False, patch_artist=True)
        for patch in ax.patches:
            patch.set(facecolor="#9ecae1", alpha=0.55, edgecolor="#2b6c8a")
    ax.set_title(
        f"Осадки: недельная сумма ERA5, дороги = {precip_scope_label(precip_scope)}, сценарий = {precip_scenario_label(precip_scenario)}"
    )
    ax.set_ylabel("мм/нед.")
    ax.set_xlim(-0.5, len(weeks) - 0.5)
    ax.set_xticks(np.arange(len(weeks)))
    ax.set_xticklabels(week_labels(weeks), rotation=35, ha="right", fontsize=8)
    ax.set_xlabel("начало недели")
    if precip_y_max is not None:
        ax.set_ylim(0, precip_y_max)
    ax.grid(axis="y", alpha=0.22)
    draw_penalty_thresholds(ax, penalty_rules, precip_y_max)


def draw_penalty_thresholds(ax: plt.Axes, penalty_rules: pd.DataFrame, precip_y_max: float | None) -> None:
    if penalty_rules.empty:
        return
    ymax = precip_y_max if precip_y_max is not None else ax.get_ylim()[1]
    styles = {
        "unpaved": {"color": "#b2182b", "linestyle": ":", "linewidth": 1.15, "label": "грунт"},
        "paved": {"color": "#2166ac", "linestyle": "--", "linewidth": 1.05, "label": "асфальт"},
    }
    used_labels: set[str] = set()
    threshold_values: dict[str, list[float]] = {}
    for road_type, group in penalty_rules.groupby("road_type", sort=True):
        style = styles.get(road_type, {"color": "#555555", "linestyle": "-.", "linewidth": 1.0, "label": road_type})
        for row in group.itertuples(index=False):
            y = float(row.min_weekly_mm)
            if y <= 0 or y > ymax:
                continue
            threshold_values.setdefault(road_type, []).append(y)
            label = style["label"] if style["label"] not in used_labels else None
            ax.axhline(
                y,
                color=style["color"],
                linestyle=style["linestyle"],
                linewidth=style["linewidth"],
                alpha=0.78,
                label=label,
            )
            used_labels.add(style["label"])
    if threshold_values:
        lines = ["использованные пороги"]
        for road_type in ["paved", "unpaved"]:
            values = threshold_values.get(road_type, [])
            if values:
                label = "грунт + неизвестное покрытие ···" if road_type == "unpaved" else "асфальт --"
                joined = ", ".join(f"{value:g}" for value in sorted(set(values)))
                lines.append(f"{label}: {joined} мм/нед.")
        ax.text(
            0.995,
            0.965,
            "\n".join(lines),
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=7.2,
            color="#222222",
            bbox={"facecolor": "white", "edgecolor": "#cfcfcf", "alpha": 0.88, "boxstyle": "round,pad=0.3"},
        )


def plot_country(
    frame: pd.DataFrame,
    precip_frame: pd.DataFrame,
    penalty_rules: pd.DataFrame,
    iso: str,
    out_path: Path,
    scenario: str,
    origin_scope: str,
    metric: str,
    agg: str,
    cap_value: float,
    precip_scenario: str,
    precip_scope: str,
    precip_y_max: float | None,
) -> dict[str, object]:
    frame = add_ratios(frame)
    weeks = sorted(frame["week_start"].dropna().unique())
    weeks = [pd.Timestamp(x) for x in weeks]
    crops = crop_order(sorted(frame["crop_code"].dropna().unique().tolist()))

    dest_types = [dest_type for dest_type in DEST_TYPE_ORDER if dest_type in set(frame["dest_type"].dropna())]
    if not dest_types:
        dest_types = sorted(frame["dest_type"].dropna().unique().tolist())

    side_panel_path = LBR_SIDE_PANEL if iso.upper() == "LBR" and LBR_SIDE_PANEL.exists() else None
    use_side_panel = side_panel_path is not None
    fig = plt.figure(figsize=((18.6 if use_side_panel else 15.8), max(11.8, 3.8 + len(dest_types) * 2.6 + len(crops) * 0.50)))
    if use_side_panel:
        grid = GridSpec(
            len(dest_types) + 1,
            3,
            figure=fig,
            height_ratios=[*[1.0 for _ in dest_types], 0.72],
            width_ratios=[1.0, 0.030, 0.74],
            hspace=0.46,
            wspace=0.08,
        )
    else:
        grid = GridSpec(
            len(dest_types) + 1,
            2,
            figure=fig,
            height_ratios=[*[1.0 for _ in dest_types], 0.72],
            width_ratios=[1.0, 0.030],
            hspace=0.46,
            wspace=0.025,
        )
    heat_axes = []
    shared_ax = None
    for row_idx, _dest_type in enumerate(dest_types):
        ax = fig.add_subplot(grid[row_idx, 0], sharex=shared_ax)
        if shared_ax is None:
            shared_ax = ax
        heat_axes.append(ax)
    precip_ax = fig.add_subplot(grid[len(dest_types), 0], sharex=shared_ax)
    cbar_ax = fig.add_subplot(grid[: len(dest_types), 1])
    fig.add_subplot(grid[len(dest_types), 1]).set_axis_off()
    if use_side_panel:
        side_ax = fig.add_subplot(grid[:, 2])
        side_ax.imshow(mpimg.imread(side_panel_path))
        side_ax.set_axis_off()
    images = []
    for ax, dest_type in zip(heat_axes, dest_types):
        image = plot_heatmap(ax, frame, dest_type, weeks, crops, metric, agg, cap_value)
        if image is not None:
            images.append(image)
    if images:
        cbar = fig.colorbar(
            images[0],
            cax=cbar_ax,
            orientation="vertical",
        )
        if metric == "delta_minutes":
            cbar.set_ticks(np.arange(len(DAMAGE_CLASS_LABELS)))
            cbar.set_ticklabels(DAMAGE_CLASS_LABELS)
            cbar.ax.tick_params(labelsize=8)
            cbar.set_label("Прокси деградации доступности по дополнительной задержке маршрута")
        else:
            cbar.set_label(f"Медианный множитель времени пути относительно базового OD, потолок {cap_value:g}x")
    plot_precip_boxplot(precip_ax, precip_frame, penalty_rules, weeks, precip_scenario, precip_scope, precip_y_max)
    for ax in heat_axes:
        ax.tick_params(axis="x", labelbottom=False)

    ok_rows = int(frame["travel_ratio"].notna().sum())
    all_rows = int(len(frame))
    max_ratio = float(frame["travel_ratio"].max(skipna=True) or 0)
    max_delta = float(frame["delta_minutes"].max(skipna=True) or 0)
    fig.suptitle(
        f"{'Либерия' if iso.upper() == 'LBR' else iso} 2024: недельное влияние осадков на доступность | сценарий: {routing_scenario_label(scenario)} | источники: {origin_scope_label(origin_scope)} | агрегация: {agg_label_ru(agg)}\n"
        f"недель={len(weeks)}/53 | валидных маршрутов={ok_rows:,}/{all_rows:,} | "
        f"макс. задержка={max_delta:.0f} мин | макс. множитель={max_ratio:.1f} раза | дороги с неизвестным покрытием считаются грунтовыми",
        y=0.988,
        fontsize=11.0,
    )
    fig.text(
        0.07,
        0.045,
        f"Ячейка матрицы = {agg_label_ru(agg)} оценка дополнительных минут по маршрутам «источник — назначение» для культуры и недели; базовый уровень = лучшая неделя для того же маршрута; × = нет валидного маршрута.\n"
        "Состав источников указан в заголовке; режим всех кластеров включает все сохранённые терминалы кластеров культур и заданные лимиты по назначениям. "
        "Классы задержки акцентируют интервалы 6-12 часов; <3ч = низкая деградация, >=12ч = тяжёлая деградация. "
        "Осадки: ERA5, недельная сумма, все дороги, участки с неизвестным покрытием учитываются как грунтовые.",
        ha="left",
        va="center",
        fontsize=8.5,
    )
    fig.subplots_adjust(left=0.07, right=0.95, top=0.90, bottom=0.14)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=175)
    plt.close(fig)
    return {
        "country_code": iso,
        "weeks": len(weeks),
        "rows": all_rows,
        "ok_rows": ok_rows,
        "max_ratio": max_ratio,
        "max_delta_minutes": max_delta,
        "png": str(out_path),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else ROOT / "outputs" / "astar_accessibility_weekly" / f"{args.scenario}_{args.origin_scope}_{args.metric}_heatmaps"
    with psycopg.connect(args.db_url) as conn:
        if args.countries.strip().lower() == "loaded":
            countries = loaded_countries(conn, args.scenario, args.origin_scope, args.min_weeks)
        else:
            countries = [x.strip().upper() for x in args.countries.split(",") if x.strip()]
        log(f"[render] countries={','.join(countries)} out_dir={out_dir}")
        penalty_rules = fetch_penalty_rules(conn)
        manifest = []
        for iso in countries:
            frame = fetch_country(conn, iso, args.scenario, args.origin_scope)
            if frame.empty:
                log(f"[skip] {iso} no rows")
                continue
            precip_frame = fetch_precip(conn, iso, args.precip_scenario, args.precip_scope, args.precip_factor)
            item = plot_country(
                frame,
                precip_frame,
                penalty_rules,
                iso,
                out_dir / f"{iso}_weekly_accessibility_impact_heatmap.png",
                args.scenario,
                args.origin_scope,
                args.metric,
                args.agg,
                args.cap_minutes if args.metric == "delta_minutes" else args.cap_ratio,
                args.precip_scenario,
                args.precip_scope,
                args.precip_y_max,
            )
            manifest.append(item)
            log(
                f"[done] {iso} weeks={item['weeks']} rows={item['rows']:,} "
                f"ok={item['ok_rows']:,} max_delta={item['max_delta_minutes']:.0f} max_ratio={item['max_ratio']:.1f} png={item['png']}"
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"[done] manifest={out_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
