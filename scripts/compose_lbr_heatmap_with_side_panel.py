#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent

BASE_HEATMAP = ROOT / "outputs" / "astar_accessibility_weekly" / "cluster_connected_allclusters_10small_3large_3ports_3airports_delta_minutes_heatmaps" / "LBR_weekly_accessibility_impact_heatmap.png"
SIDE_PANEL = REPO_ROOT / "itmo-phd-thesis-template-en" / "images" / "ch4" / "lbr_precip_grid_week_2024_08_19.png"

# The original heatmap canvas before any right-column paste.
BASE_LEFT_WIDTH = 2765


def main() -> None:
    base_img = Image.open(BASE_HEATMAP).convert("RGB")
    if base_img.width > BASE_LEFT_WIDTH:
        base_img = base_img.crop((0, 0, BASE_LEFT_WIDTH, base_img.height))

    side_img = Image.open(SIDE_PANEL).convert("RGB")

    margin_top = 45
    margin_bottom = 45
    margin_between = 12
    margin_right = 18

    side_target_h = base_img.height - margin_top - margin_bottom
    side_img = ImageOps.contain(side_img, (1400, side_target_h), method=Image.Resampling.LANCZOS)

    canvas_w = base_img.width + margin_between + side_img.width + margin_right
    canvas = Image.new("RGB", (canvas_w, base_img.height), "white")
    canvas.paste(base_img, (0, 0))
    canvas.paste(side_img, (base_img.width + margin_between, margin_top))
    canvas.save(BASE_HEATMAP, quality=95)

    print(
        {
            "saved": str(BASE_HEATMAP),
            "left_size": base_img.size,
            "side_size": side_img.size,
            "canvas_size": canvas.size,
        },
        flush=True,
    )


if __name__ == "__main__":
    main()
