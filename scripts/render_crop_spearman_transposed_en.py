#!/usr/bin/env python3
"""Render English crop-by-factor Spearman heatmap."""

import render_crop_spearman_transposed_ru as base


base.OUTPUT = base.REPO_ROOT / "itmo-phd-thesis-template-en/images/ch4/crop_spearman_transposed_4x3_en.png"
base.CROP_LABELS = {
    "avocado": "avocado",
    "banana": "banana",
    "pineapple": "pineapple",
    "mango": "mango",
    "plantain": "plantain",
}
base.FACTORS = [
    ("rho_log_threshold_impact", "rainfall\nimpact", 1.0),
    ("rho_log_remoteness_h", "time\nremoteness", 1.0),
    ("rho_actual_unpaved_time_share", "unpaved\nroad share", -1.0),
]
base.TITLE = "Spearman Correlation with Accessibility Delay"
base.COLORBAR_LABEL = "Spearman rho"


if __name__ == "__main__":
    base.main()
