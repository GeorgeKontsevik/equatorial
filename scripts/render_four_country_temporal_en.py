#!/usr/bin/env python3
"""Render English four-country rainfall/delay time series."""

import render_four_country_temporal_ru as base


base.OUTPUT = (
    base.REPO_ROOT / "itmo-phd-thesis-template-en/images/ch4/temporal_rain_burden_four_countries_square_en.png"
)
base.COUNTRY_LABELS = {
    "COL": "Colombia",
    "LBR": "Liberia",
    "CMR": "Cameroon",
    "GAB": "Gabon",
}
base.MONTH_LABELS = {
    1: "Jan",
    3: "Mar",
    5: "May",
    7: "Jul",
    9: "Sep",
    11: "Nov",
}
base.RAIN_YLABEL = "rainfall, mm/week"
base.DELAY_YLABEL = "delay, h/week"
base.RAIN_LEGEND_LABEL = "rainfall"
base.DELAY_LEGEND_LABEL = "accessibility delay"
base.TITLE = "Rainfall and Accessibility Delay"


if __name__ == "__main__":
    base.main()
