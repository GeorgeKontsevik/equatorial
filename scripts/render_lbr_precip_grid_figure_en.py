#!/usr/bin/env python3
"""Render English Liberia precipitation grid and road degradation panel."""

import sys

import render_lbr_precip_grid_figure as base


base.DEFAULT_THESIS_IMAGE = (
    base.REPO_ROOT / "itmo-phd-thesis-template-en" / "images" / "ch4" / "lbr_precip_grid_week_2024_08_19_en.png"
)
base.ROAD_TITLE_PREFIX = "Road degradation"
base.PRECIP_TITLE_PREFIX = "Precipitation"
base.DAMAGE_LABELS = ["minor", "moderate", "severe", "closure"]
base.DAMAGE_COLORBAR_LABEL = "Road degradation level"
base.PRECIP_COLORBAR_LABEL = "mm per week"
base.DATE_FORMAT = "%Y-%m-%d"


if __name__ == "__main__":
    if "--thesis-image" not in sys.argv:
        sys.argv.extend(["--thesis-image", str(base.DEFAULT_THESIS_IMAGE)])
    base.main()
