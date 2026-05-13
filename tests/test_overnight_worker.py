from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data import run_road_hazard_overnight_worker as worker


class OvernightWorkerCommandPlanTests(unittest.TestCase):
    def test_build_stage_commands_uses_separate_origin_selection_script(self) -> None:
        project_root = Path("/tmp/equatorial")
        output_root = project_root / "outputs" / "road_weekly_scenarios" / "GAB" / "2024-01-01_to_2024-12-31_7d_crop_connected_visibility_speed_dijkstra"
        args = Namespace(
            country_code="GAB",
            config=Path("config/generated/full_year_2024_test/gab.yaml"),
            damage_config=Path("config/road_climate_damage_gabon_2024_03_05.yaml"),
            thresholds_yaml=Path("config/road_hazard_thresholds_exact_mar_may.yaml"),
            start_date="2024-01-01",
            end_date="2024-12-31",
            step_days=7,
            city_threshold=50000,
            candidate_top_n=100,
            top_n_per_crop=3,
            spam_dir=Path("spam_tifs"),
            output_root=output_root,
            speed_paved_kmh=60.0,
            speed_unpaved_kmh=50.0,
            min_component_nodes=500,
            isolation_minutes=100000.0,
            run_fetch=False,
            fetch_datasets="",
            skip_overlay=False,
            skip_accessibility=False,
            skip_plots=False,
            skip_parquet=False,
            export_overlay_parquet=False,
        )

        plan = worker.build_stage_commands(args=args, project_root=project_root, python_bin="python")
        stage_names = [stage["stage"] for stage in plan["stages"]]
        modules = [" ".join(stage["command"]) for stage in plan["stages"]]

        self.assertIn("build_crop_origin_candidates", stage_names)
        self.assertIn("select_baseline_connected_crop_origins", stage_names)
        self.assertIn("weekly_accessibility_dijkstra", stage_names)
        self.assertTrue(
            any("src.data.select_baseline_connected_crop_origins" in command for command in modules),
            modules,
        )
        self.assertEqual(
            plan["origins_gpkg"],
            project_root
            / "outputs"
            / "road_weekly_scenarios"
            / "GAB"
            / "origins_spam_top3_by_crop_baseline_connected"
            / "spam_crop_top3_baseline_connected_origins.gpkg",
        )


if __name__ == "__main__":
    unittest.main()
