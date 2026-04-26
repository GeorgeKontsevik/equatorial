"""Run weekly road accessibility scenarios with pandana on a continuously updated graph."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import re

import geopandas as gpd
import numpy as np
import pandana as pdna
import pandas as pd
import yaml
from shapely.geometry import LineString, MultiLineString


FACTOR_PREFIXES = (
    "chirps_",
    "flood_",
    "landslide_",
    "gem_",
    "liquefaction_",
    "worldcover_",
    "soil_",
    "era5_",
    "cams_",
    "flopros_",
)

THRESHOLD_LEVELS = (
    "speed_reduction_1",
    "speed_reduction_2",
    "speed_reduction_3",
    "catastrophic_temporary",
    "catastrophic_permanent",
)
LEGACY_THRESHOLD_LEVEL_ALIASES = {
    "minor": "speed_reduction_1",
    "moderate": "speed_reduction_2",
    "severe": "speed_reduction_3",
    "catastrophic": "catastrophic_temporary",
}
SPEED_PENALTY_BY_LEVEL = {"speed_reduction_1": 0.10, "speed_reduction_2": 0.25, "speed_reduction_3": 0.40}
CLOSURE_WEEKS_BY_LEVEL = {"catastrophic_temporary": 1, "catastrophic_permanent": 5200}
PANDANA_UNREACHABLE_SENTINEL = np.iinfo(np.uint32).max / 1000.0
DROUGHT_FACTOR_PREFIX = "era5_spi_"
DROUGHT_SPEED_PENALTY_BY_LEVEL = {
    "speed_reduction_1": 0.0,
    "speed_reduction_2": 0.05,
    "speed_reduction_3": 0.10,
}
SPEED_ONLY_FACTOR_PREFIXES = (
    "era5_spi_",
    "era5_skt_",
)
SPEED_ONLY_PENALTY_BY_LEVEL = {
    "speed_reduction_1": 0.0,
    "speed_reduction_2": 0.05,
    "speed_reduction_3": 0.10,
}
TEMPERATURE_FACTOR_MARKERS = ("era5_t2m_", "era5_skt_")


@dataclass(slots=True)
class Scenario:
    name: str
    unknown_surface_mode: str


@dataclass(slots=True)
class ThresholdRule:
    factor: str
    direction: str
    thresholds: dict[str, float]
    surface_scope: str = "effective_unpaved"
    condition_factor: str | None = None
    condition_operator: str = "gte"
    condition_value: float | str | None = None
    effects: dict[str, dict[str, object]] | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run weekly accessibility scenarios with pandana.")
    parser.add_argument("--country-code", type=str, required=True, help="ISO3 country code, for example GAB.")
    parser.add_argument("--start-date", type=str, default="2024-07-01")
    parser.add_argument("--end-date", type=str, default="2024-09-30")
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--city-threshold", type=int, default=50000)
    parser.add_argument("--n-origins", type=int, default=5)
    parser.add_argument("--origins-seed", type=int, default=42)
    parser.add_argument("--origins-file", type=Path, default=None)
    parser.add_argument("--cities-file", type=Path, default=None)
    parser.add_argument("--overlay-gpkg", type=Path, default=None)
    parser.add_argument("--mapping-distance-m", type=float, default=5000.0)
    parser.add_argument("--isolation-minutes", type=float, default=100000.0)
    parser.add_argument("--speed-paved-kmh", type=float, default=60.0)
    parser.add_argument("--speed-unpaved-kmh", type=float, default=50.0)
    parser.add_argument("--min-component-nodes", type=int, default=500)
    parser.add_argument("--thresholds-csv", type=Path, default=None)
    parser.add_argument("--thresholds-yaml", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser.parse_args()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _week_starts(start: date, end: date, step_days: int) -> list[date]:
    values: list[date] = []
    cursor = start
    while cursor <= end:
        values.append(cursor)
        cursor += timedelta(days=step_days)
    return values


def _week_token(week_start: date) -> str:
    return week_start.isoformat().replace("-", "_")


def _iter_lines(geometry: object) -> list[LineString]:
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return [line for line in geometry.geoms if isinstance(line, LineString)]
    return []


def _round_coord(x: float, y: float) -> tuple[float, float]:
    return (round(float(x), 1), round(float(y), 1))


def _existing_cities_file(project_root: Path, iso3: str, threshold: int) -> Path | None:
    base = project_root / "outputs" / "road_scenarios" / iso3
    if not base.exists():
        return None
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        summary = folder / "summary.json"
        cities = folder / "cities_over_threshold.gpkg"
        if not summary.exists() or not cities.exists():
            continue
        payload = json.loads(summary.read_text(encoding="utf-8"))
        if int(payload.get("city_population_threshold", -1)) == int(threshold):
            return cities
    return None


def _existing_origins_file(project_root: Path, iso3: str) -> Path | None:
    base = project_root / "outputs" / "road_scenarios" / iso3
    if not base.exists():
        return None
    for folder in sorted(base.iterdir()):
        path = folder / "cropland_origins.gpkg"
        if path.exists():
            return path
    return None


def _resolve_overlay_path(project_root: Path, iso3: str, step_days: int, provided: Path | None) -> Path:
    if provided is not None:
        return provided
    candidates = sorted((project_root / "outputs" / "road_multisource_overlay" / iso3).glob(f"*_{step_days}d/roads_with_multisource_overlay.gpkg"))
    if not candidates:
        raise FileNotFoundError("No overlay GPKG found. Run run_multisource_road_overlay.py first.")
    return candidates[-1]


def _resolve_cities(project_root: Path, iso3: str, threshold: int, provided: Path | None) -> Path:
    if provided is not None:
        return provided
    cached = _existing_cities_file(project_root, iso3, threshold)
    if cached is None:
        raise FileNotFoundError(
            f"No cached cities_over_threshold.gpkg found for threshold {threshold}. "
            "Pass --cities-file explicitly or run the monthly scenario once."
        )
    return cached


def _resolve_origins(
    project_root: Path,
    iso3: str,
    n_origins: int,
    seed: int,
    output_dir: Path,
    provided: Path | None,
) -> Path:
    if provided is not None:
        return provided

    fixed_path = output_dir / f"fixed_origins_n{n_origins}_seed{seed}.gpkg"
    if fixed_path.exists():
        return fixed_path

    source = _existing_origins_file(project_root, iso3)
    if source is None:
        raise FileNotFoundError(
            "No cached cropland origins found. Pass --origins-file explicitly or run the monthly scenario once."
        )

    origins = gpd.read_file(source)
    if origins.empty:
        raise RuntimeError("Origins source is empty.")
    if n_origins <= 0 or n_origins > len(origins):
        sample = origins.copy()
    else:
        sample = origins.sample(n=n_origins, random_state=seed).copy()
    sample = sample.reset_index(drop=True)
    sample["origin_id"] = np.arange(len(sample), dtype=int)
    output_dir.mkdir(parents=True, exist_ok=True)
    sample.to_file(fixed_path, driver="GPKG")
    return fixed_path


def _numeric_factor_columns(roads: gpd.GeoDataFrame) -> list[str]:
    cols: list[str] = []
    for col in roads.columns:
        if col in {"road_row_id", "length_km"}:
            continue
        if not col.startswith(FACTOR_PREFIXES):
            continue
        if pd.api.types.is_numeric_dtype(roads[col]):
            cols.append(col)
    return cols


def _weekly_factor_column(factor: str, week_start: date) -> str | None:
    token = _week_token(week_start)
    special_weekly = {
        "chirps_24h_max_weekly_mm": f"chirps_24h_max_week_{token}_mm",
        "era5_tp_daily_sum_weekly_max_mm": f"era5_tp_daily_sum_max_week_{token}_mm",
        "era5_tp_1h_max_weekly_mm_per_h": f"era5_tp_1h_max_week_{token}_mm_per_h",
        "era5_crosswind_10m_weekly_max_m_s": f"era5_crosswind_10m_week_{token}_max",
        "era5_wind_gust_weekly_max_m_s": f"era5_wind_gust_week_{token}_max",
        "era5_max_total_precip_rate_weekly_mm_per_h": f"era5_max_total_precip_rate_week_{token}_mm_per_h",
    }
    if factor in special_weekly:
        return special_weekly[factor]
    if factor == "flood_weekly":
        return f"flood_week_{token}"
    if factor == "chirps_weekly_mm":
        return f"chirps_week_{token}_mm"
    if "_weekly_" in factor and (factor.startswith("era5_") or factor.startswith("cams_")):
        return factor.replace("_weekly_", f"_week_{token}_", 1)
    if factor in {
        "landslide_susceptibility",
        "gem_pga_475y",
        "liquefaction_class",
        "worldcover_class",
        "flopros_merl_riv",
        "flopros_dl_max_riv",
    }:
        return factor
    if factor.startswith(("soil_", "era5_spi_")):
        return factor
    return None


def _convert_factor_units(factor: str, values: pd.Series) -> pd.Series:
    series = pd.to_numeric(values, errors="coerce")
    if factor.startswith(TEMPERATURE_FACTOR_MARKERS):
        return series - 273.15
    return series


def _validate_threshold_factor_inputs(roads: gpd.GeoDataFrame, rules: list[ThresholdRule], weeks: list[date]) -> None:
    missing: list[str] = []
    factors: set[str] = set()
    for rule in rules:
        factors.add(rule.factor)
        if rule.condition_factor:
            factors.add(rule.condition_factor)
    for factor in sorted(factors):
        if factor in roads.columns and pd.api.types.is_numeric_dtype(roads[factor]):
            continue
        for week_start in weeks:
            col = _weekly_factor_column(factor, week_start)
            if col is None:
                missing.append(f"{factor} -> unsupported factor contract")
                break
            if col not in roads.columns:
                missing.append(f"{factor} -> missing {col}")
                break
    if missing:
        sample = "; ".join(missing[:8])
        extra = "" if len(missing) <= 8 else f" ... (+{len(missing) - 8} more)"
        raise RuntimeError(f"Overlay is missing required weekly factor columns for thresholds: {sample}{extra}")


def _threshold_float(value: object) -> float:
    if value is None:
        return float("nan")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return numeric if np.isfinite(numeric) else float("nan")


def _optional_float(value: object) -> float | None:
    numeric = _threshold_float(value)
    return None if not np.isfinite(numeric) else float(numeric)


def _optional_int(value: object) -> int | None:
    numeric = _optional_float(value)
    return None if numeric is None else int(numeric)


def _normalise_threshold_mapping(raw: dict, *, path: Path, rule_idx: int, section: str) -> dict[str, object]:
    values = {level: None for level in THRESHOLD_LEVELS}
    for raw_level, value in raw.items():
        level = LEGACY_THRESHOLD_LEVEL_ALIASES.get(str(raw_level), str(raw_level))
        if level not in values:
            raise ValueError(f"Unsupported {section} level `{raw_level}` in threshold YAML rule #{rule_idx}: {path}")
        values[level] = value
    return values


def _load_thresholds_from_csv(path: Path) -> list[ThresholdRule]:
    frame = pd.read_csv(path)
    required = {"factor", "threshold", "threshold_value"}
    if not required.issubset(set(frame.columns)):
        raise ValueError(f"Threshold CSV must contain columns {sorted(required)}: {path}")
    frame = frame.loc[frame["threshold"].isin(THRESHOLD_LEVELS)].copy()
    if "scenario" in frame.columns:
        # Use one scenario as reference (thresholds should be identical between scenarios).
        scenario_order = ["unknown_as_paved", "unknown_as_unpaved"]
        for scenario in scenario_order:
            part = frame.loc[frame["scenario"] == scenario].copy()
            if not part.empty:
                frame = part
                break
    grouped = (
        frame.groupby(["factor", "threshold"], as_index=False)["threshold_value"]
        .mean()
        .dropna(subset=["threshold_value"])
    )
    rules: list[ThresholdRule] = []
    for factor, part in grouped.groupby("factor"):
        row = {lvl: np.nan for lvl in THRESHOLD_LEVELS}
        for r in part.itertuples(index=False):
            row[str(r.threshold)] = float(r.threshold_value)
        if any(np.isfinite(float(row[lvl])) for lvl in THRESHOLD_LEVELS):
            direction = "gte"
            if "direction" in frame.columns:
                dir_vals = frame.loc[frame["factor"] == factor, "direction"].dropna().astype(str).str.lower().unique().tolist()
                if dir_vals:
                    direction = dir_vals[0]
            if direction not in {"gte", "lte"}:
                raise ValueError(f"Unsupported direction `{direction}` for factor `{factor}` in {path}")
            rules.append(
                ThresholdRule(
                    factor=str(factor),
                    direction=direction,
                    thresholds={lvl: float(row[lvl]) for lvl in THRESHOLD_LEVELS},
                    surface_scope="effective_unpaved",
                    effects=None,
                )
            )
    return rules


def _load_thresholds_from_yaml(path: Path) -> list[ThresholdRule]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = payload.get("road_hazard_thresholds", payload) if isinstance(payload, dict) else {}
    raw_rules = root.get("rules", []) if isinstance(root, dict) else []
    if not isinstance(raw_rules, list):
        raise ValueError(f"Threshold YAML must contain a rules list: {path}")

    rules: list[ThresholdRule] = []
    for idx, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Threshold YAML rule #{idx} must be a mapping: {path}")
        factor = str(item.get("factor", "")).strip()
        if not factor:
            raise ValueError(f"Threshold YAML rule #{idx} is missing factor: {path}")
        direction = str(item.get("direction", "gte")).strip().lower()
        if direction not in {"gte", "gt", "lte", "lt", "eq", "ne"}:
            raise ValueError(f"Unsupported direction `{direction}` for factor `{factor}` in {path}")
        raw_thresholds = item.get("thresholds", {}) or {}
        if not isinstance(raw_thresholds, dict):
            raise ValueError(f"Threshold YAML rule #{idx} thresholds must be a mapping: {path}")
        raw_thresholds = _normalise_threshold_mapping(raw_thresholds, path=path, rule_idx=idx, section="threshold")
        thresholds = {level: _threshold_float(raw_thresholds.get(level)) for level in THRESHOLD_LEVELS}
        if not any(np.isfinite(value) for value in thresholds.values()):
            continue

        condition = item.get("condition", {}) or {}
        if condition and not isinstance(condition, dict):
            raise ValueError(f"Threshold YAML rule #{idx} condition must be a mapping: {path}")
        condition_factor = item.get("condition_factor")
        condition_operator = item.get("condition_operator", "gte")
        condition_value = item.get("condition_value")
        if condition:
            condition_factor = condition.get("factor", condition_factor)
            condition_operator = condition.get("operator", condition_operator)
            condition_value = condition.get("value", condition_value)

        effects = item.get("effects")
        if effects is not None and not isinstance(effects, dict):
            raise ValueError(f"Threshold YAML rule #{idx} effects must be a mapping: {path}")
        if effects is not None:
            effects = _normalise_threshold_mapping(effects, path=path, rule_idx=idx, section="effect")

        rules.append(
            ThresholdRule(
                factor=factor,
                direction=direction,
                thresholds=thresholds,
                surface_scope=str(item.get("surface_scope", "all")).strip().lower() or "all",
                condition_factor=None if condition_factor is None else str(condition_factor).strip(),
                condition_operator=str(condition_operator).strip().lower(),
                condition_value=condition_value,
                effects=effects,
            )
        )
    if not rules:
        raise ValueError(f"No active threshold rules found in {path}")
    return rules


def _build_edges(roads: gpd.GeoDataFrame, factor_cols: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    node_lookup: dict[tuple[float, float], int] = {}
    node_rows: list[dict[str, float | int]] = []
    edge_rows: list[dict[str, float | int | str]] = []

    def ensure_node(x: float, y: float) -> int:
        key = _round_coord(x, y)
        node_id = node_lookup.get(key)
        if node_id is None:
            node_id = len(node_lookup)
            node_lookup[key] = node_id
            node_rows.append({"node_id": node_id, "x": key[0], "y": key[1]})
        return node_id

    for _, row in roads.iterrows():
        row_values = {col: row[col] for col in factor_cols}
        for line in _iter_lines(row.geometry):
            coords = list(line.coords)
            for start, end in zip(coords[:-1], coords[1:], strict=False):
                u = ensure_node(start[0], start[1])
                v = ensure_node(end[0], end[1])
                length_m = float(((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5)
                if length_m <= 0:
                    continue
                record: dict[str, float | int | str] = {
                    "u": u,
                    "v": v,
                    "length_m": length_m,
                    "road_row_id": int(row["road_row_id"]),
                    "surface_group": str(row["surface_group"]),
                }
                for col in factor_cols:
                    value = row_values[col]
                    record[col] = np.nan if value is None else float(value)
                edge_rows.append(record)

    nodes = pd.DataFrame(node_rows).sort_values("node_id").reset_index(drop=True)
    edges = pd.DataFrame(edge_rows)
    if edges.empty:
        raise RuntimeError("No traversable edges were generated from the overlay roads.")
    return nodes, edges


def _component_labels(num_nodes: int, edges: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    parent = np.arange(num_nodes, dtype=np.int64)
    rank = np.zeros(num_nodes, dtype=np.int8)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = int(parent[x])
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            parent[ra] = rb
        elif rank[ra] > rank[rb]:
            parent[rb] = ra
        else:
            parent[rb] = ra
            rank[ra] += 1

    for u, v in edges[["u", "v"]].itertuples(index=False):
        union(int(u), int(v))
    roots = np.asarray([find(i) for i in range(num_nodes)], dtype=np.int64)
    comp_roots, counts = np.unique(roots, return_counts=True)
    return roots, np.asarray(list(zip(comp_roots, counts)), dtype=np.int64)


def _filter_small_components(
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
    min_component_nodes: int,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    if min_component_nodes <= 1:
        return nodes, edges, {"components_total": 0, "components_dropped": 0, "nodes_dropped": 0, "edges_dropped": 0}

    roots, comp_stats_arr = _component_labels(len(nodes), edges)
    comp_roots = comp_stats_arr[:, 0]
    comp_counts = comp_stats_arr[:, 1]
    keep_roots = set(comp_roots[comp_counts >= min_component_nodes].tolist())
    keep_node_mask = np.asarray([int(root) in keep_roots for root in roots], dtype=bool)
    kept_node_ids = set(nodes.loc[keep_node_mask, "node_id"].astype(int).tolist())
    keep_edge_mask = edges["u"].astype(int).isin(kept_node_ids) & edges["v"].astype(int).isin(kept_node_ids)

    nodes_f = nodes.loc[keep_node_mask].copy()
    edges_f = edges.loc[keep_edge_mask].copy().reset_index(drop=True)
    stats = {
        "components_total": int(len(comp_roots)),
        "components_dropped": int((comp_counts < min_component_nodes).sum()),
        "nodes_dropped": int((~keep_node_mask).sum()),
        "edges_dropped": int((~keep_edge_mask).sum()),
    }
    return nodes_f, edges_f, stats


def _road_factor_values(roads: gpd.GeoDataFrame, week_start: date) -> dict[str, pd.Series]:
    values: dict[str, pd.Series] = {}
    for col in roads.columns:
        if not col.startswith(FACTOR_PREFIXES):
            continue
        if not pd.api.types.is_numeric_dtype(roads[col]):
            continue
        values[col] = pd.to_numeric(roads[col], errors="coerce")

    derived_weekly_factors = [
        "flood_weekly",
        "chirps_weekly_mm",
        "chirps_24h_max_weekly_mm",
        "era5_t2m_weekly_mean",
        "era5_t2m_weekly_max",
        "era5_skt_weekly_mean",
        "era5_skt_weekly_max",
        "era5_tp_weekly_sum",
        "era5_tp_daily_sum_weekly_max_mm",
        "era5_tp_1h_max_weekly_mm_per_h",
        "era5_swvl1_weekly_mean",
        "era5_u10_weekly_mean",
        "era5_v10_weekly_mean",
        "era5_wind_speed_weekly_mean",
        "era5_wind_speed_weekly_max",
        "era5_crosswind_10m_weekly_max_m_s",
        "era5_wind_gust_weekly_max_m_s",
        "era5_max_total_precip_rate_weekly_mm_per_h",
        "cams_pm2p5_weekly_mean",
        "cams_pm2p5_weekly_max",
        "cams_pm10_weekly_mean",
        "cams_pm10_weekly_max",
        "cams_duaod550_weekly_mean",
        "cams_duaod550_weekly_max",
    ]
    for factor in derived_weekly_factors:
        col = _weekly_factor_column(factor, week_start)
        if col is not None and col in roads.columns and pd.api.types.is_numeric_dtype(roads[col]):
            values[factor] = _convert_factor_units(factor, roads[col])
    return values


def _effective_surface(road_surface: pd.Series, unknown_mode: str) -> pd.Series:
    values = road_surface.astype("string").str.lower().fillna("unknown")
    return values.where(values != "unknown", unknown_mode)


def _surface_scope_mask(
    scope: str,
    *,
    effective_surface: pd.Series,
    road_surface: pd.Series,
    unique_road_ids: np.ndarray,
) -> np.ndarray:
    normalized = scope.strip().lower().replace("-", "_")
    if normalized in {"all", "any", "both", "*"}:
        return np.ones(len(unique_road_ids), dtype=bool)

    source = effective_surface
    target = normalized
    if normalized.startswith("effective_"):
        target = normalized.removeprefix("effective_")
    elif normalized.startswith("actual_"):
        source = road_surface.astype("string").str.lower().fillna("unknown")
        target = normalized.removeprefix("actual_")

    if target not in {"paved", "unpaved", "unknown"}:
        raise ValueError(f"Unsupported threshold surface_scope `{scope}`.")
    return np.asarray([str(source.loc[rid]).lower() == target for rid in unique_road_ids], dtype=bool)


def _compare_values(values: np.ndarray, operator: str, threshold: object) -> np.ndarray:
    op = operator.strip().lower()
    numeric_threshold = _optional_float(threshold)
    finite = np.isfinite(values)
    if numeric_threshold is None:
        return np.zeros(values.shape, dtype=bool)
    if op == "gte":
        return finite & (values >= numeric_threshold)
    if op == "gt":
        return finite & (values > numeric_threshold)
    if op == "lte":
        return finite & (values <= numeric_threshold)
    if op == "lt":
        return finite & (values < numeric_threshold)
    if op == "eq":
        return finite & (values == numeric_threshold)
    if op == "ne":
        return finite & (values != numeric_threshold)
    raise ValueError(f"Unsupported threshold comparison operator `{operator}`.")


def _dense_factor_values(
    roads: gpd.GeoDataFrame,
    factor_values: dict[str, pd.Series],
    factor: str,
    unique_road_ids: np.ndarray,
    road_id_to_dense: dict[int, int],
) -> np.ndarray:
    series = factor_values.get(factor)
    if series is None:
        series = pd.Series(np.nan, index=roads.index)
    road_arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    dense_values = np.full(len(unique_road_ids), np.nan, dtype=float)
    road_dense_idx = np.asarray([road_id_to_dense[int(rid)] for rid in roads["road_row_id"].astype(int)], dtype=int)
    dense_values[road_dense_idx] = road_arr
    return dense_values


def _condition_mask(
    rule: ThresholdRule,
    roads: gpd.GeoDataFrame,
    factor_values: dict[str, pd.Series],
    unique_road_ids: np.ndarray,
    road_id_to_dense: dict[int, int],
) -> np.ndarray:
    if not rule.condition_factor:
        return np.ones(len(unique_road_ids), dtype=bool)
    values = _dense_factor_values(roads, factor_values, rule.condition_factor, unique_road_ids, road_id_to_dense)
    return _compare_values(values, rule.condition_operator, rule.condition_value)


def _speed_penalty_for_factor(factor: str, level: str) -> float | None:
    if factor.startswith(DROUGHT_FACTOR_PREFIX):
        return DROUGHT_SPEED_PENALTY_BY_LEVEL.get(level)
    if factor.startswith(SPEED_ONLY_FACTOR_PREFIXES):
        return SPEED_ONLY_PENALTY_BY_LEVEL.get(level)
    return SPEED_PENALTY_BY_LEVEL.get(level)


def _closure_weeks_for_factor(factor: str, level: str) -> int | None:
    if factor.startswith(SPEED_ONLY_FACTOR_PREFIXES):
        return None
    return CLOSURE_WEEKS_BY_LEVEL.get(level)


def _rule_effect(rule: ThresholdRule, level: str) -> tuple[float | None, int | None]:
    if rule.effects is None:
        return _speed_penalty_for_factor(rule.factor, level), _closure_weeks_for_factor(rule.factor, level)
    raw = rule.effects.get(level)
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(f"Effect for {rule.factor}/{level} must be a mapping.")
    return _optional_float(raw.get("speed_penalty_fraction")), _optional_int(raw.get("closure_weeks"))


def _compute_accessibility(
    nodes: pd.DataFrame,
    edges: pd.DataFrame,
    origins: gpd.GeoDataFrame,
    cities: gpd.GeoDataFrame,
    mapping_distance_m: float,
    isolation_minutes: float,
) -> pd.DataFrame:
    network = pdna.Network(
        node_x=nodes["x"],
        node_y=nodes["y"],
        edge_from=edges["u"],
        edge_to=edges["v"],
        edge_weights=edges[["travel_minutes"]],
        twoway=True,
    )

    # No snapping/search heuristics: plain nearest node mapping, then exact shortest path
    # to each destination node and min across destinations.
    origin_nodes = network.get_node_ids(
        origins.geometry.x.values,
        origins.geometry.y.values,
        mapping_distance=None,
    ).to_numpy(dtype=int)
    city_nodes = network.get_node_ids(
        cities.geometry.x.values,
        cities.geometry.y.values,
        mapping_distance=None,
    ).to_numpy(dtype=int)
    city_nodes = city_nodes[city_nodes >= 0]
    city_nodes = np.unique(city_nodes)

    best = np.full(len(origin_nodes), np.inf, dtype="float64")
    valid_origins = origin_nodes >= 0
    if city_nodes.size > 0 and valid_origins.any():
        for city_node in city_nodes:
            target = np.full(valid_origins.sum(), int(city_node), dtype=int)
            dist = network.shortest_path_lengths(
                origin_nodes[valid_origins],
                target,
                imp_name="travel_minutes",
            )
            dist_arr = np.asarray(dist, dtype="float64")
            dist_arr[dist_arr >= (PANDANA_UNREACHABLE_SENTINEL - 1.0)] = np.inf
            dist_arr[~np.isfinite(dist_arr)] = np.inf
            best[valid_origins] = np.minimum(best[valid_origins], dist_arr)
    best[~valid_origins] = np.inf

    result = origins[["origin_id"]].copy()
    connected = np.isfinite(best)
    result["connected"] = connected
    result["access_minutes"] = np.where(connected, best, float(isolation_minutes))
    return result


def main() -> None:
    args = parse_args()
    project_root = _project_root()
    iso3 = args.country_code.upper()
    start_date = _parse_iso_date(args.start_date)
    end_date = _parse_iso_date(args.end_date)
    week_starts = _week_starts(start_date, end_date, args.step_days)

    period_slug = f"{start_date.isoformat()}_to_{end_date.isoformat()}_{args.step_days}d"
    output_dir = (
        args.output_root
        if args.output_root is not None
        else project_root / "outputs" / "road_weekly_scenarios" / iso3 / f"{period_slug}_pop{args.city_threshold}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    overlay_path = _resolve_overlay_path(project_root, iso3, args.step_days, args.overlay_gpkg)
    cities_path = _resolve_cities(project_root, iso3, args.city_threshold, args.cities_file)
    origins_path = _resolve_origins(project_root, iso3, args.n_origins, args.origins_seed, output_dir, args.origins_file)

    roads = gpd.read_file(overlay_path)
    cities = gpd.read_file(cities_path)
    origins = gpd.read_file(origins_path)
    if roads.empty or cities.empty or origins.empty:
        raise RuntimeError("One of required layers is empty (roads/cities/origins).")

    target_crs = roads.estimate_utm_crs()
    if target_crs is None:
        raise RuntimeError("Unable to estimate projected CRS for road network.")
    roads = roads.to_crs(target_crs)
    cities = cities.to_crs(target_crs)
    origins = origins.to_crs(target_crs)

    factor_cols = _numeric_factor_columns(roads)
    if args.thresholds_yaml is not None:
        thresholds_yaml_path = args.thresholds_yaml
        if not thresholds_yaml_path.exists():
            raise FileNotFoundError(f"Missing thresholds YAML: {thresholds_yaml_path}")
        threshold_rules = _load_thresholds_from_yaml(thresholds_yaml_path)
        thresholds_source = f"yaml:{thresholds_yaml_path}"
    elif args.thresholds_csv is not None:
        thresholds_csv_path = args.thresholds_csv
        if not thresholds_csv_path.exists():
            raise FileNotFoundError(f"Missing thresholds CSV: {thresholds_csv_path}")
        threshold_rules = _load_thresholds_from_csv(thresholds_csv_path)
        thresholds_source = f"csv:{thresholds_csv_path}"
    else:
        raise ValueError("Provide either --thresholds-yaml or --thresholds-csv.")
    _validate_threshold_factor_inputs(roads, threshold_rules, week_starts)

    nodes, edges = _build_edges(roads, factor_cols)
    nodes, edges, comp_filter_stats = _filter_small_components(nodes, edges, args.min_component_nodes)
    road_ids = edges["road_row_id"].to_numpy(dtype=int)
    unique_road_ids = np.asarray(sorted(roads["road_row_id"].astype(int).unique()), dtype=int)
    road_id_to_dense = {rid: idx for idx, rid in enumerate(unique_road_ids)}
    edge_road_dense = np.asarray([road_id_to_dense[rid] for rid in road_ids], dtype=int)

    road_surface = roads.set_index("road_row_id")["surface_group"]
    edge_surface = pd.Series(
        np.asarray([road_surface.loc[rid] for rid in road_ids], dtype="object"),
        index=edges.index,
        dtype="object",
    )
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unpaved", args.speed_unpaved_kmh, args.speed_paved_kmh)
    base_speed = np.where(edge_surface.astype("string").str.lower() == "unknown", args.speed_unpaved_kmh, base_speed)
    base_minutes = edges["length_m"].to_numpy(dtype=float) / 1000.0 / np.maximum(base_speed, 1.0) * 60.0

    baseline_edges = edges[["u", "v"]].copy()
    baseline_edges["travel_minutes"] = base_minutes
    baseline_access = _compute_accessibility(
        nodes,
        baseline_edges,
        origins=origins,
        cities=cities,
        mapping_distance_m=args.mapping_distance_m,
        isolation_minutes=args.isolation_minutes,
    )
    baseline_connected_fraction_before = float(baseline_access["connected"].mean())
    if not bool(baseline_access["connected"].all()):
        raise RuntimeError(
            "Baseline has disconnected origins. Provide a valid origins set where all origins are connected in baseline."
        )
    baseline_access["week_start"] = pd.NaT
    baseline_access["scenario"] = "baseline"

    scenarios = [
        Scenario(name="unknown_as_paved", unknown_surface_mode="paved"),
        Scenario(name="unknown_as_unpaved", unknown_surface_mode="unpaved"),
    ]

    weekly_access_rows: list[pd.DataFrame] = []
    weekly_summary_rows: list[dict[str, object]] = []
    factor_rows: list[dict[str, object]] = []
    road_state_rows: list[dict[str, object]] = []

    for scenario in scenarios:
        close_until = np.full(len(unique_road_ids), -1, dtype=int)
        effective_surface = _effective_surface(road_surface, scenario.unknown_surface_mode)
        unpaved_road_mask = np.asarray([effective_surface.loc[rid] == "unpaved" for rid in unique_road_ids], dtype=bool)
        road_surface_by_id = road_surface.astype("string").str.lower().fillna("unknown")

        for week_idx, week_start in enumerate(week_starts):
            factor_values = _road_factor_values(roads, week_start)
            road_speed_penalty = np.zeros(len(unique_road_ids), dtype=float)
            road_new_closure = np.zeros(len(unique_road_ids), dtype=int)

            for rule in threshold_rules:
                factor = rule.factor
                direction = rule.direction
                dense_values = _dense_factor_values(roads, factor_values, factor, unique_road_ids, road_id_to_dense)
                applicable_mask = _surface_scope_mask(
                    rule.surface_scope,
                    effective_surface=effective_surface,
                    road_surface=road_surface_by_id,
                    unique_road_ids=unique_road_ids,
                )
                applicable_mask &= _condition_mask(rule, roads, factor_values, unique_road_ids, road_id_to_dense)
                n_applicable = int(applicable_mask.sum())

                for level in THRESHOLD_LEVELS:
                    level_threshold = rule.thresholds[level]
                    if not np.isfinite(level_threshold):
                        continue
                    active_mask = applicable_mask & _compare_values(dense_values, direction, level_threshold)
                    n_active = int(active_mask.sum())
                    speed_penalty, closure_weeks = _rule_effect(rule, level)
                    if speed_penalty is not None:
                        road_speed_penalty[active_mask] = np.maximum(road_speed_penalty[active_mask], speed_penalty)
                    if closure_weeks is not None:
                        road_new_closure[active_mask] = np.maximum(road_new_closure[active_mask], closure_weeks)
                    effect_type = "none"
                    if closure_weeks is not None:
                        effect_type = "closure"
                    elif speed_penalty is not None and speed_penalty > 0:
                        effect_type = "speed_penalty"
                    factor_rows.append(
                        {
                            "week_start": week_start.isoformat(),
                            "scenario": scenario.name,
                            "factor": factor,
                            "surface_scope": rule.surface_scope,
                            "condition_factor": rule.condition_factor,
                            "condition_operator": rule.condition_operator if rule.condition_factor else None,
                            "condition_value": rule.condition_value if rule.condition_factor else None,
                            "threshold": level,
                            "threshold_value": float(level_threshold),
                            "effect_type": effect_type,
                            "speed_penalty_fraction": None if speed_penalty is None else float(speed_penalty),
                            "closure_weeks": None if closure_weeks is None else int(closure_weeks),
                            "n_applicable_roads": n_applicable,
                            "n_triggered_roads": n_active,
                            "share_triggered_unpaved_roads": None if unpaved_road_mask.sum() == 0 else float(n_active / unpaved_road_mask.sum()),
                            "share_triggered_applicable_roads": None if n_applicable == 0 else float(n_active / n_applicable),
                        }
                    )

            close_until = np.maximum(close_until, week_idx + road_new_closure - 1)
            road_closed = close_until >= week_idx
            edge_closed = road_closed[edge_road_dense]
            edge_penalty = road_speed_penalty[edge_road_dense]

            edge_speed_factor = np.maximum(0.05, 1.0 - edge_penalty)
            edge_minutes = base_minutes / edge_speed_factor
            active_edges = edges.loc[~edge_closed, ["u", "v"]].copy()
            active_edges["travel_minutes"] = edge_minutes[~edge_closed]

            if active_edges.empty:
                access = origins[["origin_id"]].copy()
                access["access_minutes"] = float(args.isolation_minutes)
                access["connected"] = False
            else:
                access = _compute_accessibility(
                    nodes,
                    active_edges,
                    origins=origins,
                    cities=cities,
                    mapping_distance_m=args.mapping_distance_m,
                    isolation_minutes=args.isolation_minutes,
                )

            access["week_start"] = week_start.isoformat()
            access["scenario"] = scenario.name
            weekly_access_rows.append(access)

            weekly_summary_rows.append(
                {
                    "week_start": week_start.isoformat(),
                    "scenario": scenario.name,
                    "n_origins": int(len(access)),
                    "connected_share": float(access["connected"].mean()),
                    "median_access_minutes": float(access["access_minutes"].median()),
                    "p90_access_minutes": float(access["access_minutes"].quantile(0.9)),
                    "n_closed_roads": int(road_closed.sum()),
                    "share_closed_roads": float(road_closed.mean()),
                }
            )
            road_state_rows.append(
                {
                    "week_start": week_start.isoformat(),
                    "scenario": scenario.name,
                    "n_unpaved_roads_effective": int(unpaved_road_mask.sum()),
                    "n_closed_roads": int(road_closed.sum()),
                }
            )

    baseline_path = output_dir / "baseline_routes.csv"
    baseline_access.to_csv(baseline_path, index=False)

    weekly_access = pd.concat(weekly_access_rows, ignore_index=True)
    weekly_summary = pd.DataFrame(weekly_summary_rows).sort_values(["scenario", "week_start"]).reset_index(drop=True)
    factor_counts = pd.DataFrame(factor_rows).sort_values(["scenario", "week_start", "factor", "threshold"]).reset_index(drop=True)
    road_state = pd.DataFrame(road_state_rows).sort_values(["scenario", "week_start"]).reset_index(drop=True)

    weekly_access.to_csv(output_dir / "weekly_accessibility.csv", index=False)
    weekly_summary.to_csv(output_dir / "weekly_summary.csv", index=False)
    factor_counts.to_csv(output_dir / "weekly_factor_threshold_counts.csv", index=False)
    road_state.to_csv(output_dir / "weekly_road_state.csv", index=False)
    origins.to_file(output_dir / "origins_used.gpkg", driver="GPKG")
    cities.to_file(output_dir / "cities_used.gpkg", driver="GPKG")

    summary = {
        "country_code": iso3,
        "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "step_days": args.step_days},
        "n_weeks": len(week_starts),
        "n_roads": int(len(roads)),
        "min_component_nodes": int(args.min_component_nodes),
        "component_filter": comp_filter_stats,
        "n_origins": int(len(origins)),
        "n_cities": int(len(cities)),
        "fixed_origins_source": str(origins_path),
        "cities_source": str(cities_path),
        "overlay_source": str(overlay_path),
        "isolation_minutes": float(args.isolation_minutes),
        "thresholds_source": thresholds_source,
        "baseline_connected_fraction_before_resample": baseline_connected_fraction_before,
        "origins_resampled_for_connectivity": False,
        "baseline_connected_fraction_final": float(baseline_access["connected"].mean()),
        "scenarios": [scenario.name for scenario in scenarios],
        "threshold_rules_count": int(len(threshold_rules)),
        "thresholds_yaml_path": None if args.thresholds_yaml is None else str(args.thresholds_yaml),
        "thresholds_csv_path": None if args.thresholds_csv is None else str(args.thresholds_csv),
        "outputs": {
            "baseline_routes_csv": str((output_dir / "baseline_routes.csv").relative_to(project_root)),
            "weekly_accessibility_csv": str((output_dir / "weekly_accessibility.csv").relative_to(project_root)),
            "weekly_summary_csv": str((output_dir / "weekly_summary.csv").relative_to(project_root)),
            "weekly_factor_threshold_counts_csv": str((output_dir / "weekly_factor_threshold_counts.csv").relative_to(project_root)),
            "weekly_road_state_csv": str((output_dir / "weekly_road_state.csv").relative_to(project_root)),
            "origins_used_gpkg": str((output_dir / "origins_used.gpkg").relative_to(project_root)),
            "cities_used_gpkg": str((output_dir / "cities_used.gpkg").relative_to(project_root)),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
