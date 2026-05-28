"""Fetcher for Copernicus GFM observed flood extent via STAC."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.data.catalog import CatalogRecord
from src.data.utils import downloaded_record, ensure_directory, ensure_local_copy, join_notes, manual_record, set_progress_total, update_progress


COPERNICUS_GFM_STAC_API = "https://stac.eodc.eu/api/v1/search"
COPERNICUS_GFM_SOURCE = "https://global-flood.emergency.copernicus.eu/react/general-information/data-access/"
COPERNICUS_GFM_LICENSE_NOTE = (
    "Copernicus Emergency Management Service (GFM via EODC STAC). Check current access/license terms."
)


def _parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_sequence(dataset_cfg: dict) -> list[date]:
    step_days = int(dataset_cfg.get("aggregation_period_days", dataset_cfg.get("step_days", 1)))
    if step_days <= 0:
        raise ValueError("`aggregation_period_days` must be a positive integer.")

    if dataset_cfg.get("dates"):
        return sorted({_parse_iso_date(str(item)) for item in dataset_cfg["dates"]})

    if dataset_cfg.get("start_date") and dataset_cfg.get("end_date"):
        start = _parse_iso_date(str(dataset_cfg["start_date"]))
        end = _parse_iso_date(str(dataset_cfg["end_date"]))
        if end < start:
            raise ValueError(f"`end_date` must not be earlier than `start_date`: {start}..{end}")
        days = (end - start).days
        sequence = [start + timedelta(days=offset) for offset in range(0, days + 1, step_days)]
        if sequence[-1] != end:
            sequence.append(end)
        return sequence

    recent_days = int(dataset_cfg.get("recent_days", 7))
    if recent_days <= 0:
        raise ValueError("`recent_days` must be a positive integer.")
    end = datetime.now(tz=UTC).date()
    start = end - timedelta(days=recent_days - 1)
    days = (end - start).days
    sequence = [start + timedelta(days=offset) for offset in range(0, days + 1, step_days)]
    if sequence[-1] != end:
        sequence.append(end)
    return sequence


def _http_json_post(url: str, payload: dict, context) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": context.user_agent,
        },
        method="POST",
    )
    with urlopen(request, timeout=context.timeout_seconds) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _item_datetime(feature: dict) -> datetime:
    raw = str(feature.get("properties", {}).get("datetime", "")).strip()
    if not raw:
        return datetime.min.replace(tzinfo=UTC)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _week_end(start: date, *, window_days: int, hard_end: date) -> date:
    end = start + timedelta(days=max(window_days - 1, 0))
    return end if end <= hard_end else hard_end


def _search_week_features(
    *,
    stac_api_url: str,
    collection_id: str,
    bbox: list[float],
    start_day: date,
    end_day: date,
    max_items: int,
    context,
) -> list[dict]:
    payload: dict[str, object] = {
        "collections": [collection_id],
        "bbox": [float(value) for value in bbox],
        "datetime": f"{start_day.isoformat()}T00:00:00Z/{end_day.isoformat()}T23:59:59Z",
        "limit": max(1, min(max_items, 500)),
    }

    features: list[dict] = []
    token: str | None = None
    while len(features) < max_items:
        if token:
            payload["token"] = token
        elif "token" in payload:
            payload.pop("token", None)

        response = _http_json_post(stac_api_url, payload, context)
        page_features = response.get("features", [])
        if isinstance(page_features, list):
            features.extend(page_features)
        if len(features) >= max_items:
            break

        token = None
        for link in response.get("links", []):
            if link.get("rel") == "next":
                body = link.get("body", {})
                if isinstance(body, dict) and body.get("token"):
                    token = str(body["token"])
                    break
        if not token:
            break
    return features[:max_items]


def fetch(dataset_cfg: dict, context) -> list[CatalogRecord]:
    """Download Copernicus GFM weekly observed flood snapshots for configured dates and bbox."""

    bbox = dataset_cfg.get("bbox")
    if not bbox or len(bbox) != 4:
        instructions = """# Manual Steps For Copernicus GFM Flood

No bounding box was configured for flood search.

What to do:
1. Set `study_area.bbox` or `datasets.flood.bbox` in WGS84 `[minx, miny, maxx, maxy]`.
2. Re-run `python -m src.data.fetch --config config/datasets.yaml`.
"""
        return [
            manual_record(
                dataset_name="flood",
                source_url=COPERNICUS_GFM_SOURCE,
                context=context,
                instruction_text=instructions,
                license_or_access_note=COPERNICUS_GFM_LICENSE_NOTE,
                spatial_resolution_raw=str(dataset_cfg.get("spatial_resolution_raw", "20 m")),
                temporal_resolution=str(dataset_cfg.get("temporal_resolution", "event-based SAR flood extent")),
                bbox=bbox,
                notes="No flood bbox was configured.",
            ),
        ]

    stac_api_url = str(dataset_cfg.get("stac_api_url", COPERNICUS_GFM_STAC_API)).strip()
    collection_id = str(dataset_cfg.get("collection_id", "GFM")).strip()
    asset_key = str(dataset_cfg.get("asset_key", "ensemble_flood_extent")).strip()
    product = str(dataset_cfg.get("product", collection_id)).strip()
    temporal_resolution = str(dataset_cfg.get("temporal_resolution", "event-based SAR flood extent")).strip()
    spatial_resolution = str(dataset_cfg.get("spatial_resolution_raw", "20 m")).strip()
    aggregation_days = int(dataset_cfg.get("aggregation_period_days", dataset_cfg.get("step_days", 7)))
    if aggregation_days <= 0:
        raise ValueError("`aggregation_period_days` must be a positive integer.")
    max_items_per_week = int(dataset_cfg.get("max_items_per_week", 200))
    if max_items_per_week <= 0:
        raise ValueError("`max_items_per_week` must be a positive integer.")

    dates = _date_sequence(dataset_cfg)
    if not dates:
        raise ValueError("Flood fetch requires at least one date in the selection window.")
    hard_end = dates[-1]

    dataset_dir = ensure_directory(context.raw_root / "flood" / "copernicus_gfm" / product)
    records: list[CatalogRecord] = []
    missing_weeks: list[str] = []
    failed_weeks: list[str] = []
    seen_assets: set[str] = set()
    total_weeks = len(dates)
    planned_weeks: list[dict[str, object]] = []
    reused_total = 0
    new_total = 0

    set_progress_total(context, total_weeks)
    if context.progress_bar is not None:
        context.progress_bar.set_description("flood plan")
        context.progress_bar.refresh()
    for week_idx, week_start in enumerate(dates, start=1):
        week_end = _week_end(week_start, window_days=aggregation_days, hard_end=hard_end)
        try:
            features = _search_week_features(
                stac_api_url=stac_api_url,
                collection_id=collection_id,
                bbox=[float(value) for value in bbox],
                start_day=week_start,
                end_day=week_end,
                max_items=max_items_per_week,
                context=context,
            )
        except HTTPError as exc:  # pragma: no cover - provider/network dependent
            failed_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} :: HTTP {exc.code}")
            if context.logger:
                pct = int(round(week_idx / total_weeks * 100))
                context.logger.info(
                    "[flood-plan] %s/%s (%s%%) week=%s..%s status=failed",
                    week_idx,
                    total_weeks,
                    pct,
                    week_start.isoformat(),
                    week_end.isoformat(),
                )
            update_progress(context)
            continue
        except Exception as exc:  # pragma: no cover - provider/network dependent
            failed_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} :: {exc}")
            if context.logger:
                pct = int(round(week_idx / total_weeks * 100))
                context.logger.info(
                    "[flood-plan] %s/%s (%s%%) week=%s..%s status=failed",
                    week_idx,
                    total_weeks,
                    pct,
                    week_start.isoformat(),
                    week_end.isoformat(),
                )
            update_progress(context)
            continue

        if not features:
            missing_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} (no STAC items)")
            if context.logger:
                pct = int(round(week_idx / total_weeks * 100))
                context.logger.info(
                    "[flood-plan] %s/%s (%s%%) week=%s..%s status=missing assets=0",
                    week_idx,
                    total_weeks,
                    pct,
                    week_start.isoformat(),
                    week_end.isoformat(),
                )
            update_progress(context)
            continue

        features_with_asset = [
            feature
            for feature in features
            if isinstance(feature.get("assets"), dict) and asset_key in feature["assets"]
        ]
        if not features_with_asset:
            missing_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} (asset `{asset_key}` missing)")
            if context.logger:
                pct = int(round(week_idx / total_weeks * 100))
                context.logger.info(
                    "[flood-plan] %s/%s (%s%%) week=%s..%s status=missing_asset assets=0",
                    week_idx,
                    total_weeks,
                    pct,
                    week_start.isoformat(),
                    week_end.isoformat(),
                )
            update_progress(context)
            continue

        planned_assets: list[dict[str, object]] = []
        for selected_feature in sorted(features_with_asset, key=_item_datetime):
            selected_assets = selected_feature.get("assets", {})
            asset = selected_assets.get(asset_key, {})
            source_url = str(asset.get("href", "")).strip()
            if not source_url:
                continue
            if source_url in seen_assets:
                continue
            seen_assets.add(source_url)

            stamp = _item_datetime(selected_feature)
            stamp_date = stamp.date() if stamp != datetime.min.replace(tzinfo=UTC) else week_start
            year = stamp_date.year
            doy = stamp_date.timetuple().tm_yday
            date_dir = ensure_directory(dataset_dir / f"{year}" / f"{doy:03d}")
            source_name = Path(source_url).name
            filename = source_name if source_name else f"{selected_feature.get('id', 'gfm')}_{asset_key}.tif"
            local_target = date_dir / filename

            planned_assets.append(
                {
                    "feature": selected_feature,
                    "source_url": source_url,
                    "local_target": local_target,
                    "stamp": stamp,
                }
            )

        if not planned_assets:
            missing_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} (asset hrefs empty or duplicate)")
            if context.logger:
                pct = int(round(week_idx / total_weeks * 100))
                context.logger.info(
                    "[flood-plan] %s/%s (%s%%) week=%s..%s status=empty assets=0",
                    week_idx,
                    total_weeks,
                    pct,
                    week_start.isoformat(),
                    week_end.isoformat(),
                )
            update_progress(context)
            continue

        planned_weeks.append(
            {
                "week_idx": week_idx,
                "week_start": week_start,
                "week_end": week_end,
                "assets": planned_assets,
            }
        )
        if context.logger:
            pct = int(round(week_idx / total_weeks * 100))
            context.logger.info(
                "[flood-plan] %s/%s (%s%%) week=%s..%s status=planned assets=%s",
                week_idx,
                total_weeks,
                pct,
                week_start.isoformat(),
                week_end.isoformat(),
                len(planned_assets),
            )

        update_progress(context)

    total_assets = sum(len(week["assets"]) for week in planned_weeks)
    set_progress_total(context, int(total_assets))
    if context.progress_bar is not None:
        context.progress_bar.n = 0
        context.progress_bar.set_description("flood fetch")
        context.progress_bar.refresh()

    for planned_week in planned_weeks:
        week_idx = int(planned_week["week_idx"])
        week_start = planned_week["week_start"]
        week_end = planned_week["week_end"]
        planned_assets = planned_week["assets"]
        week_reused = 0
        week_new = 0
        downloaded_this_week = 0
        for planned_asset in planned_assets:
            selected_feature = planned_asset["feature"]
            source_url = str(planned_asset["source_url"])
            local_target = Path(planned_asset["local_target"])
            stamp = planned_asset["stamp"]

            try:
                local_path, reused = ensure_local_copy(source_url, local_target, context)
            except Exception as exc:  # pragma: no cover - provider/network dependent
                failed_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} :: download failed: {exc}")
                continue
            if reused:
                week_reused += 1
                reused_total += 1
            else:
                week_new += 1
                new_total += 1

            downloaded_this_week += 1
            records.append(
                downloaded_record(
                    dataset_name="flood",
                    source_url=source_url,
                    local_path=local_path,
                    context=context,
                    license_or_access_note=COPERNICUS_GFM_LICENSE_NOTE,
                    spatial_resolution_raw=spatial_resolution,
                    temporal_resolution=temporal_resolution,
                    bbox=bbox,
                    notes=join_notes(
                        f"Copernicus GFM `{asset_key}` selected for weekly window {week_start.isoformat()}..{week_end.isoformat()}.",
                        f"STAC item: `{selected_feature.get('id', '')}`.",
                        f"Observation datetime (UTC): {stamp.isoformat()}." if stamp != datetime.min.replace(tzinfo=UTC) else "",
                        "Reused an existing local copy." if reused else "Downloaded from Copernicus GFM data store.",
                    ),
                ),
            )
        if downloaded_this_week == 0:
            missing_weeks.append(f"{week_start.isoformat()}..{week_end.isoformat()} (asset hrefs empty or downloads failed)")
        if context.logger:
            pct = int(round(week_idx / total_weeks * 100))
            context.logger.info(
                "[flood-progress] %s/%s (%s%%) week=%s..%s status=ok reused=%s new=%s totals(reused=%s,new=%s)",
                week_idx,
                total_weeks,
                pct,
                week_start.isoformat(),
                week_end.isoformat(),
                week_reused,
                week_new,
                reused_total,
                new_total,
            )

    if records:
        if missing_weeks and context.logger:
            context.logger.warning("Copernicus flood windows without data: %s", len(missing_weeks))
        if failed_weeks and context.logger:
            context.logger.warning("Copernicus flood fetch failures: %s", len(failed_weeks))
        return records

    details: list[str] = []
    if missing_weeks:
        details.append(f"missing_windows={len(missing_weeks)}")
    if failed_weeks:
        details.append(f"failed_windows={len(failed_weeks)}")
    instructions = f"""# Manual Steps For Copernicus GFM Flood

Automatic download did not produce any flood files.

Requested setup:
- STAC API: {stac_api_url}
- collection: {collection_id}
- asset: {asset_key}
- dates: {dates[0].isoformat()} .. {dates[-1].isoformat()}
- weekly step: {aggregation_days} days
- bbox: {bbox}

What to do:
1. Verify available GFM items in the STAC API for this period and AOI.
2. Check whether the requested asset key exists in returned items.
3. Download files manually into:
   `data/raw/flood/copernicus_gfm/{product}/<year>/<doy>/`
"""
    return [
        manual_record(
            dataset_name="flood",
            source_url=COPERNICUS_GFM_SOURCE,
            context=context,
            instruction_text=instructions,
            license_or_access_note=COPERNICUS_GFM_LICENSE_NOTE,
            spatial_resolution_raw=spatial_resolution,
            temporal_resolution=temporal_resolution,
            bbox=bbox,
            notes=", ".join(details) if details else "No files were downloaded for the requested setup.",
        ),
    ]
